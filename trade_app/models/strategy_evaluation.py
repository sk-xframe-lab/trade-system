"""
StrategyEvaluation モデル
StrategyEngine が生成する判定ログ（APPEND ONLY の時系列テーブル）。

説明可能性のため、matched/missing/blocking の全情報を JSON カラムで保存する。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class StrategyEvaluation(Base):
    """strategy 判定ログ（strategy_evaluations テーブル）"""

    __tablename__ = "strategy_evaluations"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 対象 ─────────────────────────────────────────────────────────────
    # strategy_definitions.id への論理参照（FK 制約なし: ログテーブル）
    strategy_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    # 銘柄コード。None = 銘柄横断評価（market + time_window のみ）
    ticker: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ─── 判定時刻 ─────────────────────────────────────────────────────────
    evaluation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ─── 判定結果 ─────────────────────────────────────────────────────────
    # entry_allowed と同義（将来の拡張用に分離）
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    # True = エントリー許可, False = ブロック
    entry_allowed: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    # 適用後のポジションサイズ比率（0.0〜1.0）
    size_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)

    # ─── 説明可能性 ───────────────────────────────────────────────────────
    matched_required_states_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    matched_forbidden_states_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    missing_required_states_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    blocking_reasons_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # ─── タイムスタンプ ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyEvaluation strategy_id={self.strategy_id} "
            f"ticker={self.ticker} entry_allowed={self.entry_allowed} "
            f"time={self.evaluation_time}>"
        )
