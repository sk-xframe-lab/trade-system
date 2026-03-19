"""
Position モデル
約定済み注文から生成されるポジション（建玉）の管理。
TP/SL/時間切れによるクローズはExitWatcher（Phase 3）が担当する。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trade_app.models.database import Base
from trade_app.models.enums import ExitReason, PositionStatus


class Position(Base):
    """ポジションテーブル（建玉管理）"""

    __tablename__ = "positions"

    # ─── 主キー ────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 紐付け注文 ────────────────────────────────────────────────────────
    order_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,   # 1注文 = 1ポジション（現フェーズ）
        index=True,
    )

    # ─── ポジション内容 ────────────────────────────────────────────────────
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)

    # ─── 現在値（ExitWatcher が定期更新）────────────────────────────────────
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ─── 出口条件（分析システムが設定したTP/SL・時間制限）──────────────────
    tp_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # この時刻を過ぎたら強制クローズ（例: 当日14:50）
    exit_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ─── 決済残数量（CLOSING 中に Execution ごとに減算）────────────────────────
    # initiate_exit() 時に quantity で初期化し、apply_exit_execution() で減算する
    # remaining_qty == 0 になった時点で finalize_exit() を呼び出す
    remaining_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ─── 評価損益（ExitWatcher が更新）──────────────────────────────────────
    # (current_price - entry_price) * quantity（買い/売りの符号を考慮）
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ─── 状態管理 ─────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PositionStatus.OPEN, index=True
    )

    # ─── クローズ情報（クローズ後に設定）─────────────────────────────────────
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 確定損益（exit_price - entry_price）* quantity
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ─── タイムスタンプ ────────────────────────────────────────────────────
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # ─── リレーション ─────────────────────────────────────────────────────
    # foreign_keys 指定: Order.position_id（exit FK）との曖昧さを解消
    order: Mapped["Order"] = relationship(  # type: ignore[name-defined]
        "Order",
        back_populates="positions",
        foreign_keys="Position.order_id",
    )

    # ─── インデックス ──────────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_positions_ticker_status", "ticker", "status"),
        Index("ix_positions_status_deadline", "status", "exit_deadline"),
    )

    def calc_unrealized_pnl(self, current_price: float) -> float:
        """
        現在値を元に評価損益を計算する。
        買いポジション: (現在値 - 取得単価) × 数量
        売りポジション: (取得単価 - 現在値) × 数量
        """
        if self.side == "buy":
            return (current_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - current_price) * self.quantity

    def __repr__(self) -> str:
        return (
            f"<Position id={self.id[:8]} ticker={self.ticker} "
            f"side={self.side} qty={self.quantity} status={self.status}>"
        )
