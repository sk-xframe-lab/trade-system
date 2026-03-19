"""
StateEvaluation モデル
Market State Engine が生成する評価ログ（APPEND ONLY の時系列テーブル）。

1回の評価サイクルで複数の state_code が生成されうる（例: time_window×1 + market×1 = 2行）。
evidence_json には判定根拠（使用した価格・ルール条件など）を必ず保存する。
"""
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class StateEvaluation(Base):
    """状態評価ログ（state_evaluations テーブル）"""

    __tablename__ = "state_evaluations"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 評価対象 ─────────────────────────────────────────────────────────
    layer: Mapped[str] = mapped_column(String(32), nullable=False)
    # "market" | "index" | "symbol" | "time_window" など拡張可
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 銘柄コードや指数コード。market / time_window 等グローバルな場合は None
    target_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ─── 評価結果 ─────────────────────────────────────────────────────────
    evaluation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # 0.0〜1.0: 状態の強度（例: trend_up=0.8 は強いトレンド）
    score: Mapped[float] = mapped_column(Float(), nullable=False, default=1.0)
    # 0.0〜1.0: この評価の信頼度（データ品質・ルール確信度）
    confidence: Mapped[float] = mapped_column(Float(), nullable=False, default=1.0)
    # False になると「過去に有効だった評価」として履歴保持
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)

    # ─── エビデンス ───────────────────────────────────────────────────────
    # 判定根拠を必ず保存する。例: {"rule": "09:00〜09:15", "current_time": "09:07:30"}
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    # この評価の有効期限（None の場合は次の評価サイクルまで有効）
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ─── タイムスタンプ ───────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<StateEvaluation layer={self.layer} target={self.target_code} "
            f"state={self.state_code} time={self.evaluation_time}>"
        )
