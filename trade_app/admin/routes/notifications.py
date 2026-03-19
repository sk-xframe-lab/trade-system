"""
通知設定ルーター (SCR-09)

仕様書: 管理画面仕様書 v0.3 §3(SCR-09)

【エンドポイント一覧】
GET    /admin/notifications              — 一覧
POST   /admin/notifications              — 新規作成
GET    /admin/notifications/{id}         — 1件取得
PATCH  /admin/notifications/{id}         — 更新
DELETE /admin/notifications/{id}         — 削除
POST   /admin/notifications/{id}/test    — テスト送信
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.constants import AdminAuditEventType
from trade_app.admin.schemas.common import MessageResponse
from trade_app.admin.schemas.notification_config import (
    NotificationConfigCreate,
    NotificationConfigResponse,
    NotificationConfigUpdate,
    NotificationTestResponse,
)
from trade_app.admin.routes._common import get_client_ip, get_user_agent
from trade_app.admin.services.audit_log_service import UiAuditLogService
from trade_app.admin.services.auth_guard import RequireAdmin, get_admin_db
from trade_app.admin.services.notification_service import NotificationConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["Admin Notifications"])


@router.get("", response_model=list[NotificationConfigResponse])
async def list_notifications(
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> list[NotificationConfigResponse]:
    """通知設定一覧を取得する"""
    svc = NotificationConfigService(db)
    configs = await svc.list_all()
    return [NotificationConfigResponse.model_validate(c) for c in configs]


@router.post("", response_model=NotificationConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_notification(
    request: Request,
    body: NotificationConfigCreate,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> NotificationConfigResponse:
    """通知設定を新規作成する"""
    svc = NotificationConfigService(db)
    audit_svc = UiAuditLogService(db)
    try:
        config, after_json = await svc.create(body, created_by=current_user.user_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    await audit_svc.write(
        AdminAuditEventType.NOTIFICATION_CONFIG_CREATED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="notification_config",
        resource_id=config.id,
        resource_label=f"{config.channel_type}:{config.destination}",
        after_json=after_json,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(config)
    return NotificationConfigResponse.model_validate(config)


@router.get("/{config_id}", response_model=NotificationConfigResponse)
async def get_notification(
    config_id: str,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> NotificationConfigResponse:
    """通知設定を1件取得する"""
    svc = NotificationConfigService(db)
    config = await svc.get(config_id)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="通知設定が見つかりません")
    return NotificationConfigResponse.model_validate(config)


@router.patch("/{config_id}", response_model=NotificationConfigResponse)
async def update_notification(
    config_id: str,
    request: Request,
    body: NotificationConfigUpdate,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> NotificationConfigResponse:
    """通知設定を更新する"""
    svc = NotificationConfigService(db)
    audit_svc = UiAuditLogService(db)
    try:
        config, before_json, after_json = await svc.update(
            config_id, body, updated_by=current_user.user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_svc.write(
        AdminAuditEventType.NOTIFICATION_CONFIG_UPDATED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="notification_config",
        resource_id=config.id,
        resource_label=f"{config.channel_type}:{config.destination}",
        before_json=before_json,
        after_json=after_json,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(config)
    return NotificationConfigResponse.model_validate(config)


@router.delete("/{config_id}", response_model=MessageResponse)
async def delete_notification(
    config_id: str,
    request: Request,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> MessageResponse:
    """通知設定を物理削除する"""
    svc = NotificationConfigService(db)
    audit_svc = UiAuditLogService(db)
    try:
        config = await svc.delete(config_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    before_json = {
        "id": config.id,
        "channel_type": config.channel_type,
        "destination": config.destination,
        "is_enabled": config.is_enabled,
    }
    await audit_svc.write(
        AdminAuditEventType.NOTIFICATION_CONFIG_DELETED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="notification_config",
        resource_id=config.id,
        resource_label=f"{config.channel_type}:{config.destination}",
        before_json=before_json,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return MessageResponse(message=f"通知設定 ({config.channel_type}) を削除しました")


@router.post("/{config_id}/test", response_model=NotificationTestResponse)
async def test_notification(
    config_id: str,
    request: Request,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> NotificationTestResponse:
    """
    テスト通知を送信する。

    TODO: メール/Telegram の実際の送信処理を実装すること（現在はスタブ）。
    """
    svc = NotificationConfigService(db)
    result = await svc.send_test(config_id)

    await audit_svc_write_if_ok(result, config_id, current_user, request, db)
    return result


async def audit_svc_write_if_ok(result, config_id, current_user, request, db):
    """テスト送信結果を監査ログに記録する"""
    audit_svc = UiAuditLogService(db)
    event_type = (
        AdminAuditEventType.NOTIFICATION_TEST_SENT
        if result.success
        else AdminAuditEventType.NOTIFICATION_TEST_FAILED
    )
    await audit_svc.write(
        event_type,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="notification_config",
        resource_id=config_id,
        description=result.message,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
