"""
銘柄設定ルーター (SCR-04, SCR-05)

仕様書: 管理画面仕様書 v0.3 §3(SCR-04, SCR-05), §4(銘柄操作)

【エンドポイント一覧】
GET    /admin/symbols              — 一覧（フィルタ・ページネーション）
POST   /admin/symbols              — 新規作成
GET    /admin/symbols/{id}         — 1件取得
PATCH  /admin/symbols/{id}         — 更新（symbol_code 変更不可）
DELETE /admin/symbols/{id}         — 論理削除
PATCH  /admin/symbols/{id}/enable  — 有効化
PATCH  /admin/symbols/{id}/disable — 無効化
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.constants import AdminAuditEventType
from trade_app.admin.schemas.common import MessageResponse, PaginatedResponse
from trade_app.admin.schemas.symbol_config import (
    SymbolConfigCreate,
    SymbolConfigFilter,
    SymbolConfigResponse,
    SymbolConfigUpdate,
)
from trade_app.admin.routes._common import get_client_ip, get_user_agent
from trade_app.admin.services.audit_log_service import UiAuditLogService
from trade_app.admin.services.auth_guard import RequireAdmin, get_admin_db
from trade_app.admin.services.symbol_config_service import SymbolConfigService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/symbols", tags=["Admin Symbols"])


@router.get("", response_model=PaginatedResponse[SymbolConfigResponse])
async def list_symbols(
    request: Request,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    trade_type: str | None = Query(default=None),
    is_enabled: bool | None = Query(default=None),
    strategy_id: str | None = Query(default=None),
    search: str | None = Query(default=None, description="symbol_code / symbol_name 部分一致"),
    include_deleted: bool = Query(default=False),
) -> PaginatedResponse[SymbolConfigResponse]:
    """銘柄設定一覧を取得する（フィルタ・ページネーション対応）"""
    svc = SymbolConfigService(db)
    filters = SymbolConfigFilter(
        trade_type=trade_type,
        is_enabled=is_enabled,
        strategy_id=strategy_id,
        search=search,
        include_deleted=include_deleted,
    )
    offset = (page - 1) * per_page
    symbols, total = await svc.list(filters, offset=offset, limit=per_page)
    items = [SymbolConfigResponse.model_validate(s) for s in symbols]
    return PaginatedResponse.build(items=items, total=total, page=page, per_page=per_page)


@router.post("", response_model=SymbolConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_symbol(
    request: Request,
    body: SymbolConfigCreate,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> SymbolConfigResponse:
    """銘柄設定を新規作成する"""
    svc = SymbolConfigService(db)
    audit_svc = UiAuditLogService(db)
    try:
        symbol, after_json = await svc.create(body, created_by=current_user.user_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    await audit_svc.write(
        AdminAuditEventType.SYMBOL_CREATED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="symbol_config",
        resource_id=symbol.id,
        resource_label=symbol.symbol_code,
        after_json=after_json,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(symbol)
    return SymbolConfigResponse.model_validate(symbol)


@router.get("/{symbol_id}", response_model=SymbolConfigResponse)
async def get_symbol(
    symbol_id: str,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> SymbolConfigResponse:
    """銘柄設定を1件取得する"""
    svc = SymbolConfigService(db)
    symbol = await svc.get(symbol_id)
    if symbol is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="銘柄設定が見つかりません")
    return SymbolConfigResponse.model_validate(symbol)


@router.patch("/{symbol_id}", response_model=SymbolConfigResponse)
async def update_symbol(
    symbol_id: str,
    request: Request,
    body: SymbolConfigUpdate,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> SymbolConfigResponse:
    """銘柄設定を更新する（symbol_code は変更不可）"""
    svc = SymbolConfigService(db)
    audit_svc = UiAuditLogService(db)
    try:
        symbol, before_json, after_json = await svc.update(
            symbol_id, body, updated_by=current_user.user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_svc.write(
        AdminAuditEventType.SYMBOL_UPDATED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="symbol_config",
        resource_id=symbol.id,
        resource_label=symbol.symbol_code,
        before_json=before_json,
        after_json=after_json,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(symbol)
    return SymbolConfigResponse.model_validate(symbol)


@router.delete("/{symbol_id}", response_model=MessageResponse)
async def delete_symbol(
    symbol_id: str,
    request: Request,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> MessageResponse:
    """銘柄設定を論理削除する"""
    svc = SymbolConfigService(db)
    audit_svc = UiAuditLogService(db)
    try:
        symbol, before_json = await svc.soft_delete(symbol_id, deleted_by=current_user.user_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_svc.write(
        AdminAuditEventType.SYMBOL_DELETED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="symbol_config",
        resource_id=symbol.id,
        resource_label=symbol.symbol_code,
        before_json=before_json,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    return MessageResponse(message=f"銘柄設定 '{symbol.symbol_code}' を削除しました")


@router.patch("/{symbol_id}/enable", response_model=SymbolConfigResponse)
async def enable_symbol(
    symbol_id: str,
    request: Request,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> SymbolConfigResponse:
    """銘柄設定を有効化する"""
    svc = SymbolConfigService(db)
    audit_svc = UiAuditLogService(db)
    try:
        symbol, before_json, after_json = await svc.toggle_enabled(
            symbol_id, enabled=True, updated_by=current_user.user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_svc.write(
        AdminAuditEventType.SYMBOL_ENABLED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="symbol_config",
        resource_id=symbol.id,
        resource_label=symbol.symbol_code,
        before_json=before_json,
        after_json=after_json,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(symbol)
    return SymbolConfigResponse.model_validate(symbol)


@router.patch("/{symbol_id}/disable", response_model=SymbolConfigResponse)
async def disable_symbol(
    symbol_id: str,
    request: Request,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> SymbolConfigResponse:
    """銘柄設定を無効化する"""
    svc = SymbolConfigService(db)
    audit_svc = UiAuditLogService(db)
    try:
        symbol, before_json, after_json = await svc.toggle_enabled(
            symbol_id, enabled=False, updated_by=current_user.user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    await audit_svc.write(
        AdminAuditEventType.SYMBOL_DISABLED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="symbol_config",
        resource_id=symbol.id,
        resource_label=symbol.symbol_code,
        before_json=before_json,
        after_json=after_json,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()
    await db.refresh(symbol)
    return SymbolConfigResponse.model_validate(symbol)
