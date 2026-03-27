"""
pytest 共通フィクスチャ
インメモリ SQLite を使用してテスト用 DB を構築する。
Redis はモックに差し替える。
"""
import asyncio
from typing import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from trade_app.models.database import Base
from trade_app.admin.database import AdminBase

# 全モデルを明示的に import して Base.metadata / AdminBase.metadata に登録する
# これがないと create_all 時に一部テーブルが作られない
import trade_app.models.signal  # noqa: F401
import trade_app.models.order  # noqa: F401
import trade_app.models.position  # noqa: F401
import trade_app.models.trade_result  # noqa: F401
import trade_app.models.audit_log  # noqa: F401
import trade_app.models.execution  # noqa: F401
import trade_app.models.broker_request  # noqa: F401
import trade_app.models.broker_response  # noqa: F401
import trade_app.models.system_event  # noqa: F401
import trade_app.models.order_state_transition  # noqa: F401
import trade_app.models.trading_halt  # noqa: F401
import trade_app.models.position_exit_transition  # noqa: F401
# Phase 4 Market State Engine モデル
import trade_app.models.state_definition  # noqa: F401
import trade_app.models.state_evaluation  # noqa: F401
import trade_app.models.current_state_snapshot  # noqa: F401
# Phase 6 Strategy Engine モデル
import trade_app.models.strategy_definition  # noqa: F401
import trade_app.models.strategy_condition  # noqa: F401
import trade_app.models.strategy_evaluation  # noqa: F401
# Phase 7 Strategy Runner モデル
import trade_app.models.current_strategy_decision  # noqa: F401
# Phase 8 Signal Router Integration Gate モデル
import trade_app.models.signal_strategy_decision  # noqa: F401
# Phase 9 Signal Planning Layer モデル
import trade_app.models.signal_plan  # noqa: F401
import trade_app.models.signal_plan_reason  # noqa: F401
# Phase AT Daily Metrics モデル
import trade_app.models.daily_price_history  # noqa: F401
# Admin UI モデル（管理画面専用テーブル）
import trade_app.admin.models.ui_user  # noqa: F401
import trade_app.admin.models.ui_session  # noqa: F401
import trade_app.admin.models.ui_audit_log  # noqa: F401
import trade_app.admin.models.symbol_config  # noqa: F401
import trade_app.admin.models.notification_config  # noqa: F401


# ─── テスト用インメモリ DB ──────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="function")
async def db_engine():
    """テスト毎にインメモリ SQLite エンジンを生成する"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, checkfirst=True))
        # admin_db テーブル（AdminBase）も同一インメモリ DB に作成する
        # テストでは admin_db と trade_db を同一 SQLite DB で代用する
        await conn.run_sync(lambda c: AdminBase.metadata.create_all(c, checkfirst=True))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """テスト用 DB セッションを返す"""
    session_factory = async_sessionmaker(
        bind=db_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        yield session
        await session.rollback()


# ─── テスト用 Redis モック ──────────────────────────────────────────────────

@pytest.fixture
def mock_redis():
    """
    Redis クライアントのモック。
    インメモリ辞書で get/set/setex/delete/ping をシミュレートする。
    """
    _store: dict = {}

    mock = AsyncMock()

    async def _get(key):
        return _store.get(key)

    async def _setex(key, ttl, value):
        _store[key] = value.encode() if isinstance(value, str) else value

    async def _set(key, value, **kwargs):
        nx = kwargs.get("nx", False)
        if nx and key in _store:
            return None  # NX: 既存キーはセットしない
        _store[key] = value
        return True

    async def _delete(*keys):
        for k in keys:
            _store.pop(k, None)

    async def _ping():
        return True

    mock.get = _get
    mock.setex = _setex
    mock.set = _set
    mock.delete = _delete
    mock.ping = _ping

    return mock


# ─── pytest-asyncio 設定 ─────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()
