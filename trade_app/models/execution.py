"""
Execution モデル
ブローカーから報告された約定イベントを1件ずつ記録する。
一部約定（Partial Fill）でも発生するため、1注文に対して複数件になりうる。
約定価格の加重平均は Order.filled_price が保持する。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class Execution(Base):
    """約定履歴テーブル（1約定イベント = 1レコード）"""

    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # 紐付け注文
    order_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # ブローカーが採番した約定 ID（重複防止・照合用）
    # 取得できない場合は NULL
    broker_execution_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )

    # 約定内容
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)   # この約定の数量
    price: Mapped[float] = mapped_column(Float, nullable=False)       # この約定の価格

    # ブローカーが返した約定時刻（取得できない場合は created_at で代用）
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = ()

    def __repr__(self) -> str:
        return (
            f"<Execution id={self.id[:8]} order={self.order_id[:8]} "
            f"qty={self.quantity} @ {self.price:.0f}>"
        )
