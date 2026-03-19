"""
UiUser モデル — 管理画面ユーザー

仕様書: 管理画面仕様書 v0.3 §7(DBエンティティ案 > ui_users)

【migration】
alembic_admin/ チェーンで管理。alembic_admin/versions/001_admin_initial.py を参照。

【Phase 1 ロール運用】
role カラムは admin / operator / viewer を定義するが、
Phase 1 では全ユーザーを admin として扱い、UI 分岐を行わない。

【認証】
Google OAuth (SSO) + TOTP (2FA)。
totp_secret_encrypted は I-3 (暗号化方式) 確定後に暗号化実装を追加する。
現在は文字列カラムとして定義のみ。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.admin.database import AdminBase


class UiUser(AdminBase):
    """管理画面ユーザーテーブル"""

    __tablename__ = "ui_users"

    # ─── 主キー ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── Google OAuth 情報 ───────────────────────────────────────────────────
    # Google OAuthのメールアドレス。変更不可。
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ─── ロール ───────────────────────────────────────────────────────────────
    # AdminRole: "admin" / "operator" / "viewer"
    # Phase 1 では全員 admin として扱う。UI 分岐は Phase 2 以降。
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="admin")

    # ─── アカウント状態 ───────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ─── 2FA (TOTP) ──────────────────────────────────────────────────────────
    # TODO(I-3): 暗号化方式確定後に暗号化実装を追加すること。
    # 現在はプレーンな文字列カラムとして定義のみ。
    # 本番運用では必ず暗号化すること。
    totp_secret_encrypted: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    # TOTP が設定済みかどうか（秘密鍵を返さずに確認するためのフラグ）
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ─── タイムスタンプ ───────────────────────────────────────────────────────
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
        Index("ix_ui_users_email", "email"),
        Index("ix_ui_users_role_active", "role", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<UiUser email={self.email} role={self.role} active={self.is_active}>"
