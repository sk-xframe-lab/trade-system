"""
halt 操作ルーター — 管理画面版 (SCR-06, SCR-07)

仕様書: 管理画面仕様書 v0.3 §3(SCR-06, SCR-07)

【既存 /api/admin/halts との関係】
trade_app/routes/admin.py に既存の halt エンドポイントがある（API_TOKEN 認証）。
こちらは管理画面用の UI 認証（セッショントークン）対応版。
HaltManager は共通コンポーネントを再利用する。

【DB セッション分離】
このルーターは 2 つの DB セッションを使用する:
  - trade_db (get_trade_db): HaltManager 経由で trading_halts テーブルを操作
  - admin_db (get_admin_db): UiAuditLogService 経由で ui_audit_logs テーブルを操作

trading_halts は trade_db に存在するため get_trade_db を使用する。
ui_audit_logs は admin_db に存在するため get_admin_db を使用する。
2 つのセッションは独立してコミットする（別トランザクション）。

【エンドポイント一覧】
GET    /admin/halt          — アクティブ halt 一覧
POST   /admin/halt          — 手動 halt 発動
DELETE /admin/halt/{id}     — 指定 halt 解除
DELETE /admin/halt          — 全 halt 解除
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.constants import AdminAuditEventType
from trade_app.admin.database import get_trade_db
from trade_app.admin.schemas.common import MessageResponse
from trade_app.admin.routes._common import get_client_ip, get_user_agent
from trade_app.admin.services.audit_log_service import UiAuditLogService
from trade_app.admin.services.auth_guard import RequireAdmin, get_admin_db
from trade_app.models.enums import HaltType
from trade_app.services.halt_manager import HaltManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/halt", tags=["Admin Halt"])


class HaltCreateRequest(BaseModel):
    reason: str = Field(..., description="停止理由（人間可読）", min_length=1)


class HaltItem(BaseModel):
    id: str
    halt_type: str
    reason: str
    is_active: bool
    activated_at: str
    activated_by: str | None

    model_config = {"from_attributes": True}


class HaltListResponse(BaseModel):
    halts: list[HaltItem]
    count: int
    is_halted: bool


@router.get("", response_model=HaltListResponse)
async def list_halts(
    current_user: RequireAdmin,
    trade_db: AsyncSession = Depends(get_trade_db),
) -> HaltListResponse:
    """アクティブ halt 一覧を返す"""
    halt_mgr = HaltManager()
    active_halts = await halt_mgr.get_active_halts(trade_db)
    items = [
        HaltItem(
            id=h.id,
            halt_type=h.halt_type,
            reason=h.reason,
            is_active=h.is_active,
            activated_at=h.activated_at.isoformat(),
            activated_by=h.activated_by,
        )
        for h in active_halts
    ]
    return HaltListResponse(
        halts=items,
        count=len(items),
        is_halted=len(items) > 0,
    )


@router.post("", response_model=HaltItem, status_code=status.HTTP_201_CREATED)
async def create_halt(
    request: Request,
    body: HaltCreateRequest,
    current_user: RequireAdmin,
    trade_db: AsyncSession = Depends(get_trade_db),
    admin_db: AsyncSession = Depends(get_admin_db),
) -> HaltItem:
    """手動 halt を発動する"""
    halt_mgr = HaltManager()
    halt = await halt_mgr.activate_halt(
        trade_db,
        halt_type=HaltType.MANUAL,
        reason=body.reason,
        activated_by=current_user.email,
    )
    await trade_db.commit()

    audit_svc = UiAuditLogService(admin_db)
    await audit_svc.write(
        AdminAuditEventType.HALT_TRIGGERED_MANUAL,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="trading_halt",
        resource_id=halt.id,
        resource_label=body.reason,
        after_json={"halt_type": halt.halt_type, "reason": halt.reason},
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await admin_db.commit()

    logger.info("手動halt発動: id=%s by=%s reason=%s", halt.id[:8], current_user.email, body.reason)
    return HaltItem(
        id=halt.id,
        halt_type=halt.halt_type,
        reason=halt.reason,
        is_active=halt.is_active,
        activated_at=halt.activated_at.isoformat(),
        activated_by=halt.activated_by,
    )


@router.delete("/{halt_id}", response_model=MessageResponse)
async def release_halt(
    halt_id: str,
    request: Request,
    current_user: RequireAdmin,
    trade_db: AsyncSession = Depends(get_trade_db),
    admin_db: AsyncSession = Depends(get_admin_db),
) -> MessageResponse:
    """指定した halt を解除する"""
    halt_mgr = HaltManager()
    released = await halt_mgr.deactivate_halt(
        trade_db,
        halt_id=halt_id,
        deactivated_by=current_user.email,
    )
    if not released:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"halt が見つかりません: {halt_id}",
        )
    await trade_db.commit()

    audit_svc = UiAuditLogService(admin_db)
    await audit_svc.write(
        AdminAuditEventType.HALT_RELEASED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="trading_halt",
        resource_id=halt_id,
        description=f"halt 解除: {halt_id[:8]}",
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await admin_db.commit()
    return MessageResponse(message=f"halt を解除しました: {halt_id[:8]}")


@router.delete("", response_model=MessageResponse)
async def release_all_halts(
    request: Request,
    current_user: RequireAdmin,
    trade_db: AsyncSession = Depends(get_trade_db),
    admin_db: AsyncSession = Depends(get_admin_db),
) -> MessageResponse:
    """アクティブな全 halt を解除する"""
    halt_mgr = HaltManager()
    count = await halt_mgr.deactivate_all_halts(
        trade_db,
        deactivated_by=current_user.email,
    )
    await trade_db.commit()

    audit_svc = UiAuditLogService(admin_db)
    await audit_svc.write(
        AdminAuditEventType.HALT_RELEASED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="trading_halt",
        description=f"全 halt 解除: {count} 件",
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await admin_db.commit()
    return MessageResponse(message=f"全 halt を解除しました（{count} 件）")
