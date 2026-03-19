"""
CurrentStateSnapshot モデル
layer × target ごとの現在状態スナップショット（マテリアライズドビュー相当）。

MarketStateEngine が評価サイクルを実行するたびに UPSERT する。
取引ロジックはこのテーブルを参照することで現在状態を高速に取得できる。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class CurrentStateSnapshot(Base):
    """現在状態スナップショット（current_state_snapshots テーブル）"""

    __tablename__ = "current_state_snapshots"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 対象 ─────────────────────────────────────────────────────────────
    layer: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ─── 状態データ ───────────────────────────────────────────────────────
    # アクティブな状態コードのリスト
    # 例: ["morning_trend_zone"]
    active_states_json: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    # 状態のサマリー情報
    # 例: {"primary_state": "morning_trend_zone", "block_entry": false, "evaluated_at": "..."}
    state_summary_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )

    # ─── タイムスタンプ ───────────────────────────────────────────────────
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<CurrentStateSnapshot layer={self.layer} target={self.target_code} "
            f"states={self.active_states_json} updated={self.updated_at}>"
        )
