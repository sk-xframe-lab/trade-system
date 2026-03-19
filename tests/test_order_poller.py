"""
OrderPoller のテスト

MockBrokerAdapter の FillBehavior を使って各シナリオを検証する:
  - IMMEDIATE      : SUBMITTED → FILLED → ポジション開設
  - PARTIAL_THEN_FULL : PARTIAL → FILLED の2段階遷移
  - CANCELLED      : SUBMITTED → CANCELLED
  - UNKNOWN        : SUBMITTED → UNKNOWN（新規発注ブロック確認）
  - NEVER_FILL     : 長時間 SUBMITTED → UNKNOWN へ自動遷移
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from trade_app.models.enums import OrderStatus, PositionStatus, SignalStatus
from trade_app.models.execution import Execution
from trade_app.models.order import Order
from trade_app.models.order_state_transition import OrderStateTransition
from trade_app.models.position import Position
from trade_app.models.signal import TradeSignal


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_signal_and_order(
    db_session,
    ticker="7203",
    quantity=100,
    limit_price=2500.0,
    broker_order_id: str = None,
    submitted: bool = True,
) -> tuple[TradeSignal, Order]:
    """テスト用シグナル + 注文ペアを作成する"""
    signal = TradeSignal(
        idempotency_key=str(uuid.uuid4()),
        source_system="test",
        ticker=ticker,
        signal_type="entry",
        order_type="limit",
        side="buy",
        quantity=quantity,
        limit_price=limit_price,
        status=SignalStatus.PROCESSING.value,
        generated_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(signal)

    order = Order(
        signal_id=None,  # flush 後に設定
        ticker=ticker,
        order_type="limit",
        side="buy",
        quantity=quantity,
        limit_price=limit_price,
        status=OrderStatus.SUBMITTED.value,
        broker_order_id=broker_order_id or f"MOCK-{uuid.uuid4().hex[:12].upper()}",
        submitted_at=datetime.now(timezone.utc) if submitted else None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(order)

    # signal_id を flush 後に設定するため一旦 None で追加し
    # テスト内で order.signal_id = signal.id のように設定する
    return signal, order


# ─── テスト ────────────────────────────────────────────────────────────────────

class TestOrderPollerProcessOrder:
    """OrderPoller._process_order() の単体テスト"""

    @pytest.mark.asyncio
    async def test_filled_creates_execution_and_position(self, db_session, mock_redis):
        """
        SUBMITTED → FILLED: Execution レコードと Position が作成されること
        """
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter, FillBehavior
        from trade_app.brokers.base import OrderStatusResponse

        # テストデータを作成
        signal, order = _make_signal_and_order(db_session, ticker="7203", limit_price=2500.0)
        await db_session.flush()
        order.signal_id = signal.id
        await db_session.flush()

        # FILLED を返す broker をセットアップ
        broker = MockBrokerAdapter(
            fill_delay_sec=0.0,
            default_behavior=FillBehavior.IMMEDIATE,
        )
        # 直接 _orders に FILLED をセット（非同期タスク待ちを避ける）
        from trade_app.models.enums import OrderStatus as OS
        from trade_app.brokers.base import OrderStatusResponse
        broker._orders[order.broker_order_id] = OrderStatusResponse(
            broker_order_id=order.broker_order_id,
            status=OS.FILLED,
            filled_quantity=100,
            filled_price=2500.0,
        )

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.commit()

        # Order が FILLED になっていること
        result = await db_session.execute(
            select(Order).where(Order.id == order.id)
        )
        updated_order = result.scalar_one()
        assert updated_order.status == OrderStatus.FILLED.value
        assert updated_order.filled_quantity == 100
        assert updated_order.filled_price == 2500.0

        # Execution が作成されていること
        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 1
        assert executions[0].quantity == 100
        assert executions[0].price == 2500.0

        # Position が作成されていること
        pos_result = await db_session.execute(
            select(Position).where(Position.order_id == order.id)
        )
        positions = pos_result.scalars().all()
        assert len(positions) == 1
        assert positions[0].status == PositionStatus.OPEN.value

    @pytest.mark.asyncio
    async def test_cancelled_order_no_position(self, db_session, mock_redis):
        """
        SUBMITTED → CANCELLED: ポジションが作成されないこと
        """
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS

        signal, order = _make_signal_and_order(db_session, ticker="6501")
        await db_session.flush()
        order.signal_id = signal.id
        await db_session.flush()

        broker = MockBrokerAdapter()
        broker._orders[order.broker_order_id] = OrderStatusResponse(
            broker_order_id=order.broker_order_id,
            status=OS.CANCELLED,
        )

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.commit()

        result = await db_session.execute(select(Order).where(Order.id == order.id))
        updated = result.scalar_one()
        assert updated.status == OrderStatus.CANCELLED.value

        # ポジション なし
        pos_result = await db_session.execute(
            select(Position).where(Position.order_id == order.id)
        )
        assert pos_result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_unknown_status_blocks_risk_check(self, db_session, mock_redis):
        """
        UNKNOWN 状態の注文がある銘柄への新規発注がリスクチェックでブロックされること
        """
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.services.risk_manager import RiskManager, RiskRejectedError
        from trade_app.brokers.mock_broker import MockBrokerAdapter, FillBehavior
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS

        ticker = "9984"
        signal, order = _make_signal_and_order(db_session, ticker=ticker)
        await db_session.flush()
        order.signal_id = signal.id
        await db_session.flush()

        broker = MockBrokerAdapter(default_behavior=FillBehavior.UNKNOWN)
        broker._orders[order.broker_order_id] = OrderStatusResponse(
            broker_order_id=order.broker_order_id,
            status=OS.UNKNOWN,
        )
        broker._order_behaviors[order.broker_order_id] = FillBehavior.UNKNOWN

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        # UNKNOWN に遷移させる
        await poller._process_order(
            db=db_session,
            order=order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.commit()

        # Order が UNKNOWN になっていること
        result = await db_session.execute(select(Order).where(Order.id == order.id))
        assert result.scalar_one().status == OS.UNKNOWN.value

        # 新規シグナルのリスクチェックでブロックされること
        new_signal = TradeSignal(
            idempotency_key=str(uuid.uuid4()),
            source_system="test",
            ticker=ticker,
            signal_type="entry",
            order_type="limit",
            side="buy",
            quantity=100,
            limit_price=50000.0,
            status=SignalStatus.RECEIVED.value,
            generated_at=datetime.now(timezone.utc),
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(new_signal)
        await db_session.flush()

        risk = RiskManager(
            db=db_session,
            broker=MockBrokerAdapter(),
            audit=AuditLogger(db_session),
        )
        with pytest.raises(RiskRejectedError, match="未解決注文"):
            with patch(
                "trade_app.services.risk_manager.RiskManager._check_market_hours",
                return_value=None,
            ), patch(
                "trade_app.services.risk_manager.RiskManager._check_position_size",
                return_value=None,
            ), patch(
                "trade_app.services.risk_manager.RiskManager._check_max_positions",
                return_value=None,
            ), patch(
                "trade_app.services.risk_manager.RiskManager._check_daily_loss",
                return_value=None,
            ), patch(
                "trade_app.services.risk_manager.RiskManager._check_ticker_concentration",
                return_value=None,
            ), patch(
                "trade_app.services.halt_manager.HaltManager.is_halted",
                new_callable=AsyncMock,
                return_value=(False, ""),
            ):
                await risk.check(new_signal)

    @pytest.mark.asyncio
    async def test_partial_fill_creates_execution_and_stays_partial(self, db_session):
        """
        SUBMITTED → PARTIAL: Execution レコードが作成され PARTIAL のまま継続すること
        """
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS

        signal, order = _make_signal_and_order(db_session, ticker="7267", quantity=200)
        await db_session.flush()
        order.signal_id = signal.id
        await db_session.flush()

        broker = MockBrokerAdapter()
        broker._orders[order.broker_order_id] = OrderStatusResponse(
            broker_order_id=order.broker_order_id,
            status=OS.PARTIAL,
            filled_quantity=100,   # 200株中100株だけ約定
            filled_price=1500.0,
        )

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.commit()

        result = await db_session.execute(select(Order).where(Order.id == order.id))
        updated = result.scalar_one()
        assert updated.status == OS.PARTIAL.value
        assert updated.filled_quantity == 100

        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 1
        assert executions[0].quantity == 100

    @pytest.mark.asyncio
    async def test_transitions_recorded_for_filled_order(self, db_session):
        """
        FILLED 遷移時に order_state_transitions が記録されること
        """
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS

        signal, order = _make_signal_and_order(db_session, ticker="8306")
        await db_session.flush()
        order.signal_id = signal.id
        await db_session.flush()

        broker = MockBrokerAdapter()
        broker._orders[order.broker_order_id] = OrderStatusResponse(
            broker_order_id=order.broker_order_id,
            status=OS.FILLED,
            filled_quantity=100,
            filled_price=1200.0,
        )

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.commit()

        trans_result = await db_session.execute(
            select(OrderStateTransition)
            .where(OrderStateTransition.order_id == order.id)
            .order_by(OrderStateTransition.created_at)
        )
        transitions = trans_result.scalars().all()
        assert len(transitions) >= 1

        # 最後の遷移が FILLED であること
        last = transitions[-1]
        assert last.to_status == OS.FILLED.value
        assert last.triggered_by == "poller"


# ─── exit 注文処理テスト ────────────────────────────────────────────────────────

class TestOrderPollerExitOrders:
    """exit 注文（is_exit_order=True）に対する OrderPoller の処理を検証する"""

    # ─── 共通ヘルパー ─────────────────────────────────────────────────────────

    async def _setup_exit_order(
        self,
        db_session,
        ticker: str = "7203",
        qty: int = 100,
        filled_qty: int = 0,
        order_status: str = None,
    ):
        """OPEN ポジションと紐付く exit 注文を作成して返す"""
        from trade_app.models.enums import OrderStatus, PositionStatus

        entry_order = Order(
            ticker=ticker,
            order_type="market",
            side="buy",
            quantity=qty,
            status=OrderStatus.FILLED.value,
            filled_quantity=qty,
            filled_price=2500.0,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(entry_order)
        await db_session.flush()

        from trade_app.models.position import Position
        position = Position(
            order_id=entry_order.id,
            ticker=ticker,
            side="buy",
            quantity=qty,
            entry_price=2500.0,
            status=PositionStatus.CLOSING.value,
            exit_reason="tp_hit",
            remaining_qty=qty,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(position)
        await db_session.flush()

        from trade_app.models.enums import OrderStatus as OS
        exit_order = Order(
            position_id=position.id,
            is_exit_order=True,
            ticker=ticker,
            order_type="market",
            side="sell",
            quantity=qty,
            status=order_status or OS.SUBMITTED.value,
            filled_quantity=filled_qty,
            broker_order_id=f"MOCK-EXIT-{uuid.uuid4().hex[:8].upper()}",
            submitted_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(exit_order)
        await db_session.flush()

        return position, exit_order

    # ─── PARTIAL ─────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_exit_partial_updates_remaining_qty(self, db_session):
        """exit 注文 PARTIAL: remaining_qty が減算されること"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS

        position, exit_order = await self._setup_exit_order(db_session, qty=100)

        broker = MockBrokerAdapter()
        _resp = OrderStatusResponse(
            broker_order_id=exit_order.broker_order_id,
            status=OS.PARTIAL,
            filled_quantity=40,
            filled_price=2600.0,
        )
        _resp.broker_execution_id = f"EXEC-{uuid.uuid4().hex[:8]}"
        broker._orders[exit_order.broker_order_id] = _resp

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.refresh(position)

        assert position.status == "closing"  # CLOSING 維持
        assert position.remaining_qty == 60  # 100 - 40

    @pytest.mark.asyncio
    async def test_exit_partial_creates_execution(self, db_session):
        """exit 注文 PARTIAL: Execution が作成されること"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS

        position, exit_order = await self._setup_exit_order(db_session, qty=100)

        exec_id = f"EXEC-{uuid.uuid4().hex[:8]}"
        broker = MockBrokerAdapter()
        _resp = OrderStatusResponse(
            broker_order_id=exit_order.broker_order_id,
            status=OS.PARTIAL,
            filled_quantity=50,
            filled_price=2600.0,
        )
        _resp.broker_execution_id = exec_id
        broker._orders[exit_order.broker_order_id] = _resp

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )

        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == exit_order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 1
        assert executions[0].quantity == 50
        assert executions[0].broker_execution_id == exec_id

    # ─── FILLED ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_exit_filled_closes_position(self, db_session):
        """exit 注文 FILLED: ポジションが CLOSED に遷移すること"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS
        from trade_app.models.position import Position

        position, exit_order = await self._setup_exit_order(db_session, qty=100)

        broker = MockBrokerAdapter()
        _resp = OrderStatusResponse(
            broker_order_id=exit_order.broker_order_id,
            status=OS.FILLED,
            filled_quantity=100,
            filled_price=2600.0,
        )
        _resp.broker_execution_id = f"EXEC-{uuid.uuid4().hex[:8]}"
        broker._orders[exit_order.broker_order_id] = _resp

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.refresh(position)

        from trade_app.models.enums import PositionStatus
        assert position.status == PositionStatus.CLOSED.value
        assert position.remaining_qty == 0

    @pytest.mark.asyncio
    async def test_exit_filled_creates_execution(self, db_session):
        """exit 注文 FILLED: Execution が作成されること"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS

        position, exit_order = await self._setup_exit_order(db_session, qty=100)

        exec_id = f"EXEC-{uuid.uuid4().hex[:8]}"
        broker = MockBrokerAdapter()
        _resp = OrderStatusResponse(
            broker_order_id=exit_order.broker_order_id,
            status=OS.FILLED,
            filled_quantity=100,
            filled_price=2600.0,
        )
        _resp.broker_execution_id = exec_id
        broker._orders[exit_order.broker_order_id] = _resp

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )

        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == exit_order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 1
        assert executions[0].quantity == 100

    # ─── CANCELLED → revert_to_open ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_exit_cancelled_reverts_to_open(self, db_session):
        """exit 注文 CANCELLED: ポジションが OPEN に戻ること"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS, PositionStatus
        from trade_app.models.position import Position

        position, exit_order = await self._setup_exit_order(db_session, qty=100)

        broker = MockBrokerAdapter()
        broker._orders[exit_order.broker_order_id] = OrderStatusResponse(
            broker_order_id=exit_order.broker_order_id,
            status=OS.CANCELLED,
        )

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.refresh(position)

        assert position.status == PositionStatus.OPEN.value
        assert position.exit_reason is None

    # ─── REJECTED → revert_to_open ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_exit_rejected_reverts_to_open(self, db_session):
        """exit 注文 REJECTED: ポジションが OPEN に戻ること"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS, PositionStatus

        position, exit_order = await self._setup_exit_order(db_session, qty=100)

        broker = MockBrokerAdapter()
        broker._orders[exit_order.broker_order_id] = OrderStatusResponse(
            broker_order_id=exit_order.broker_order_id,
            status=OS.REJECTED,
            message="証拠金不足",
        )

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.refresh(position)

        assert position.status == PositionStatus.OPEN.value
        assert position.exit_reason is None

    # ─── UNKNOWN → CLOSING 維持 ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_exit_unknown_keeps_closing(self, db_session):
        """exit 注文 UNKNOWN: ポジションが CLOSING のまま維持されること"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS, PositionStatus

        position, exit_order = await self._setup_exit_order(db_session, qty=100)

        broker = MockBrokerAdapter()
        broker._orders[exit_order.broker_order_id] = OrderStatusResponse(
            broker_order_id=exit_order.broker_order_id,
            status=OS.UNKNOWN,
        )

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )
        await db_session.refresh(position)

        assert position.status == PositionStatus.CLOSING.value  # CLOSING 維持

        result = await db_session.execute(select(Order).where(Order.id == exit_order.id))
        updated = result.scalar_one()
        assert updated.status == OS.UNKNOWN.value

    # ─── broker_execution_id 重複防止 ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_exit_filled_duplicate_exec_id_no_new_execution(self, db_session):
        """broker_execution_id 重複時 Execution が新規作成されないこと"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS

        position, exit_order = await self._setup_exit_order(db_session, qty=100)

        dup_exec_id = f"EXEC-DUP-{uuid.uuid4().hex[:8]}"
        existing_exec = Execution(
            order_id=exit_order.id,
            broker_execution_id=dup_exec_id,
            ticker="7203",
            side="sell",
            quantity=100,
            price=2600.0,
            executed_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing_exec)
        position.remaining_qty = 0
        await db_session.flush()

        broker = MockBrokerAdapter()
        _resp = OrderStatusResponse(
            broker_order_id=exit_order.broker_order_id,
            status=OS.FILLED,
            filled_quantity=100,
            filled_price=2600.0,
        )
        _resp.broker_execution_id = dup_exec_id
        broker._orders[exit_order.broker_order_id] = _resp

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )

        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == exit_order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 1, f"Execution が重複作成された: {len(executions)} 件"

    # ─── 二重ポーリングでも二重計上しない ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_double_polling_no_double_execution(self, db_session):
        """同じ PARTIAL 応答を2回ポーリングしても Execution が1件だけであること"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.brokers.mock_broker import MockBrokerAdapter
        from trade_app.brokers.base import OrderStatusResponse
        from trade_app.models.enums import OrderStatus as OS

        position, exit_order = await self._setup_exit_order(db_session, qty=100)

        exec_id = f"EXEC-DOUBLE-{uuid.uuid4().hex[:8]}"
        broker = MockBrokerAdapter()
        _resp = OrderStatusResponse(
            broker_order_id=exit_order.broker_order_id,
            status=OS.PARTIAL,
            filled_quantity=50,
            filled_price=2600.0,
        )
        _resp.broker_execution_id = exec_id
        broker._orders[exit_order.broker_order_id] = _resp

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        # 1回目
        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )

        # 2回目（同一 broker_execution_id）
        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )

        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == exit_order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 1, f"二重計上: {len(executions)} 件"


# ─── broker_order_id = NULL ガードテスト ──────────────────────────────────────

class TestNoBrokerOrderId:
    """
    broker_order_id が NULL の SUBMITTED 注文に対する OrderPoller の挙動を検証する。

    Phase 10-A で BrokerAPIError 時に SUBMITTED のまま残る注文が存在しうる。
    get_order_status(None) を呼ばず、長時間経過後に UNKNOWN へ遷移させること。
    """

    def _make_no_broker_id_order(self, db_session, submitted_at=None) -> Order:
        """broker_order_id = NULL の SUBMITTED 注文を作成"""
        order = Order(
            id=str(uuid.uuid4()),
            ticker="7203",
            order_type="limit",
            side="buy",
            quantity=100,
            limit_price=2500.0,
            status=OrderStatus.SUBMITTED.value,
            broker_order_id=None,  # NULL: BrokerAPIError タイムアウト由来
            submitted_at=submitted_at or datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(order)
        return order

    @pytest.mark.asyncio
    async def test_get_order_status_not_called_when_no_broker_id(self, db_session):
        """broker_order_id = NULL の注文で get_order_status が呼ばれないこと"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from unittest.mock import AsyncMock, MagicMock

        order = self._make_no_broker_id_order(db_session)
        await db_session.flush()

        broker = MagicMock()
        broker.get_order_status = AsyncMock()  # 呼ばれないことを確認

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )

        broker.get_order_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_unchanged_before_stuck_threshold(self, db_session):
        """STUCK_ORDER_SEC 未満では状態が維持されること（何もしない）"""
        from trade_app.services.order_poller import OrderPoller, STUCK_ORDER_SEC
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from unittest.mock import MagicMock, AsyncMock

        # 経過時間を STUCK_ORDER_SEC の半分に設定
        submitted_at = datetime.now(timezone.utc) - timedelta(seconds=STUCK_ORDER_SEC // 2)
        order = self._make_no_broker_id_order(db_session, submitted_at=submitted_at)
        await db_session.flush()

        broker = MagicMock()
        broker.get_order_status = AsyncMock()

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )

        # ステータス変化なし
        result = await db_session.execute(select(Order).where(Order.id == order.id))
        updated = result.scalar_one()
        assert updated.status == OrderStatus.SUBMITTED.value

        # broker 照会なし
        broker.get_order_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_transitions_to_unknown_after_stuck_threshold(self, db_session):
        """STUCK_ORDER_SEC 超過後に UNKNOWN へ遷移すること"""
        from trade_app.services.order_poller import OrderPoller, STUCK_ORDER_SEC
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from trade_app.models.order_state_transition import OrderStateTransition
        from unittest.mock import MagicMock, AsyncMock

        # 経過時間を STUCK_ORDER_SEC + 1 秒に設定
        submitted_at = datetime.now(timezone.utc) - timedelta(seconds=STUCK_ORDER_SEC + 1)
        order = self._make_no_broker_id_order(db_session, submitted_at=submitted_at)
        await db_session.flush()

        broker = MagicMock()
        broker.get_order_status = AsyncMock()

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )

        # UNKNOWN に遷移していること
        result = await db_session.execute(select(Order).where(Order.id == order.id))
        updated = result.scalar_one()
        assert updated.status == OrderStatus.UNKNOWN.value

        # 状態遷移レコードが記録されていること
        trans_result = await db_session.execute(
            select(OrderStateTransition).where(OrderStateTransition.order_id == order.id)
        )
        transitions = trans_result.scalars().all()
        assert len(transitions) >= 1
        last = transitions[-1]
        assert last.to_status == OrderStatus.UNKNOWN.value
        assert "broker_order_id" in (last.reason or "")

        # broker 照会なし
        broker.get_order_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_created_at_when_submitted_at_is_none(self, db_session):
        """submitted_at が NULL の場合は created_at を基準に経過時間を計算すること"""
        from trade_app.services.order_poller import OrderPoller, STUCK_ORDER_SEC
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger
        from unittest.mock import MagicMock, AsyncMock

        # submitted_at=None, created_at を十分古い時刻に設定
        old_time = datetime.now(timezone.utc) - timedelta(seconds=STUCK_ORDER_SEC + 10)
        order = Order(
            id=str(uuid.uuid4()),
            ticker="7203",
            order_type="limit",
            side="buy",
            quantity=100,
            limit_price=2500.0,
            status=OrderStatus.SUBMITTED.value,
            broker_order_id=None,
            submitted_at=None,           # NULL
            created_at=old_time,         # 古い created_at
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(order)
        await db_session.flush()

        broker = MagicMock()
        broker.get_order_status = AsyncMock()

        audit = AuditLogger(db_session)
        broker_logger = BrokerCallLogger(db_session)
        poller = OrderPoller()

        await poller._process_order(
            db=db_session,
            order=order,
            broker=broker,
            broker_logger=broker_logger,
            audit=audit,
        )

        # created_at 基準で STUCK → UNKNOWN に遷移していること
        result = await db_session.execute(select(Order).where(Order.id == order.id))
        updated = result.scalar_one()
        assert updated.status == OrderStatus.UNKNOWN.value
