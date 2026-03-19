"""管理画面 DB 初回スキーマ作成（全テーブル）

対象テーブル:
  - ui_users          : 管理画面ユーザー（Google OAuth + TOTP）
  - ui_sessions       : セッション管理（トークンハッシュ保存）
  - ui_audit_logs     : 管理画面操作ログ（APPEND ONLY）
  - symbol_configs    : 銘柄設定（取引パラメータ管理）
  - notification_configs : 通知設定（email / telegram）

【Phase 1 制約】
ADMIN_DATABASE_URL のデフォルト値は trade_db と同一 PostgreSQL を使用する。
このため alembic_version テーブルは trade_db と共用される。
Phase 2 で物理 DB 分離する場合は ADMIN_DATABASE_URL を別 DB に向けること。

【設計参照】
docs/admin/component_design.md §4 データモデル

Revision ID: a1b2c3d4e5f6
Revises: None
Create Date: 2026-03-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── ui_users ──────────────────────────────────────────────────────────────
    op.create_table(
        "ui_users",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="admin"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("totp_secret_encrypted", sa.String(512), nullable=True),
        sa.Column("totp_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("email", name="uq_ui_users_email"),
    )
    op.create_index("ix_ui_users_email", "ui_users", ["email"])
    op.create_index("ix_ui_users_role_active", "ui_users", ["role", "is_active"])

    # ── ui_sessions ───────────────────────────────────────────────────────────
    op.create_table(
        "ui_sessions",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("ui_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_token_hash", sa.String(256), nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column(
            "is_2fa_completed", sa.Boolean, nullable=False, server_default="false"
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("session_token_hash", name="uq_ui_sessions_token_hash"),
    )
    op.create_index("ix_ui_sessions_user_id", "ui_sessions", ["user_id"])
    op.create_index("ix_ui_sessions_token_hash", "ui_sessions", ["session_token_hash"])
    op.create_index("ix_ui_sessions_expires_at", "ui_sessions", ["expires_at"])

    # ── ui_audit_logs (APPEND ONLY) ───────────────────────────────────────────
    op.create_table(
        "ui_audit_logs",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("user_email", sa.String(254), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("resource_label", sa.String(128), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("before_json", sa.JSON, nullable=True),
        sa.Column("after_json", sa.JSON, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_ui_audit_logs_event_type", "ui_audit_logs", ["event_type"]
    )
    op.create_index(
        "ix_ui_audit_logs_created_at", "ui_audit_logs", ["created_at"]
    )
    op.create_index(
        "ix_ui_audit_logs_user_created", "ui_audit_logs", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_ui_audit_logs_event_created", "ui_audit_logs", ["event_type", "created_at"]
    )
    op.create_index(
        "ix_ui_audit_logs_resource",
        "ui_audit_logs",
        ["resource_type", "resource_id", "created_at"],
    )

    # ── symbol_configs ────────────────────────────────────────────────────────
    op.create_table(
        "symbol_configs",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("symbol_code", sa.String(8), nullable=False),
        sa.Column("symbol_name", sa.String(128), nullable=True),
        sa.Column("trade_type", sa.String(16), nullable=False),
        # strategy_id: FK なし（strategy_configs テーブルは O-4 確定待ち）
        sa.Column("strategy_id", sa.String(36), nullable=True),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("open_behavior", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("trading_start_time", sa.Time, nullable=True),
        sa.Column("trading_end_time", sa.Time, nullable=True),
        sa.Column("max_single_investment_jpy", sa.Integer, nullable=False),
        sa.Column("max_daily_investment_jpy", sa.Integer, nullable=False),
        sa.Column("take_profit_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("stop_loss_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("max_hold_minutes", sa.Integer, nullable=False),
        # created_by / updated_by: FK なし（ui_users.id への参照）
        # TODO(I-4): admin_db 内完結の FK。I-4（OAuth）完了後に migration で追加可能。
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("updated_by", sa.String(36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("symbol_code", name="uq_symbol_configs_code"),
    )
    op.create_index("ix_symbol_configs_code", "symbol_configs", ["symbol_code"])
    op.create_index("ix_symbol_configs_enabled", "symbol_configs", ["is_enabled"])
    op.create_index("ix_symbol_configs_trade_type", "symbol_configs", ["trade_type"])

    # ── notification_configs ──────────────────────────────────────────────────
    op.create_table(
        "notification_configs",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("channel_type", sa.String(16), nullable=False),
        sa.Column("destination", sa.String(256), nullable=False),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("events_json", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("updated_by", sa.String(36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_notification_configs_channel", "notification_configs", ["channel_type"]
    )
    op.create_index(
        "ix_notification_configs_enabled", "notification_configs", ["is_enabled"]
    )


def downgrade() -> None:
    op.drop_table("notification_configs")
    op.drop_table("symbol_configs")
    op.drop_table("ui_audit_logs")
    op.drop_table("ui_sessions")
    op.drop_table("ui_users")
