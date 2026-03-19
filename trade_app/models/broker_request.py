"""
BrokerRequest モデル
ブローカーに送信したリクエストの永続化記録。
発注・キャンセル・状態照会のすべてを記録する（監査・リカバリ用）。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class BrokerRequest(Base):
    """ブローカーリクエスト記録テーブル"""

    __tablename__ = "broker_requests"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 紐付け注文（リカバリ時のステータス照会などで NULL になる場合がある）
    order_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # リクエスト種別
    # "place"        : 発注
    # "cancel"       : キャンセル
    # "status_query" : 状態照会
    request_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    # ブローカーへ送信したペイロード（JSON）
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 送信時刻
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_broker_requests_type_sent", "request_type", "sent_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<BrokerRequest id={self.id[:8]} type={self.request_type} "
            f"order={self.order_id[:8] if self.order_id else 'None'}>"
        )
