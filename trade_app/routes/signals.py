"""
POST /api/signals                                 — シグナル受信エンドポイント
GET  /api/signals/{id}                            — シグナル状態照会エンドポイント
GET  /api/signals/{id}/strategy-decision          — strategy gate 判定結果照会

処理フロー（202 Accepted パターン）:
  1. ヘッダーバリデーション（Authorization / Idempotency-Key / X-Source-System）
  2. Pydantic バリデーション
  3. SignalReceiver.receive() → DB保存 + Redis冪等性登録
  4. BackgroundTasks でリスクチェック → 発注 → ポジション開設を非同期実行
  5. 202 Accepted を即時返却
"""
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as redis

from trade_app.config import get_settings
from trade_app.models.database import get_db
from trade_app.models.signal import TradeSignal
from trade_app.schemas.signal import (
    SignalAcceptedResponse,
    SignalDuplicateResponse,
    SignalRequest,
    SignalStatusResponse,
    SignalStrategyDecisionResponse,
)
from trade_app.models.signal_strategy_decision import SignalStrategyDecision
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.signal_receiver import DuplicateSignalError, SignalReceiver

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/signals", tags=["signals"])
settings = get_settings()


def get_redis_client() -> redis.Redis:
    """Redis クライアントを返す（main.py で初期化したものを参照）"""
    from trade_app.main import get_redis_client as _get
    return _get()


def _verify_auth(authorization: str) -> None:
    """
    Bearer トークン認証を検証する。
    Authorization: Bearer <token> の形式を期待する。
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authorization ヘッダーの形式が不正です（Bearer <token> 形式が必要）",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="無効な API トークンです",
        )


def _verify_idempotency_key(key: str) -> str:
    """Idempotency-Key の形式を検証する（UUID v4 形式）"""
    try:
        uuid.UUID(key, version=4)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Idempotency-Key が UUID v4 形式でありません: {key}",
        )
    return key


# ─── POST /api/signals ────────────────────────────────────────────────────────

@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SignalAcceptedResponse,
    responses={
        409: {"model": SignalDuplicateResponse, "description": "重複シグナル"},
        403: {"description": "認証エラー"},
        422: {"description": "バリデーションエラー"},
    },
    summary="売買シグナルを受信する",
)
async def receive_signal(
    request: SignalRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis_client),
    authorization: str = Header(..., description="Bearer <API_TOKEN>"),
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", description="UUID v4 形式の冪等性キー"
    ),
    x_source_system: str = Header(
        ..., alias="X-Source-System", description="送信元システム識別子"
    ),
):
    """
    分析システムからの売買シグナルを受信・保存し、バックグラウンドで処理を開始する。

    - 同一 Idempotency-Key の重複送信は 409 を返す（冪等性保証）
    - バリデーション通過後は即時 202 を返し、発注処理はバックグラウンドで実行
    - 処理結果は GET /api/signals/{signal_id} で確認できる
    """
    # ─── ヘッダーバリデーション ───────────────────────────────────────────
    _verify_auth(authorization)
    _verify_idempotency_key(idempotency_key)

    audit = AuditLogger(db)
    receiver = SignalReceiver(db=db, redis_client=redis_client, audit=audit)

    try:
        signal = await receiver.receive(
            request=request,
            idempotency_key=idempotency_key,
            source_system=x_source_system,
        )
    except DuplicateSignalError as e:
        return SignalDuplicateResponse(signal_id=e.signal_id)  # type: ignore[return-value]

    # ─── バックグラウンドで発注処理を実行（SignalPipeline 経由）─────────
    # SignalPipeline は FastAPI 依存がなく、将来 Celery/ARQ にも移行可能
    from trade_app.services.pipeline import SignalPipeline
    background_tasks.add_task(
        SignalPipeline.process,
        signal.id,
    )

    return SignalAcceptedResponse(
        signal_id=signal.id,
        idempotency_key=idempotency_key,
    )


# ─── GET /api/signals/{signal_id} ────────────────────────────────────────────

@router.get(
    "/{signal_id}",
    response_model=SignalStatusResponse,
    summary="シグナルの処理状態を照会する",
)
async def get_signal_status(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
):
    """シグナルの現在の処理状態を返す"""
    _verify_auth(authorization)

    result = await db.execute(
        select(TradeSignal).where(TradeSignal.id == signal_id)
    )
    signal = result.scalar_one_or_none()

    if signal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"シグナルが見つかりません: {signal_id}",
        )

    return SignalStatusResponse(
        signal_id=signal.id,
        ticker=signal.ticker,
        side=signal.side,
        order_type=signal.order_type,
        quantity=signal.quantity,
        limit_price=signal.limit_price,
        strategy=signal.strategy,
        score=signal.score,
        status=signal.status,
        reject_reason=signal.reject_reason,
        generated_at=signal.generated_at,
        received_at=signal.received_at,
    )


# ─── GET /api/signals/{signal_id}/strategy-decision ──────────────────────────

@router.get(
    "/{signal_id}/strategy-decision",
    response_model=SignalStrategyDecisionResponse,
    summary="signal に適用された strategy gate 判定結果を照会する",
    responses={
        404: {"description": "シグナルまたは判定結果が見つからない"},
        403: {"description": "認証エラー"},
    },
)
async def get_signal_strategy_decision(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
):
    """
    指定シグナルに対して SignalStrategyGate が記録した最新の判定結果を返す。

    - entry signal のみ記録される（exit signal は gate をバイパスするため記録なし）
    - 同一 signal_id に複数レコードが存在する場合は最新（decision_time DESC）を返す
    """
    _verify_auth(authorization)

    result = await db.execute(
        select(SignalStrategyDecision)
        .where(SignalStrategyDecision.signal_id == signal_id)
        .order_by(SignalStrategyDecision.decision_time.desc())
        .limit(1)
    )
    decision = result.scalar_one_or_none()

    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"strategy gate 判定結果が見つかりません: signal_id={signal_id}",
        )

    return SignalStrategyDecisionResponse(
        id=decision.id,
        signal_id=decision.signal_id,
        ticker=decision.ticker,
        signal_direction=decision.signal_direction,
        global_decision_id=decision.global_decision_id,
        symbol_decision_id=decision.symbol_decision_id,
        decision_time=decision.decision_time,
        entry_allowed=decision.entry_allowed,
        size_ratio=decision.size_ratio,
        blocking_reasons=decision.blocking_reasons_json or [],
        evidence=decision.evidence_json or {},
        created_at=decision.created_at,
    )
