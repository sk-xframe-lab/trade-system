"""
Alembic admin_db マイグレーション設定

ADMIN_DATABASE_URL_SYNC 環境変数（psycopg2）を使用して同期マイグレーションを実行する。

【trade_db alembic との違い】
  - alembic/ → trade_db (alembic.ini)      → Base.metadata (trading tables)
  - alembic_admin/ → admin_db (alembic_admin.ini) → AdminBase.metadata (admin UI tables)

管理対象テーブル:
  ui_users, ui_sessions, ui_audit_logs, symbol_configs, notification_configs

注意: admin_db の migration バージョン管理には専用テーブル alembic_version_admin を使用する。
      Phase 1 は同一 PostgreSQL DB を使用するため、trade_db の alembic_version テーブルと
      衝突しないよう version_table="alembic_version_admin" を context.configure() に指定する。
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# パスを通す（trade_app モジュールを import できるようにする）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# AdminBase と全管理画面モデルを import してマイグレーション対象に含める
from trade_app.admin.database import AdminBase  # noqa: F401, E402
from trade_app.admin.models import (  # noqa: F401, E402
    UiUser,
    UiSession,
    UiAuditLog,
    SymbolConfig,
    NotificationConfig,
)

config = context.config

# 環境変数から admin_db 同期 URL を上書き（Docker 環境での接続文字列を優先）
admin_db_url = os.environ.get("ADMIN_DATABASE_URL_SYNC")
if admin_db_url:
    config.set_main_option("sqlalchemy.url", admin_db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# admin_db の metadata を使用（trade_db の Base.metadata とは分離）
target_metadata = AdminBase.metadata


def run_migrations_offline() -> None:
    """オフラインモード（接続なし）でマイグレーションを実行"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Phase 1 で trade_db と同一 PostgreSQL DB を使用する場合、
        # alembic_version テーブルが衝突しないよう admin_db 専用のテーブル名を使用する。
        version_table="alembic_version_admin",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """オンラインモード（実DB接続）でマイグレーションを実行"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # Phase 1 で trade_db と同一 PostgreSQL DB を使用する場合、
            # alembic_version テーブルが衝突しないよう admin_db 専用のテーブル名を使用する。
            version_table="alembic_version_admin",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
