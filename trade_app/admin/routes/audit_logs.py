"""
監査ログルーター (SCR-12)

仕様書: 管理画面仕様書 v0.3 §3(SCR-12), §6(監査ログ要件)

【エンドポイント一覧】
GET /admin/audit-logs          — 一覧（フィルタ・ページネーション）
GET /admin/audit-logs/export   — CSV エクスポート（監査ログ記録あり）
GET /admin/audit-logs/{id}     — 詳細1件（モーダル表示用）

【APPEND ONLY 保証】
監査ログは一覧表示・詳細表示・エクスポートのみ。
INSERT のみ許可。UPDATE / DELETE エンドポイントは定義しない。

【CSV エクスポートの監査記録】
CSV エクスポートは機密性のある操作（全記録をダウンロード）のため
AUDIT_LOG_EXPORTED イベントとして監査ログに記録する。
CSV 生成後・StreamingResponse 返却前に commit する。
"""
import csv
import io
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.constants import AdminAuditEventType
from trade_app.admin.routes._common import get_client_ip, get_user_agent
from trade_app.admin.schemas.audit_log import AuditLogDetail, AuditLogFilter, AuditLogListItem
from trade_app.admin.schemas.common import PaginatedResponse
from trade_app.admin.services.audit_log_service import UiAuditLogService
from trade_app.admin.services.auth_guard import RequireAdmin, get_admin_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit-logs", tags=["Admin Audit Logs"])


@router.get("", response_model=PaginatedResponse[AuditLogListItem])
async def list_audit_logs(
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    user_email: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    resource_id: str | None = Query(default=None),
) -> PaginatedResponse[AuditLogListItem]:
    """監査ログ一覧を取得する（フィルタ・ページネーション対応）"""
    svc = UiAuditLogService(db)
    filters = AuditLogFilter(
        date_from=date_from,
        date_to=date_to,
        user_email=user_email,
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    offset = (page - 1) * per_page
    logs, total = await svc.query(filters, offset=offset, limit=per_page)

    items = [
        AuditLogListItem(
            id=log.id,
            created_at=log.created_at,
            user_email=log.user_email,
            ip_address=log.ip_address,
            event_type=log.event_type,
            resource_type=log.resource_type,
            resource_id=log.resource_id,
            resource_label=log.resource_label,
            change_summary=_build_change_summary(log),
        )
        for log in logs
    ]
    return PaginatedResponse.build(items=items, total=total, page=page, per_page=per_page)


@router.get("/export")
async def export_audit_logs_csv(
    request: Request,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    user_email: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
) -> StreamingResponse:
    """
    監査ログを CSV エクスポートする。

    最大 5000 件を返す。大量データの場合は date_from/date_to で範囲指定すること。
    エクスポート操作自体も AUDIT_LOG_EXPORTED として監査ログに記録される。
    """
    svc = UiAuditLogService(db)
    filters = AuditLogFilter(
        date_from=date_from,
        date_to=date_to,
        user_email=user_email,
        event_type=event_type,
        resource_type=resource_type,
    )
    logs, total = await svc.query(filters, offset=0, limit=5000)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "created_at", "user_email", "ip_address",
        "event_type", "resource_type", "resource_id", "resource_label", "description",
    ])
    for log in logs:
        writer.writerow([
            log.id,
            log.created_at.isoformat() if log.created_at else "",
            log.user_email or "",
            log.ip_address or "",
            log.event_type,
            log.resource_type or "",
            log.resource_id or "",
            log.resource_label or "",
            log.description or "",
        ])

    # CSV エクスポートを監査ログに記録する（機密性の高い操作のため必須）
    filter_summary = _build_export_filter_summary(date_from, date_to, user_email, event_type, resource_type)
    await svc.write(
        AdminAuditEventType.AUDIT_LOG_EXPORTED,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="audit_log",
        description=f"CSV エクスポート: {total} 件 / フィルタ: {filter_summary}",
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
    )
    await db.commit()

    output.seek(0)
    filename = f"audit_logs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    logger.info(
        "監査ログ CSV エクスポート: rows=%d by=%s filter=%s",
        total, current_user.email, filter_summary,
    )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/{log_id}", response_model=AuditLogDetail)
async def get_audit_log(
    log_id: str,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> AuditLogDetail:
    """監査ログ詳細を1件取得する（モーダル表示用）"""
    svc = UiAuditLogService(db)
    log = await svc.get_by_id(log_id)
    if log is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="監査ログが見つかりません")
    return AuditLogDetail.model_validate(log)


def _build_change_summary(log) -> str | None:
    """before_json / after_json から変更サマリーを生成する"""
    if log.before_json is None and log.after_json is None:
        return log.description

    changed_fields = []
    if log.before_json and log.after_json:
        for key in log.after_json:
            if key in log.before_json and log.before_json[key] != log.after_json[key]:
                changed_fields.append(key)
        if changed_fields:
            return f"変更フィールド: {', '.join(changed_fields)}"
    elif log.after_json:
        return f"作成: {', '.join(list(log.after_json.keys())[:5])}"
    elif log.before_json:
        return "削除"

    return log.description


def _build_export_filter_summary(
    date_from: datetime | None,
    date_to: datetime | None,
    user_email: str | None,
    event_type: str | None,
    resource_type: str | None,
) -> str:
    """CSV エクスポート時の監査ログ description 用フィルタサマリーを生成する"""
    parts = []
    if date_from:
        parts.append(f"from={date_from.date()}")
    if date_to:
        parts.append(f"to={date_to.date()}")
    if user_email:
        parts.append(f"user={user_email}")
    if event_type:
        parts.append(f"event={event_type}")
    if resource_type:
        parts.append(f"resource={resource_type}")
    return ", ".join(parts) if parts else "全件"
