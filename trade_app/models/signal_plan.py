"""
SignalPlan モデル
Signal ごとの Planning Layer 実行結果（APPEND ONLY）。

SignalPlanningService.plan() が Signal を評価するたびに INSERT する。
縮小・拒否理由の詳細は signal_plan_reasons テーブルを参照。

読み取り用途:
  - OrderRouter / RiskManager への planned_order_qty 連携
  - 監査・デバッグ目的

書き込み用途:
  - SignalPlanningService.plan() のみ（SignalPipeline 経由）
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class SignalPlan(Base):
    """signal ごとの planning 結果（signal_plans テーブル）"""

    __tablename__ = "signal_plans"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 対象 ─────────────────────────────────────────────────────────────
    # trade_signals.id への論理参照（FK 制約なし: 監査ログテーブル）
    signal_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # signal_strategy_decisions.id への論理参照（nullable: exit bypass 時）
    signal_strategy_decision_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )

    # ─── 判定結果 ─────────────────────────────────────────────────────────
    # "accepted" / "reduced" / "rejected"
    planning_status: Mapped[str] = mapped_column(String(16), nullable=False)
    # 計画発注数量（lot 丸め済み）。rejected 時は 0
    planned_order_qty: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    # 計画発注想定金額（planned_order_qty × limit_price）
    planned_notional: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # ─── 執行パラメータ候補 ───────────────────────────────────────────────
    order_type_candidate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    limit_price: Mapped[float | None] = mapped_column(Float(), nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float(), nullable=True)
    max_slippage_bps: Mapped[float | None] = mapped_column(Float(), nullable=True)
    participation_rate_cap: Mapped[float | None] = mapped_column(Float(), nullable=True)
    entry_timeout_seconds: Mapped[int | None] = mapped_column(Integer(), nullable=True)

    # ─── 計画詳細 ─────────────────────────────────────────────────────────
    # strategy gate から受け取った size_ratio（縮小前の比率）
    applied_size_ratio: Mapped[float | None] = mapped_column(Float(), nullable=True)
    # 拒否時の最終理由コード
    rejection_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 各 stage の詳細トレース
    planning_trace_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # ─── タイムスタンプ ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<SignalPlan signal_id={self.signal_id[:8]} "
            f"status={self.planning_status} qty={self.planned_order_qty}>"
        )
