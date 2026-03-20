"""
CurrentStrategyDecision モデル
(strategy_id, ticker) ごとの最新 strategy 判定正本。

StrategyRunner が評価サイクルごとに UPSERT する。
strategy_evaluations（APPEND ONLY 時系列）とは別の正本テーブル。

読み取り用途:
  - 取引ロジックが現在の entry_allowed を高速に取得する
  - GET /api/v1/strategies/latest で返す

書き込み用途:
  - DecisionRepository.upsert_decisions() のみ（StrategyEngine 経由）
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class CurrentStrategyDecision(Base):
    """現在 strategy 判定正本（current_strategy_decisions テーブル）"""

    __tablename__ = "current_strategy_decisions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 対象 ─────────────────────────────────────────────────────────────
    # strategy_definitions.id への論理参照
    strategy_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    # 非正規化: strategy_code を重複保持（JOIN 不要にするため）
    strategy_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # 銘柄コード。None = 銘柄横断評価（market + time_window のみ）
    ticker: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ─── 判定結果 ─────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    entry_allowed: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    size_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)

    # ─── 説明可能性 ───────────────────────────────────────────────────────
    blocking_reasons_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    matched_required_states_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    missing_required_states_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    matched_forbidden_states_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # ─── タイムスタンプ ───────────────────────────────────────────────────
    # 元になった strategy_evaluations の evaluation_time
    evaluation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # このレコードが最後に upsert された時刻
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<CurrentStrategyDecision strategy_code={self.strategy_code} "
            f"ticker={self.ticker} entry_allowed={self.entry_allowed} "
            f"updated={self.updated_at}>"
        )
