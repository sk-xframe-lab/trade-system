"""
OrderStateTransition モデル
注文のステータス変化を時系列で全件記録する。
「いつ・誰が・なぜ」ステータスを変えたかが完全に追跡できる。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from trade_app.models.database import Base


class OrderStateTransition(Base):
    """注文状態遷移履歴テーブル（APPEND ONLY）"""

    __tablename__ = "order_state_transitions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 対象注文
    order_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 遷移前のステータス（初回作成時は NULL）
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # 遷移後のステータス
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)

    # 遷移理由（人が読める説明）
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 遷移を引き起こしたコンポーネント
    # "pipeline"  : SignalPipeline（初回発注）
    # "poller"    : OrderPoller（約定・キャンセル検出）
    # "recovery"  : RecoveryManager（起動時リカバリ）
    # "manual"    : 手動操作
    triggered_by: Mapped[str] = mapped_column(
        String(16), nullable=False, default="system"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_order_transitions_order_created", "order_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<OrderStateTransition order={self.order_id[:8]} "
            f"{self.from_status}→{self.to_status} by={self.triggered_by}>"
        )


# ─── ユーティリティ関数 ───────────────────────────────────────────────────────

async def record_transition(
    db,
    order_id: str,
    from_status: str | None,
    to_status: str,
    reason: str = "",
    triggered_by: str = "system",
) -> None:
    """
    注文状態遷移を1件記録する（ユーティリティ関数）。

    DB の flush は呼び出し元が責任を持つ。
    この関数は add のみ行い commit はしない。

    Args:
        db          : AsyncSession
        order_id    : 対象注文の UUID 文字列
        from_status : 遷移前ステータス（初回作成時は None）
        to_status   : 遷移後ステータス
        reason      : 遷移理由（ログ・デバッグ用）
        triggered_by: 遷移を起こしたコンポーネント名
    """
    transition = OrderStateTransition(
        order_id=order_id,
        from_status=from_status,
        to_status=to_status,
        reason=reason or "",
        triggered_by=triggered_by,
        created_at=datetime.now(timezone.utc),
    )
    db.add(transition)
