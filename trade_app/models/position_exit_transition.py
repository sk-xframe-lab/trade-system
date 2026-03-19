"""
PositionExitTransition モデル
ポジションのクローズ状態遷移を記録する（APPEND ONLY）。

OPEN → CLOSING → CLOSED の遷移を全て記録し、
partial exit / full exit のどちらでも追跡可能にする。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


def _record_exit_transition_sync(db_sync, position_id: str, from_status: str | None,
                                  to_status: str, exit_reason: str | None = None,
                                  triggered_by: str = "system",
                                  exit_order_id: str | None = None,
                                  details: dict | None = None) -> "PositionExitTransition":
    """同期版: テスト用ヘルパー"""
    t = PositionExitTransition(
        position_id=position_id,
        from_status=from_status,
        to_status=to_status,
        exit_reason=exit_reason,
        triggered_by=triggered_by,
        exit_order_id=exit_order_id,
        details=details,
        created_at=datetime.now(timezone.utc),
    )
    db_sync.add(t)
    return t


async def record_exit_transition(
    db,
    position_id: str,
    from_status: str | None,
    to_status: str,
    exit_reason: str | None = None,
    triggered_by: str = "system",
    exit_order_id: str | None = None,
    details: dict | None = None,
) -> "PositionExitTransition":
    """
    ポジション状態遷移を DB に記録する。
    flush は呼ぶが commit は呼ばない（呼び出し元が管理する）。
    """
    t = PositionExitTransition(
        position_id=position_id,
        from_status=from_status,
        to_status=to_status,
        exit_reason=exit_reason,
        triggered_by=triggered_by,
        exit_order_id=exit_order_id,
        details=details,
        created_at=datetime.now(timezone.utc),
    )
    db.add(t)
    await db.flush()
    return t


class PositionExitTransition(Base):
    """ポジション出口状態遷移テーブル（APPEND ONLY）"""

    __tablename__ = "position_exit_transitions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 対象ポジション
    position_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("positions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 遷移前後の状態
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)

    # exit理由（tp_hit / sl_hit / timeout / manual / signal）
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 遷移を起こしたコンポーネント（watcher / poller / manual / system）
    triggered_by: Mapped[str] = mapped_column(
        String(32), nullable=False, default="system"
    )

    # この遷移に対応する exit 注文 ID（CLOSING 時に設定）
    exit_order_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), nullable=True
    )

    # 補足情報
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index(
            "ix_position_exit_transitions_position_created",
            "position_id",
            "created_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<PositionExitTransition pos={self.position_id[:8]} "
            f"{self.from_status}→{self.to_status} reason={self.exit_reason}>"
        )
