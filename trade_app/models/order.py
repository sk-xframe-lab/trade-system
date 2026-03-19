"""
Order モデル
シグナルに対して実際にブローカーへ送信した注文の記録。
1シグナル = 1注文を基本とするが、将来の分割発注に備えて FK 設計にする。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trade_app.models.database import Base
from trade_app.models.enums import OrderStatus


class Order(Base):
    """注文テーブル"""

    __tablename__ = "orders"

    # ─── 主キー ────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 紐付けシグナル ────────────────────────────────────────────────────
    # exit注文（is_exit_order=True）の場合は NULL
    signal_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("trade_signals.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # ─── 紐付けポジション（exit注文のみ使用）──────────────────────────────
    # ExitWatcher が発行する決済注文でのみ設定される
    position_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("positions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # ─── exit注文フラグ ────────────────────────────────────────────────────
    # True の場合、OrderPoller は約定後にポジションクローズ処理を行う
    is_exit_order: Mapped[bool] = mapped_column(
        nullable=False, default=False, index=True
    )

    # ─── ブローカー発行の注文ID ────────────────────────────────────────────
    # ブローカーから返却された注文番号。約定照会・キャンセルに使用
    # 発注前は NULL、発注後に設定
    broker_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )

    # ─── 注文内容 ─────────────────────────────────────────────────────────
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ─── 状態管理 ─────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OrderStatus.PENDING, index=True
    )

    # ─── 約定情報（約定後に設定）──────────────────────────────────────────
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ─── タイムスタンプ ────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    filled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # キャンセル要求を broker に送信した時刻（NULL = 未要求）
    # ExitWatcher 等が cancel_order() を呼ぶ際に設定し二重キャンセルを防ぐ
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # ─── リレーション ─────────────────────────────────────────────────────
    # positions は Position.order_id 経由でアクセス（lazy load）
    # foreign_keys 指定: Order.position_id（exit FK）との曖昧さを解消
    positions: Mapped[list["Position"]] = relationship(  # type: ignore[name-defined]
        "Position",
        back_populates="order",
        foreign_keys="Position.order_id",
        lazy="select",
    )

    # ─── インデックス ──────────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_orders_ticker_status", "ticker", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id[:8]} ticker={self.ticker} "
            f"side={self.side} status={self.status}>"
        )
