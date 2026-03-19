"""
StrategyDefinition モデル
strategy の有効/無効・方向・優先度・最大サイズ比率を管理するマスタテーブル。

設計制約:
  Strategy Engine は発注しない。判定結果（StrategyDecisionResult）を返すのみ。
  BrokerAdapter / OrderRouter / PositionManager への依存は一切持たない。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class StrategyDefinition(Base):
    """strategy 定義マスタ（strategy_definitions テーブル）"""

    __tablename__ = "strategy_definitions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 識別 ─────────────────────────────────────────────────────────────
    strategy_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    strategy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # ─── 制御パラメータ ───────────────────────────────────────────────────
    # "long" | "short" | "both"
    direction: Mapped[str] = mapped_column(String(16), nullable=False, default="both")
    # 優先度: 数値が高いほど優先（将来の競合解決で使用）
    priority: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    # False の場合 entry_allowed は常に False
    is_enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    # 最大ポジションサイズ比率（1.0 = 100%）。size_modifier と組み合わせて使用。
    max_size_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=1.0)

    # ─── タイムスタンプ ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyDefinition code={self.strategy_code} "
            f"enabled={self.is_enabled} direction={self.direction}>"
        )
