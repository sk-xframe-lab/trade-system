"""
Alembic マイグレーション設定
DATABASE_URL_SYNC 環境変数（psycopg2）を使用して同期マイグレーションを実行する。
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# パスを通す（trade_app モジュールを import できるようにする）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 全モデルを import してマイグレーション対象に含める（必ず全モデルをimportすること）
from trade_app.models.database import Base  # noqa: F401, E402
from trade_app.models import signal, order, position, trade_result, audit_log  # noqa: F401, E402
from trade_app.models import (  # noqa: F401, E402
    execution,
    broker_request,
    broker_response,
    system_event,
    order_state_transition,
)
# Phase 3 追加モデル
from trade_app.models import trading_halt, position_exit_transition  # noqa: F401, E402
# Phase 4 Market State Engine モデル
from trade_app.models import state_definition, state_evaluation, current_state_snapshot  # noqa: F401, E402
# Phase AT Daily Metrics モデル
from trade_app.models import daily_price_history  # noqa: F401, E402

config = context.config

# 環境変数から同期 DB URL を上書き（Docker 環境での接続文字列を優先）
db_url = os.environ.get("DATABASE_URL_SYNC")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """オフラインモード（接続なし）でマイグレーションを実行"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
            # タイムスタンプカラムの精度を PostgreSQL に合わせる
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
