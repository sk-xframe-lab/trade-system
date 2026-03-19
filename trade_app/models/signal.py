"""
TradeSignal モデル
分析システムから受信した売買シグナルの永続化レコード。
受信した全シグナルは必ずここに保存し、処理可否に関わらず追跡可能にする。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base
from trade_app.models.enums import OrderType, Side, SignalStatus


class TradeSignal(Base):
    """受信シグナルテーブル"""

    __tablename__ = "trade_signals"

    # ─── 主キー ────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 冪等性 ────────────────────────────────────────────────────────────
    # 送信側が生成したUUID。同一キーの2回目以降は409で返す
    idempotency_key: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )

    # ─── 送信元メタ情報 ────────────────────────────────────────────────────
    # X-Source-System ヘッダーの値（例: "stock-analysis-v1"）
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)

    # ─── シグナル本体 ──────────────────────────────────────────────────────
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_type: Mapped[str] = mapped_column(
        String(16), nullable=False  # "entry" or "exit"
    )
    order_type: Mapped[str] = mapped_column(
        String(16), nullable=False  # OrderType enum 値
    )
    side: Mapped[str] = mapped_column(
        String(8), nullable=False   # Side enum 値
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ─── 参考情報 ─────────────────────────────────────────────────────────
    # 分析システムが生成した戦略名・スコア（リスク判断の参考のみ）
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ─── タイムスタンプ ────────────────────────────────────────────────────
    # 分析システムがシグナルを生成した時刻（送信側が設定）
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 自動売買システムが受信した時刻（自動設定）
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # ─── 状態管理 ─────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SignalStatus.RECEIVED,
        index=True,
    )
    # リスクチェック拒否・失敗時の理由
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ─── 任意の追加情報 ────────────────────────────────────────────────────
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ─── インデックス ──────────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_trade_signals_ticker_received", "ticker", "received_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<TradeSignal id={self.id[:8]} ticker={self.ticker} "
            f"side={self.side} status={self.status}>"
        )
