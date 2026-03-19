"""
ExitWatcher テスト

TP/SL/TimeStop での initiate_exit 呼び出し、CLOSING スキップ、
価格 None の挙動、二重 exit 防止などを検証する。
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from trade_app.models.enums import ExitReason, OrderStatus, PositionStatus
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.position_exit_transition import PositionExitTransition
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.exit_watcher import ExitWatcher
from trade_app.services.exit_policies import (
    StopLossPolicy,
    TakeProfitPolicy,
    TimeStopPolicy,
)


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_open_position(
    db_session,
    ticker: str = "7203",
    side: str = "buy",
    tp_price: float | None = None,
    sl_price: float | None = None,
    exit_deadline: datetime | None = None,
    qty: int = 100,
) -> Position:
    entry_order = Order(
        id=str(uuid.uuid4()),
        ticker=ticker,
        order_type="market",
        side=side,
        quantity=qty,
        status=OrderStatus.FILLED.value,
        filled_quantity=qty,
        filled_price=2500.0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(entry_order)

    position = Position(
        order_id=entry_order.id,
        ticker=ticker,
        side=side,
        quantity=qty,
        entry_price=2500.0,
        tp_price=tp_price,
        sl_price=sl_price,
        exit_deadline=exit_deadline,
        status=PositionStatus.OPEN.value,
        opened_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(position)
    return position


def _mock_broker(price: float | None = None):
    from trade_app.brokers.base import OrderStatusResponse
    broker = AsyncMock()
    broker.get_market_price = AsyncMock(return_value=price)
    broker.place_order = AsyncMock(return_value=OrderStatusResponse(
        broker_order_id=f"MOCK-{uuid.uuid4().hex[:8]}",
        status=OrderStatus.SUBMITTED,
    ))
    return broker


# ─── TP で initiate_exit ──────────────────────────────────────────────────────

class TestExitWatcherTP:

    @pytest.mark.asyncio
    async def test_tp_triggers_initiate_exit(self, db_session):
        """TP 価格到達で initiate_exit() が呼ばれ CLOSING に遷移すること"""
        position = _make_open_position(db_session, tp_price=3000.0)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=3000.0)

        watcher = ExitWatcher(policies=[TakeProfitPolicy()])
        await watcher._evaluate_position(db_session, position, broker, audit)

        assert position.status == PositionStatus.CLOSING.value
        assert position.exit_reason == ExitReason.TP_HIT.value

    @pytest.mark.asyncio
    async def test_tp_not_reached_keeps_open(self, db_session):
        """TP 価格未到達ではポジションが OPEN のまま"""
        position = _make_open_position(db_session, tp_price=3000.0)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=2999.0)

        watcher = ExitWatcher(policies=[TakeProfitPolicy()])
        await watcher._evaluate_position(db_session, position, broker, audit)

        assert position.status == PositionStatus.OPEN.value


# ─── SL で initiate_exit ──────────────────────────────────────────────────────

class TestExitWatcherSL:

    @pytest.mark.asyncio
    async def test_sl_triggers_initiate_exit(self, db_session):
        """SL 価格到達で initiate_exit() が呼ばれ CLOSING に遷移すること"""
        position = _make_open_position(db_session, sl_price=2000.0)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=2000.0)

        watcher = ExitWatcher(policies=[StopLossPolicy()])
        await watcher._evaluate_position(db_session, position, broker, audit)

        assert position.status == PositionStatus.CLOSING.value
        assert position.exit_reason == ExitReason.SL_HIT.value

    @pytest.mark.asyncio
    async def test_sl_not_reached_keeps_open(self, db_session):
        """SL 価格未到達ではポジションが OPEN のまま"""
        position = _make_open_position(db_session, sl_price=2000.0)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=2001.0)

        watcher = ExitWatcher(policies=[StopLossPolicy()])
        await watcher._evaluate_position(db_session, position, broker, audit)

        assert position.status == PositionStatus.OPEN.value


# ─── TimeStop で initiate_exit ────────────────────────────────────────────────

class TestExitWatcherTimeStop:

    @pytest.mark.asyncio
    async def test_timestop_triggers_initiate_exit(self, db_session):
        """deadline 超過で initiate_exit() が呼ばれ CLOSING に遷移すること"""
        deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        position = _make_open_position(db_session, exit_deadline=deadline)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=None)  # 価格不要

        watcher = ExitWatcher(policies=[TimeStopPolicy()])
        await watcher._evaluate_position(db_session, position, broker, audit)

        assert position.status == PositionStatus.CLOSING.value
        assert position.exit_reason == ExitReason.TIMEOUT.value

    @pytest.mark.asyncio
    async def test_timestop_fires_even_without_price(self, db_session):
        """価格が None でも deadline 超過なら exit が始まること"""
        deadline = datetime.now(timezone.utc) - timedelta(minutes=5)
        position = _make_open_position(db_session, exit_deadline=deadline)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=None)

        watcher = ExitWatcher(policies=[TimeStopPolicy()])
        await watcher._evaluate_position(db_session, position, broker, audit)

        assert position.status == PositionStatus.CLOSING.value


# ─── CLOSING ポジションのスキップ ─────────────────────────────────────────────

class TestExitWatcherSkipClosing:

    @pytest.mark.asyncio
    async def test_closing_position_is_skipped_in_watch_once(self, db_session):
        """CLOSING 状態のポジションは _watch_once でクエリされない（OPEN のみ対象）"""
        # OPEN ポジションなし、CLOSING のみ
        closing_pos = _make_open_position(db_session, tp_price=3000.0)
        await db_session.flush()
        closing_pos.status = PositionStatus.CLOSING.value
        await db_session.flush()

        # _evaluate_position が呼ばれないことを確認するため
        # _watch_once を DB モックなしで直接呼び出す（AsyncSessionLocal をパッチ）
        initiate_exit_called = []

        async def _mock_evaluate(db, pos, broker, audit):
            initiate_exit_called.append(pos.id)

        watcher = ExitWatcher()
        watcher._evaluate_position = _mock_evaluate

        from unittest.mock import AsyncMock, MagicMock
        import contextlib

        @contextlib.asynccontextmanager
        async def mock_session():
            yield db_session

        with patch("trade_app.services.exit_watcher.AsyncSessionLocal", mock_session):
            with patch("trade_app.services.exit_watcher._get_broker", return_value=_mock_broker()):
                await watcher._watch_once()

        # CLOSING ポジションは _evaluate_position に渡されない
        assert closing_pos.id not in initiate_exit_called

    @pytest.mark.asyncio
    async def test_evaluate_position_with_closing_status_does_nothing(self, db_session):
        """CLOSING ポジションを直接 _evaluate_position に渡しても initiate_exit は呼ばれない
        (initiate_exit が ValueError を上げて処理が止まる)"""
        position = _make_open_position(db_session, tp_price=1.0)  # TP はすぐ発火
        await db_session.flush()
        position.status = PositionStatus.CLOSING.value  # CLOSING に手動変更
        position.remaining_qty = 100
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=9999.0)  # TP を超えた価格

        watcher = ExitWatcher(policies=[TakeProfitPolicy()])
        # ValueError が発生しても _evaluate_position は例外を握りつぶす
        await watcher._evaluate_position(db_session, position, broker, audit)

        # CLOSING のまま（CLOSED にはなっていない）
        assert position.status == PositionStatus.CLOSING.value


# ─── 価格 None の挙動 ─────────────────────────────────────────────────────────

class TestExitWatcherPriceNone:

    @pytest.mark.asyncio
    async def test_price_none_skips_tp_sl(self, db_session):
        """価格 None の場合 TP/SL は発火しない"""
        position = _make_open_position(db_session, tp_price=3000.0, sl_price=2000.0)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=None)

        watcher = ExitWatcher(policies=[TakeProfitPolicy(), StopLossPolicy()])
        await watcher._evaluate_position(db_session, position, broker, audit)

        assert position.status == PositionStatus.OPEN.value

    @pytest.mark.asyncio
    async def test_price_none_still_fires_timestop(self, db_session):
        """価格 None でも TimeStop は発火すること"""
        deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        position = _make_open_position(
            db_session, tp_price=3000.0, sl_price=2000.0, exit_deadline=deadline
        )
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=None)

        from trade_app.services.exit_policies import DEFAULT_EXIT_POLICIES
        watcher = ExitWatcher(policies=DEFAULT_EXIT_POLICIES)
        await watcher._evaluate_position(db_session, position, broker, audit)

        assert position.status == PositionStatus.CLOSING.value
        assert position.exit_reason == ExitReason.TIMEOUT.value


# ─── BrokerAdapter.get_market_price() 経路 ───────────────────────────────────

class TestExitWatcherBrokerIntegration:

    @pytest.mark.asyncio
    async def test_get_market_price_called_for_each_position(self, db_session):
        """_evaluate_position が broker.get_market_price() を呼び出すこと"""
        position = _make_open_position(db_session, ticker="9984", tp_price=5000.0)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=4000.0)

        watcher = ExitWatcher(policies=[TakeProfitPolicy()])
        await watcher._evaluate_position(db_session, position, broker, audit)

        broker.get_market_price.assert_called_once_with("9984")

    @pytest.mark.asyncio
    async def test_price_fetch_exception_does_not_crash(self, db_session):
        """価格取得で例外が発生しても _evaluate_position がクラッシュしないこと"""
        position = _make_open_position(db_session, tp_price=3000.0)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = AsyncMock()
        broker.get_market_price = AsyncMock(side_effect=RuntimeError("connection error"))

        watcher = ExitWatcher(policies=[TakeProfitPolicy()])
        # 例外を握りつぶして完了すること
        await watcher._evaluate_position(db_session, position, broker, audit)

        # 価格取得失敗 → TP 発火なし
        assert position.status == PositionStatus.OPEN.value


# ─── 二重 exit 発行しないこと ─────────────────────────────────────────────────

class TestExitWatcherNoDuplicateExit:

    @pytest.mark.asyncio
    async def test_closing_position_not_re_evaluated(self, db_session):
        """_watch_once は OPEN 状態のポジションのみ SELECT するため CLOSING は対象外"""
        # OPEN 1件 + CLOSING 1件
        open_pos = _make_open_position(db_session, ticker="7203", tp_price=3000.0)
        closing_pos = _make_open_position(db_session, ticker="6501", tp_price=1.0)
        await db_session.flush()
        closing_pos.status = PositionStatus.CLOSING.value
        closing_pos.remaining_qty = 100
        await db_session.flush()

        evaluated = []

        async def _mock_evaluate(db, pos, broker, audit):
            evaluated.append(pos.id)
            # OPEN のポジションは TP 発火させない（価格が低い）

        watcher = ExitWatcher()
        watcher._evaluate_position = _mock_evaluate

        import contextlib

        @contextlib.asynccontextmanager
        async def mock_session():
            yield db_session

        with patch("trade_app.services.exit_watcher.AsyncSessionLocal", mock_session):
            with patch("trade_app.services.exit_watcher._get_broker", return_value=_mock_broker(price=2000.0)):
                await watcher._watch_once()

        # OPEN のみが評価対象
        assert open_pos.id in evaluated
        assert closing_pos.id not in evaluated

    @pytest.mark.asyncio
    async def test_initiate_exit_creates_one_exit_order(self, db_session):
        """TP 発火で exit 注文が1件だけ作成されること"""
        position = _make_open_position(db_session, tp_price=3000.0)
        await db_session.flush()

        audit = AuditLogger(db_session)
        broker = _mock_broker(price=3000.0)

        watcher = ExitWatcher(policies=[TakeProfitPolicy()])
        await watcher._evaluate_position(db_session, position, broker, audit)

        await db_session.flush()

        result = await db_session.execute(
            select(Order).where(
                Order.position_id == position.id,
                Order.is_exit_order == True,  # noqa: E712
            )
        )
        exit_orders = result.scalars().all()
        assert len(exit_orders) == 1
