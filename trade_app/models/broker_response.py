"""
BrokerResponse モデル
ブローカーから受信したレスポンスの永続化記録。
BrokerRequest と 1:1 で紐付く。エラーレスポンスも保存する。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class BrokerResponse(Base):
    """ブローカーレスポンス記録テーブル"""

    __tablename__ = "broker_responses"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 紐付けリクエスト
    broker_request_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("broker_requests.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 紐付け注文（クエリ効率のため冗長に保持）
    order_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # HTTP ステータスコード or "ok" / "error"
    status_code: Mapped[str] = mapped_column(String(16), nullable=False)

    # ブローカーから受信したペイロード（JSON）
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # エラーフラグ・エラーメッセージ
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # レスポンス受信時刻
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = ()

    def __repr__(self) -> str:
        return (
            f"<BrokerResponse id={self.id[:8]} status={self.status_code} "
            f"error={self.is_error}>"
        )
