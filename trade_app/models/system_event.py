"""
SystemEvent モデル
起動・シャットダウン・リカバリ・照合などのシステムレベルイベントを記録する。
AuditLog が「取引操作」の監査ならば SystemEvent は「システム動作」の監査。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class SystemEvent(Base):
    """システムイベントテーブル（APPEND ONLY）"""

    __tablename__ = "system_events"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # イベント種別（SystemEventType enum 値）
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # 詳細情報（recovered_count / reconcile_diff / error 等）
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 人が読めるサマリー
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        Index("ix_system_events_type_created", "event_type", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<SystemEvent type={self.event_type} at={self.created_at}>"
