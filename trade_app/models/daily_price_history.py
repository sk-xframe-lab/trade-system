"""
DailyPriceHistory モデル

銘柄ごとの日次 OHLCV データを保存する。
MA5 / MA20 / ATR14 / RSI14 はアプリ側（DailyMetricsComputer）で計算するため
raw OHLCV のみを格納する。

設計制約:
  - UNIQUE (ticker, trading_date): 同一銘柄・同一取引日は 1 行のみ
  - close は NOT NULL（計算に必須）
  - high / low / open / volume は NULL 許容（データソースによって欠損あり）
  - source: "j_quants" / "tachibana" / "manual" など投入元を記録
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class DailyPriceHistory(Base):
    """日次 OHLCV データ（daily_price_history テーブル）"""

    __tablename__ = "daily_price_history"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 識別 ─────────────────────────────────────────────────────────────
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date(), nullable=False)

    # ─── OHLCV ────────────────────────────────────────────────────────────
    open: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    close: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)

    # ─── メタ ─────────────────────────────────────────────────────────────
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<DailyPriceHistory ticker={self.ticker} date={self.trading_date} close={self.close}>"
        )
