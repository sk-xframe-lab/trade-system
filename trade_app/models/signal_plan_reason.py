"""
SignalPlanReason モデル
Planning 各 stage での縮小・拒否理由の履歴（APPEND ONLY）。

SignalPlanningService が縮小または拒否を行うたびに INSERT する。
1 つの signal_plan に対して複数の理由レコードが存在しうる（段階ごと）。

読み取り用途:
  - 監査・デバッグ目的

書き込み用途:
  - SignalPlanningService.plan() のみ
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class SignalPlanReason(Base):
    """planning 段階ごとの縮小・拒否理由（signal_plan_reasons テーブル）"""

    __tablename__ = "signal_plan_reasons"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 対象 ─────────────────────────────────────────────────────────────
    # signal_plans.id への論理参照（FK 制約なし: 監査ログテーブル）
    signal_plan_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # ─── 理由 ─────────────────────────────────────────────────────────────
    # PlanningReasonCode enum の値
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_detail: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # ─── 調整詳細 ─────────────────────────────────────────────────────────
    # その stage での入力スナップショット（quantity, ratio など）
    input_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 調整前後の値（quantity など）
    adjustment_before: Mapped[float | None] = mapped_column(Float(), nullable=True)
    adjustment_after: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # ─── タイムスタンプ ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<SignalPlanReason plan_id={self.signal_plan_id[:8]} "
            f"code={self.reason_code}>"
        )
