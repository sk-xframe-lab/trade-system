"""
TradeResult モデル
クローズしたポジションの確定損益記録。
ポジション履歴の集計・パフォーマンス分析に使用する。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class TradeResult(Base):
    """確定損益テーブル（クローズしたポジションの記録）"""

    __tablename__ = "trade_results"

    # ─── 主キー ────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 紐付けポジション ──────────────────────────────────────────────────
    position_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("positions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,   # ポジション1件に対して結果は1件
        index=True,
    )

    # ─── 取引内容 ─────────────────────────────────────────────────────────
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)

    # ─── 損益 ─────────────────────────────────────────────────────────────
    pnl: Mapped[float] = mapped_column(Float, nullable=False)         # 円損益
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=False)     # 損益率(%)

    # ─── 保有情報 ─────────────────────────────────────────────────────────
    holding_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ─── タイムスタンプ ────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # ─── インデックス ──────────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_trade_results_ticker_created", "ticker", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<TradeResult id={self.id[:8]} ticker={self.ticker} "
            f"pnl={self.pnl:.0f}円 reason={self.exit_reason}>"
        )
