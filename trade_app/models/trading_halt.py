"""
TradingHalt モデル
取引停止状態をDBで管理する。halt 状態の正本は必ずこのテーブル。

停止理由:
  - daily_loss       : 日次損失上限到達
  - consecutive_losses: 連続損失閾値超過
  - manual           : API 経由の手動停止

is_active=True のレコードが1件でも存在する場合、新規発注を全て拒否する。
停止解除は deactivated_at / deactivated_by を設定して is_active=False にする。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base
from trade_app.models.enums import HaltType


class TradingHalt(Base):
    """取引停止状態テーブル"""

    __tablename__ = "trading_halts"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 停止種別
    halt_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # 停止理由（人間可読）
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # 現在アクティブかどうか（True = 停止中）
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )

    # 発動・解除タイムスタンプ
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 操作者（"system" または user ID/名）
    activated_by: Mapped[str] = mapped_column(
        String(32), nullable=False, default="system"
    )
    deactivated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 補足情報（損失額・連続損失数など）
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_trading_halts_type_active", "halt_type", "is_active"),
    )

    def __repr__(self) -> str:
        return (
            f"<TradingHalt id={self.id[:8]} type={self.halt_type} "
            f"active={self.is_active} reason={self.reason[:40]!r}>"
        )
