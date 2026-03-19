"""
AuditLog モデル
システムが行った全操作を時系列で記録する監査証跡テーブル。
削除・更新は行わず APPEND ONLY で運用する。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.models.database import Base


class AuditLog(Base):
    """監査ログテーブル（APPEND ONLY）"""

    __tablename__ = "audit_logs"

    # ─── 主キー ────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── イベント分類 ─────────────────────────────────────────────────────
    # AuditEventType enum 値（例: "signal_received", "order_filled"）
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # ─── 対象エンティティ ─────────────────────────────────────────────────
    # "signal", "order", "position" など
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 対象エンティティの UUID（trade_signals.id など）
    entity_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    # ─── アクター ─────────────────────────────────────────────────────────
    # "system" : 自動売買エンジン自身
    # "broker" : ブローカーからのコールバック
    # "admin"  : 手動操作
    actor: Mapped[str] = mapped_column(String(16), nullable=False, default="system")

    # ─── イベント詳細 ─────────────────────────────────────────────────────
    # 構造化データ（ticker, price, reason 等）を JSON で保存
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 人が読めるメッセージ（任意）
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ─── タイムスタンプ ────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    # ─── インデックス ──────────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_event_created", "event_type", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog event={self.event_type} "
            f"entity={self.entity_type}:{self.entity_id} at={self.created_at}>"
        )
