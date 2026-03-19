"""
NotificationConfig モデル — 通知設定

仕様書: 管理画面仕様書 v0.3 §3(SCR-09), §7(DBエンティティ案 > notification_configs)

【migration】
alembic_admin/ チェーンで管理。alembic_admin/versions/001_admin_initial.py を参照。

【events_json の保存方針】
- 定義済みイベントコード (NotificationEventCode) の配列のみ保存する。
- 自由文字列は不可。
- 例: ["ORDER_FILLED", "HALT_TRIGGERED", "BROKER_DISCONNECTED"]
- バリデーションは NotificationConfigService が担当する。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.admin.database import AdminBase


class NotificationConfig(AdminBase):
    """通知設定テーブル"""

    __tablename__ = "notification_configs"

    # ─── 主キー ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 通知先 ───────────────────────────────────────────────────────────────
    # NotificationChannelType: "email" / "telegram"
    channel_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # メールアドレス または Telegram チャットID
    destination: Mapped[str] = mapped_column(String(256), nullable=False)

    # ─── 有効フラグ ───────────────────────────────────────────────────────────
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ─── 通知対象イベント ─────────────────────────────────────────────────────
    # 定義済み NotificationEventCode の配列。自由文字列不可。
    # 例: ["ORDER_FILLED", "HALT_TRIGGERED"]
    events_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # ─── 操作者（監査用） ─────────────────────────────────────────────────────
    # TODO(I-4): admin_db 内完結の FK（→ ui_users.id）。技術的に Phase 1 追加可能だが
    # 実ユーザーが存在しない I-4（OAuth）完了前に制約を追加すると新規作成が失敗する。
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # ─── タイムスタンプ ───────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_notification_configs_enabled", "is_enabled"),
        Index("ix_notification_configs_channel", "channel_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationConfig channel={self.channel_type} "
            f"dest={self.destination!r} enabled={self.is_enabled}>"
        )
