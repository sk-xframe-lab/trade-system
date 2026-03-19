"""
管理者向け API ルーター

エンドポイント一覧:
  GET    /api/admin/halts                  : アクティブ halt 一覧
  POST   /api/admin/halts                  : 手動 halt 発動
  DELETE /api/admin/halts/{halt_id}        : 指定 halt 解除
  DELETE /api/admin/halts                  : 全 halt 解除
  POST   /api/positions/{position_id}/close: 指定ポジションを手動クローズ
  GET    /api/admin/status                 : システム稼働状況（halt + OPEN ポジション数）
  POST   /api/admin/strategies/init        : strategy seed データ投入（べき等）

認証: Authorization: Bearer <API_TOKEN>（既存シグナルエンドポイントと同一トークン）
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.config import get_settings
from trade_app.models.database import AsyncSessionLocal
from trade_app.models.enums import AuditEventType, ExitReason, HaltType, PositionStatus
from trade_app.models.position import Position
from trade_app.models.trading_halt import TradingHalt
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.halt_manager import HaltManager
from trade_app.services.position_manager import PositionManager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])
_bearer = HTTPBearer()


# ─── 認証ヘルパー ──────────────────────────────────────────────────────────────

def _verify_token(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    settings = get_settings()
    if credentials.credentials != settings.API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="認証エラー: トークンが無効です",
        )
    return credentials.credentials


# ─── DB セッションヘルパー ─────────────────────────────────────────────────────

async def _get_db():
    async with AsyncSessionLocal() as db:
        yield db


# ─── スキーマ ──────────────────────────────────────────────────────────────────

class HaltRequest(BaseModel):
    reason: str = Field(..., description="停止理由（人間可読）")


class HaltResponse(BaseModel):
    id: str
    halt_type: str
    reason: str
    is_active: bool
    activated_at: datetime
    deactivated_at: datetime | None
    activated_by: str
    deactivated_by: str | None
    details: dict | None

    model_config = {"from_attributes": True}


class ManualCloseRequest(BaseModel):
    exit_price: float | None = Field(
        default=None,
        description="決済価格。指定しない場合は現在価格（ブローカー経由）",
    )


class SystemStatusResponse(BaseModel):
    is_halted: bool
    halt_count: int
    active_halts: list[HaltResponse]
    open_position_count: int
    closing_position_count: int
    timestamp: datetime


# ─── halt 管理エンドポイント ──────────────────────────────────────────────────

@router.get(
    "/halts",
    response_model=list[HaltResponse],
    summary="アクティブな取引停止一覧を取得",
)
async def list_active_halts(
    _token: str = Depends(_verify_token),
    db: AsyncSession = Depends(_get_db),
):
    """is_active=True の取引停止レコードを全件返す。"""
    halt_mgr = HaltManager()
    halts = await halt_mgr.get_active_halts(db)
    return [HaltResponse.model_validate(h) for h in halts]


@router.post(
    "/halts",
    response_model=HaltResponse,
    status_code=status.HTTP_201_CREATED,
    summary="取引停止を手動発動",
)
async def activate_manual_halt(
    body: HaltRequest,
    _token: str = Depends(_verify_token),
    db: AsyncSession = Depends(_get_db),
):
    """
    手動で取引を停止する。
    halt が発動されると、以降のシグナル処理・発注が全てブロックされる。
    解除するまで停止状態が継続する。
    """
    halt_mgr = HaltManager()
    audit = AuditLogger(db)

    halt = await halt_mgr.activate_halt(
        db=db,
        halt_type=HaltType.MANUAL,
        reason=body.reason,
        activated_by="manual_api",
    )
    await audit.log(
        event_type=AuditEventType.HALT_ACTIVATED,
        entity_type="trading_halt",
        entity_id=halt.id,
        actor="manual_api",
        details={"halt_type": HaltType.MANUAL.value, "reason": body.reason},
        message=f"手動取引停止発動: {body.reason}",
    )
    await db.commit()

    logger.warning("管理API: 手動取引停止発動 id=%s reason=%s", halt.id[:8], body.reason)
    return HaltResponse.model_validate(halt)


@router.delete(
    "/halts/{halt_id}",
    response_model=HaltResponse,
    summary="指定した取引停止を解除",
)
async def deactivate_halt(
    halt_id: str,
    _token: str = Depends(_verify_token),
    db: AsyncSession = Depends(_get_db),
):
    """指定した halt_id の取引停止を解除する。"""
    halt_mgr = HaltManager()
    audit = AuditLogger(db)

    halt = await halt_mgr.deactivate_halt(
        db=db,
        halt_id=halt_id,
        deactivated_by="manual_api",
    )
    if halt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"halt が見つかりません: {halt_id}",
        )

    await audit.log(
        event_type=AuditEventType.HALT_DEACTIVATED,
        entity_type="trading_halt",
        entity_id=halt.id,
        actor="manual_api",
        details={"halt_type": halt.halt_type},
        message=f"取引停止解除: {halt.halt_type} id={halt.id[:8]}",
    )
    await db.commit()

    logger.info("管理API: 取引停止解除 id=%s", halt_id[:8])
    return HaltResponse.model_validate(halt)


@router.delete(
    "/halts",
    summary="全ての取引停止を解除",
)
async def deactivate_all_halts(
    _token: str = Depends(_verify_token),
    db: AsyncSession = Depends(_get_db),
):
    """全ての is_active=True の halt を解除する。"""
    halt_mgr = HaltManager()
    audit = AuditLogger(db)

    count = await halt_mgr.deactivate_all_halts(db=db, deactivated_by="manual_api")

    await audit.log(
        event_type=AuditEventType.HALT_DEACTIVATED,
        entity_type="trading_halt",
        entity_id=None,
        actor="manual_api",
        details={"deactivated_count": count},
        message=f"全取引停止解除: {count} 件",
    )
    await db.commit()

    logger.info("管理API: 全取引停止解除 count=%d", count)
    return {"deactivated_count": count, "message": f"{count} 件の取引停止を解除しました"}


# ─── システム状況エンドポイント ────────────────────────────────────────────────

@router.get(
    "/status",
    response_model=SystemStatusResponse,
    summary="システム稼働状況を取得",
)
async def get_system_status(
    _token: str = Depends(_verify_token),
    db: AsyncSession = Depends(_get_db),
):
    """halt 状態・ポジション数などのシステム稼働状況を返す。"""
    from sqlalchemy import func

    halt_mgr = HaltManager()
    active_halts = await halt_mgr.get_active_halts(db)

    open_count_result = await db.execute(
        select(func.count(Position.id)).where(
            Position.status == PositionStatus.OPEN.value
        )
    )
    closing_count_result = await db.execute(
        select(func.count(Position.id)).where(
            Position.status == PositionStatus.CLOSING.value
        )
    )

    return SystemStatusResponse(
        is_halted=len(active_halts) > 0,
        halt_count=len(active_halts),
        active_halts=[HaltResponse.model_validate(h) for h in active_halts],
        open_position_count=open_count_result.scalar() or 0,
        closing_position_count=closing_count_result.scalar() or 0,
        timestamp=datetime.now(timezone.utc),
    )


# ─── ポジション手動クローズエンドポイント ─────────────────────────────────────

@router.post(
    "/positions/{position_id}/close",
    summary="指定ポジションを手動クローズ",
)
async def manual_close_position(
    position_id: str,
    body: ManualCloseRequest,
    _token: str = Depends(_verify_token),
    db: AsyncSession = Depends(_get_db),
):
    """
    指定したポジションを手動でクローズする。

    - exit_price 指定あり: 指定価格で即時 close_position()（テスト・緊急用）
    - exit_price 指定なし: ブローカー経由で exit 注文を発行（initiate_exit）
    """
    result = await db.execute(
        select(Position).where(Position.id == position_id)
    )
    position = result.scalar_one_or_none()
    if position is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ポジションが見つかりません: {position_id}",
        )

    if position.status == PositionStatus.CLOSED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ポジションは既にクローズ済みです",
        )

    if position.status == PositionStatus.CLOSING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ポジションは既にクローズ処理中です（exit注文送信済み）",
        )

    audit = AuditLogger(db)
    pos_manager = PositionManager(db=db, audit=audit)

    if body.exit_price is not None:
        # ─── 即時クローズ（価格指定） ──────────────────────────────────────
        result_obj = await pos_manager.close_position(
            position=position,
            exit_price=body.exit_price,
            exit_reason=ExitReason.MANUAL,
        )
        logger.info(
            "管理API: ポジション手動クローズ（即時）pos=%s price=%.0f pnl=%+.0f",
            position_id[:8], body.exit_price, result_obj.pnl,
        )
        return {
            "status": "closed",
            "position_id": position_id,
            "exit_price": body.exit_price,
            "pnl": result_obj.pnl,
            "message": f"ポジションをクローズしました PnL={result_obj.pnl:+.0f}円",
        }
    else:
        # ─── exit 注文経由クローズ ─────────────────────────────────────────
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter
        settings = get_settings()
        broker = (
            TachibanaBrokerAdapter()
            if settings.BROKER_TYPE == "tachibana"
            else MockBrokerAdapter()
        )
        exit_order = await pos_manager.initiate_exit(
            position=position,
            exit_reason=ExitReason.MANUAL,
            broker=broker,
            triggered_by="manual_api",
        )
        logger.info(
            "管理API: ポジション手動クローズ開始（exit注文）pos=%s order=%s",
            position_id[:8], exit_order.id[:8],
        )
        return {
            "status": "closing",
            "position_id": position_id,
            "exit_order_id": exit_order.id,
            "message": "exit注文を発行しました。約定後にクローズ完了します。",
        }


# ─── Strategy seed エンドポイント ─────────────────────────────────────────────

@router.post(
    "/strategies/init",
    summary="strategy seed データを投入（べき等）",
    status_code=status.HTTP_200_OK,
)
async def init_strategies(
    _token: str = Depends(_verify_token),
    db: AsyncSession = Depends(_get_db),
):
    """
    strategy_definitions / strategy_conditions の seed データを投入する。

    - べき等: 既存の strategy_code が存在する場合はスキップする
    - 自動起動なし: このエンドポイントを明示的に叩いた場合のみ実行する
    - 発注は行わない

    ⚠️ 本番環境での初回セットアップまたは strategy 追加時に使用する。
    """
    from trade_app.services.strategy.seed import seed_strategies
    seeded = await seed_strategies(db)
    await db.commit()

    logger.info("管理API: strategy seed 投入 count=%d", len(seeded))
    return {
        "message": f"strategy seed 投入完了: {len(seeded)} 件処理",
        "seeded": len(seeded),
    }
