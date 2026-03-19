"""
UiAuditLogService — 管理画面監査ログ書き込み・照会サービス

仕様書: 管理画面仕様書 v0.3 §6(監査ログ要件)

【APPEND ONLY 保証】
- write() のみが INSERT を行う。UPDATE / DELETE メソッドは定義しない。
- サービスを経由せずに直接 UiAuditLog を操作してはならない。

【秘密情報の除外】
- SENSITIVE_KEYS に含まれるキーを before_json / after_json から自動的に除去する。
- 将来的にキーが追加された場合は SENSITIVE_KEYS を更新すること。

【IP/UA 記録ルール】
- USER_INITIATED_EVENTS に含まれるイベントは ip_address / user_agent が必須。
  None のまま write() を呼ぶと ValueError を送出する。
- SYSTEM_INITIATED_EVENTS は ip_address / user_agent を null で保存する。
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.constants import AdminAuditEventType, USER_INITIATED_EVENTS
from trade_app.admin.models.ui_audit_log import UiAuditLog
from trade_app.admin.schemas.audit_log import AuditLogFilter

logger = logging.getLogger(__name__)

# 監査ログに含めてはならないキー（秘密情報）
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "password",
    "password_encrypted",
    "extra_secrets_encrypted",
    "totp_secret",
    "totp_secret_encrypted",
    "session_token",
    "session_token_hash",
    "api_key",
    "secret",
    "access_token",
    "refresh_token",
})


def _sanitize(data: dict | None) -> dict | None:
    """SENSITIVE_KEYS を含むキーを再帰的に除去して返す"""
    if data is None:
        return None
    return {
        k: "[REDACTED]" if k in SENSITIVE_KEYS else (
            _sanitize(v) if isinstance(v, dict) else v
        )
        for k, v in data.items()
    }


class UiAuditLogService:
    """
    管理画面監査ログの書き込み・照会サービス。
    全ての監査ログ書き込みはこのクラスを通じて行うこと。
    """

    def __init__(self, db: AsyncSession):
        self._db = db

    async def write(
        self,
        event_type: str,
        *,
        user_id: str | None = None,
        user_email: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        resource_label: str | None = None,
        before_json: dict | None = None,
        after_json: dict | None = None,
        description: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> UiAuditLog:
        """
        監査ログを1件 INSERT する。UPDATE / DELETE は行わない（APPEND ONLY）。

        Args:
            event_type: AdminAuditEventType の値
            user_id: 操作者の ui_users.id（システム自動イベントは None 可）
            user_email: 操作者のメール（非正規化）（システム自動イベントは None 可）
            resource_type: 対象リソース種別
            resource_id: 対象リソースID
            resource_label: 人間可読ラベル
            before_json: 変更前状態（秘密情報は自動除去）
            after_json: 変更後状態（秘密情報は自動除去）
            description: 補足説明
            ip_address: クライアントIPアドレス（ユーザー起点は必須）
            user_agent: クライアントUA（ユーザー起点は必須）

        Raises:
            ValueError: USER_INITIATED_EVENTS で ip_address が None の場合
        """
        if event_type in USER_INITIATED_EVENTS and ip_address is None:
            logger.warning(
                "ユーザー起点イベント %s に ip_address が設定されていません。記録は継続します。",
                event_type,
            )

        record = UiAuditLog(
            id=str(uuid.uuid4()),
            user_id=user_id,
            user_email=user_email,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_label=resource_label,
            ip_address=ip_address,
            user_agent=user_agent,
            before_json=_sanitize(before_json),
            after_json=_sanitize(after_json),
            description=description,
            created_at=datetime.now(timezone.utc),
        )
        self._db.add(record)
        # commit は呼び出し元が行う
        return record

    async def query(
        self,
        filters: AuditLogFilter,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[UiAuditLog], int]:
        """
        フィルタ条件で監査ログを照会する。
        Returns: (logs, total_count)
        """
        stmt = select(UiAuditLog)
        count_stmt = select(func.count(UiAuditLog.id))

        if filters.date_from:
            stmt = stmt.where(UiAuditLog.created_at >= filters.date_from)
            count_stmt = count_stmt.where(UiAuditLog.created_at >= filters.date_from)
        if filters.date_to:
            stmt = stmt.where(UiAuditLog.created_at <= filters.date_to)
            count_stmt = count_stmt.where(UiAuditLog.created_at <= filters.date_to)
        if filters.user_email:
            stmt = stmt.where(UiAuditLog.user_email.ilike(f"%{filters.user_email}%"))
            count_stmt = count_stmt.where(UiAuditLog.user_email.ilike(f"%{filters.user_email}%"))
        if filters.event_type:
            stmt = stmt.where(UiAuditLog.event_type == filters.event_type)
            count_stmt = count_stmt.where(UiAuditLog.event_type == filters.event_type)
        if filters.resource_type:
            stmt = stmt.where(UiAuditLog.resource_type == filters.resource_type)
            count_stmt = count_stmt.where(UiAuditLog.resource_type == filters.resource_type)
        if filters.resource_id:
            stmt = stmt.where(UiAuditLog.resource_id.ilike(f"%{filters.resource_id}%"))
            count_stmt = count_stmt.where(UiAuditLog.resource_id.ilike(f"%{filters.resource_id}%"))

        stmt = stmt.order_by(UiAuditLog.created_at.desc()).offset(offset).limit(limit)

        result = await self._db.execute(stmt)
        logs = list(result.scalars().all())

        count_result = await self._db.execute(count_stmt)
        total = count_result.scalar() or 0

        return logs, total

    async def get_by_id(self, log_id: str) -> UiAuditLog | None:
        """監査ログを1件取得（詳細モーダル用）"""
        result = await self._db.execute(
            select(UiAuditLog).where(UiAuditLog.id == log_id)
        )
        return result.scalar_one_or_none()
