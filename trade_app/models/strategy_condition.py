"""
StrategyCondition モデル
strategy が参照する state 条件を管理するテーブル。

condition_type:
  - required_state  : 指定 state が active であれば条件成立
  - forbidden_state : 指定 state が active であれば entry を禁止
  - size_modifier   : 指定 state が active であれば size_modifier を適用

Phase 1 では operator = "exists" のみ使用。
GTE/LTE 等はスコア・信頼度の閾値比較向け（Phase 2 以降）。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class StrategyCondition(Base):
    """strategy 条件テーブル（strategy_conditions）"""

    __tablename__ = "strategy_conditions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 所属 strategy ────────────────────────────────────────────────────
    strategy_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("strategy_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ─── 条件定義 ──────────────────────────────────────────────────────────
    # "required_state" | "forbidden_state" | "size_modifier"
    condition_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 対象 layer: "market" | "symbol" | "time_window"
    layer: Mapped[str] = mapped_column(String(32), nullable=False)
    # 評価対象の state_code（例: "trend_up", "morning_trend_zone"）
    state_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # 比較演算子（Phase 1 は "exists" 固定）
    operator: Mapped[str] = mapped_column(String(16), nullable=False, default="exists")
    # gte/lte 用閾値（Phase 2 以降）
    threshold_value: Mapped[float | None] = mapped_column(Float(), nullable=True)
    # size_modifier 条件の縮小率（例: 0.5 = 50%）
    size_modifier: Mapped[float | None] = mapped_column(Float(), nullable=True)
    # 条件についての補足メモ
    notes: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # ─── タイムスタンプ ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyCondition strategy_id={self.strategy_id} "
            f"type={self.condition_type} layer={self.layer} state={self.state_code}>"
        )
