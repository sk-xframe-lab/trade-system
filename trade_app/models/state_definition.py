"""
StateDefinition モデル
Market State Engine の状態コード定義マスタ。

各状態コードの意味・重大度・発注への影響（block_entry, block_exit）を保持する。
データは起動時シード or 手動投入。評価エンジンはこのマスタを参照して
state_evaluations / current_state_snapshots を書き込む。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class StateDefinition(Base):
    """状態コード定義マスタ（state_definitions テーブル）"""

    __tablename__ = "state_definitions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 識別子 ───────────────────────────────────────────────────────────
    state_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    state_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # ─── 分類 ──────────────────────────────────────────────────────────────
    layer: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")

    # ─── 発注影響 ─────────────────────────────────────────────────────────
    # True の場合、この状態が active なら新規エントリーを禁止すること（発注ロジック側で参照）
    block_entry: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    # True の場合、exit 発注も禁止（極端な場合のみ）
    block_exit: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    # 0.0〜1.0: この状態が active なら通常サイズ × ratio に縮小すること（None = 縮小なし）
    reduce_size_ratio: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # ─── 管理 ─────────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    def __repr__(self) -> str:
        return (
            f"<StateDefinition code={self.state_code} layer={self.layer} "
            f"severity={self.severity} block_entry={self.block_entry}>"
        )
