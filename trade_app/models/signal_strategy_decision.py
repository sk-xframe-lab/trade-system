"""
SignalStrategyDecision モデル
Signal ごとの Strategy Gate 判定結果監査テーブル（APPEND ONLY）。

SignalStrategyGate.check() が Signal を評価するたびに INSERT する。
strategy_gate_rejected 時の追跡・監査に使用。

読み取り用途:
  - GET /api/signals/{signal_id}/strategy-decision で返す
  - 監査・デバッグ目的

書き込み用途:
  - SignalStrategyGate.check() のみ（SignalPipeline 経由）
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class SignalStrategyDecision(Base):
    """signal ごとの strategy gate 判定結果（signal_strategy_decisions テーブル）"""

    __tablename__ = "signal_strategy_decisions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 対象 ─────────────────────────────────────────────────────────────
    # trade_signals.id への論理参照（FK 制約なし: 監査ログテーブル）
    signal_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    # "long" または "short"（signal.side から導出: buy→long, sell→short）
    signal_direction: Mapped[str] = mapped_column(String(8), nullable=False)

    # ─── 参照した decision ────────────────────────────────────────────────
    # 参照した global decision の id（nullable: direction 不一致 or 存在しない場合）
    global_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 参照した ticker decision の id（nullable: direction 不一致 or 存在しない場合）
    symbol_decision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # ─── Gate 判定時刻 ────────────────────────────────────────────────────
    decision_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # ─── Gate 結果 ────────────────────────────────────────────────────────
    entry_allowed: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    size_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)

    # ─── 説明可能性 ───────────────────────────────────────────────────────
    blocking_reasons_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # ─── タイムスタンプ ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<SignalStrategyDecision signal_id={self.signal_id[:8]} "
            f"ticker={self.ticker} entry_allowed={self.entry_allowed} "
            f"time={self.decision_time}>"
        )
