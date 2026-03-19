"""
UiSession モデル — セッション管理

仕様書: 管理画面仕様書 v0.3 §7(DBエンティティ案 > ui_sessions)

【migration】
alembic_admin/ チェーンで管理。alembic_admin/versions/001_admin_initial.py を参照。

【セッションタイムアウト】
expires_at の値は I-5 (セッションタイムアウト時間) 確定まで未定。
アプリケーション層で設定値から計算して設定する。

【セッショントークン】
session_token_hash にはトークンのハッシュ値を保存する。
元のトークン値は保存しない。ハッシュアルゴリズムは実装時に確定すること。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trade_app.admin.database import AdminBase


class UiSession(AdminBase):
    """管理画面セッションテーブル"""

    __tablename__ = "ui_sessions"

    # ─── 主キー ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── ユーザー参照 ─────────────────────────────────────────────────────────
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ui_users.id"), nullable=False
    )

    # ─── セッショントークン ───────────────────────────────────────────────────
    # 元のトークン値は保存しない。ハッシュ値のみ保存。
    # ハッシュアルゴリズム: TODO(実装時に確定)
    session_token_hash: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)

    # ─── クライアント情報 ─────────────────────────────────────────────────────
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6 対応
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ─── 2FA 完了フラグ ──────────────────────────────────────────────────────
    # False の間は認証ガードを通過させない
    is_2fa_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # ─── 有効期限・無効化 ─────────────────────────────────────────────────────
    # TODO(I-5): セッションタイムアウト時間確定後に設定ロジックを実装
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 手動無効化時刻（ログアウト・強制無効化）
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # ─── リレーション ─────────────────────────────────────────────────────────
    user: Mapped["UiUser"] = relationship("UiUser", lazy="select")  # type: ignore[name-defined]

    __table_args__ = (
        Index("ix_ui_sessions_user_id", "user_id"),
        Index("ix_ui_sessions_token_hash", "session_token_hash"),
        Index("ix_ui_sessions_expires_at", "expires_at"),
    )

    @property
    def is_valid(self) -> bool:
        """セッションが有効か（期限内かつ無効化されていない）"""
        now = datetime.now(timezone.utc)
        return (
            self.invalidated_at is None
            and self.is_2fa_completed
            and self.expires_at > now
        )

    def __repr__(self) -> str:
        return (
            f"<UiSession user_id={self.user_id[:8]} "
            f"2fa={self.is_2fa_completed} valid={self.is_valid}>"
        )
