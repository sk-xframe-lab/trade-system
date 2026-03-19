"""
AuditLogger サービス
システムが行った全操作を監査ログテーブルに APPEND ONLY で記録する。
監査ログは削除・更新しない。
"""
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.audit_log import AuditLog
from trade_app.models.enums import AuditEventType

logger = logging.getLogger(__name__)


class AuditLogger:
    """
    監査ログ書き込みサービス。
    DBセッションをコンストラクタで受け取り、呼び出し元と同一トランザクションで動作する。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def log(
        self,
        event_type: str | AuditEventType,
        entity_type: str,
        entity_id: str | None = None,
        actor: str = "system",
        details: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> AuditLog:
        """
        監査ログを1件書き込む。

        Args:
            event_type : AuditEventType 値（例: "signal_received"）
            entity_type: 対象エンティティ種別（"signal", "order", "position"）
            entity_id  : 対象エンティティの UUID
            actor      : 操作者（"system", "broker", "admin"）
            details    : 構造化データ（ticker, price, reason 等）
            message    : 人が読めるメッセージ（任意）

        Returns:
            保存した AuditLog オブジェクト
        """
        try:
            event_type_str = (
                event_type.value if isinstance(event_type, AuditEventType)
                else str(event_type)
            )
            entry = AuditLog(
                event_type=event_type_str,
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                details=details,
                message=message,
                created_at=datetime.now(timezone.utc),
            )
            self._db.add(entry)
            await self._db.flush()   # ID を確定させる（コミットは呼び出し元）

            logger.debug(
                "監査ログ: event=%s entity=%s:%s",
                event_type_str, entity_type, entity_id
            )
            return entry

        except Exception as e:
            # 監査ログの書き込みエラーは致命的。必ずエラーログを残す
            logger.error(
                "監査ログ書き込みエラー: event=%s entity=%s:%s error=%s",
                event_type, entity_type, entity_id, e,
                exc_info=True,
            )
            raise
