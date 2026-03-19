"""
RecoveryManager テスト

検証内容:
  A. exit CANCELLED / REJECTED → revert_to_open
     - CANCELLED で OPEN に戻ること
     - REJECTED で OPEN に戻ること
     - CLOSING 以外では revert しないこと
  B. entry delta Execution
     - PARTIAL 30 → FILLED 100: Execution が 30 + 70 の2件
     - 二重ポーリング対応: broker_execution_id 重複で増えないこと
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from trade_app.brokers.base import OrderStatusResponse
from trade_app.models.enums import OrderStatus, PositionStatus
from trade_app.models.execution import Execution
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.signal import TradeSignal
from trade_app.services.recovery_manager import RecoveryManager


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _make_entry_order_filled(db_session, ticker="7203", qty=100) -> Order:
    order = Order(
        id=str(uuid.uuid4()),
        signal_id=None,
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
    db_session.add(order)
    return order


def _make_closing_position(db_session, ticker="7203", qty=100) -> Position:
    entry_order = _make_entry_order_filled(db_session, ticker=ticker, qty=qty)
    position = Position(
        id=str(uuid.uuid4()),
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
    return position


def _make_exit_order(
    db_session,
    position_id: str,
    status: str,
    broker_order_id: str | None = None,
    filled_quantity: int = 0,
) -> Order:
    order = Order(
        signal_id=None,
        position_id=position_id,
        is_exit_order=True,
        ticker="7203",
        order_type="market",
        side="sell",
        quantity=100,
        status=status,
        broker_order_id=broker_order_id or f"MOCK-{uuid.uuid4().hex[:12].upper()}",
        filled_quantity=filled_quantity,
        submitted_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(order)
    return order


def _make_entry_order_submitted(db_session, ticker="7203", qty=100) -> tuple[TradeSignal, Order]:
    signal = TradeSignal(
        idempotency_key=str(uuid.uuid4()),
        source_system="test",
        ticker=ticker,
        signal_type="entry",
        order_type="limit",
        side="buy",
        quantity=qty,
        limit_price=2500.0,
        status="processing",
        generated_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(signal)

    order = Order(
        signal_id=None,
        ticker=ticker,
        order_type="limit",
        side="buy",
        quantity=qty,
        status=OrderStatus.SUBMITTED.value,
        broker_order_id=f"MOCK-ENTRY-{uuid.uuid4().hex[:8].upper()}",
        filled_quantity=0,
        submitted_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(order)
    return signal, order


# ─── A. Recovery exit CANCELLED / REJECTED → revert_to_open ─────────────────

class TestRecoveryExitCancelledRejected:

    @pytest.mark.asyncio
    async def test_cancelled_exit_reverts_position_to_open(self, db_session):
        """CANCELLED の exit 注文が recovery で判明した場合 Position が OPEN に戻ること"""
        position = _make_closing_position(db_session)
        await db_session.flush()
        exit_order = _make_exit_order(
            db_session, position.id, OrderStatus.SUBMITTED.value,
            broker_order_id="MOCK-CANCEL-001"
        )
        await db_session.flush()

        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-CANCEL-001",
            status=OrderStatus.CANCELLED,
        )

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()
        with patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker):
            await rm._run_recovery(db_session)

        await db_session.refresh(position)
        assert position.status == PositionStatus.OPEN.value, (
            f"CANCELLED exit order 後は OPEN になるべきだが status={position.status}"
        )
        assert position.exit_reason is None

    @pytest.mark.asyncio
    async def test_rejected_exit_reverts_position_to_open(self, db_session):
        """REJECTED の exit 注文が recovery で判明した場合 Position が OPEN に戻ること"""
        position = _make_closing_position(db_session)
        await db_session.flush()
        exit_order = _make_exit_order(
            db_session, position.id, OrderStatus.SUBMITTED.value,
            broker_order_id="MOCK-REJECT-001"
        )
        await db_session.flush()

        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-REJECT-001",
            status=OrderStatus.REJECTED,
            message="残高不足",
        )

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()
        with patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker):
            await rm._run_recovery(db_session)

        await db_session.refresh(position)
        assert position.status == PositionStatus.OPEN.value

    @pytest.mark.asyncio
    async def test_cancelled_entry_order_does_not_revert_position(self, db_session):
        """entry 注文（is_exit_order=False）が CANCELLED でも Position には影響しないこと"""
        # entry 注文のみ（position は OPEN のまま）
        _, entry_order = _make_entry_order_submitted(db_session)
        entry_order.broker_order_id = "MOCK-ENTRY-CANCEL-001"
        await db_session.flush()

        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-ENTRY-CANCEL-001",
            status=OrderStatus.CANCELLED,
        )

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()
        with patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker):
            await rm._run_recovery(db_session)

        # Position が存在しないので Position に変化なし（エラーなしで完了）
        await db_session.refresh(entry_order)
        assert entry_order.status == OrderStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_cancelled_exit_does_not_revert_non_closing_position(self, db_session):
        """Position が CLOSING でない場合は revert_to_open を呼ばないこと"""
        # OPEN ポジション
        entry_order = _make_entry_order_filled(db_session)
        position = Position(
            order_id=entry_order.id,
            ticker="7203",
            side="buy",
            quantity=100,
            entry_price=2500.0,
            status=PositionStatus.OPEN.value,  # OPEN（not CLOSING）
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(position)
        await db_session.flush()

        exit_order = _make_exit_order(
            db_session, position.id, OrderStatus.SUBMITTED.value,
            broker_order_id="MOCK-OPEN-CANCEL-001"
        )
        await db_session.flush()

        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-OPEN-CANCEL-001",
            status=OrderStatus.CANCELLED,
        )

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()
        with patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker):
            await rm._run_recovery(db_session)

        await db_session.refresh(position)
        # OPEN のまま（変化なし）
        assert position.status == PositionStatus.OPEN.value

    @pytest.mark.asyncio
    async def test_revert_records_exit_transition(self, db_session):
        """revert_to_open() が PositionExitTransition を記録すること"""
        from trade_app.models.position_exit_transition import PositionExitTransition

        position = _make_closing_position(db_session)
        await db_session.flush()
        exit_order = _make_exit_order(
            db_session, position.id, OrderStatus.SUBMITTED.value,
            broker_order_id="MOCK-REVERT-TRANSITION-001"
        )
        await db_session.flush()
        pos_id = position.id

        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-REVERT-TRANSITION-001",
            status=OrderStatus.CANCELLED,
        )

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()
        with patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker):
            await rm._run_recovery(db_session)

        result = await db_session.execute(
            select(PositionExitTransition).where(
                PositionExitTransition.position_id == pos_id
            )
        )
        transitions = result.scalars().all()
        assert len(transitions) == 1
        t = transitions[0]
        assert t.from_status == PositionStatus.CLOSING.value
        assert t.to_status == PositionStatus.OPEN.value
        assert t.triggered_by == "recovery"


# ─── B. entry delta Execution ────────────────────────────────────────────────

class TestEntryDeltaExecution:
    """entry 注文の PARTIAL → FILLED が delta Execution で記録されること"""

    @pytest.mark.asyncio
    async def test_partial_then_filled_creates_two_executions(self, db_session):
        """PARTIAL 30 → FILLED 100 で Execution が 30 + 70 の2件記録されること"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger

        signal, order = _make_entry_order_submitted(db_session)
        await db_session.flush()
        order.signal_id = signal.id
        await db_session.flush()

        poller = OrderPoller()
        audit = AuditLogger(db_session)
        bl_mock = AsyncMock(spec=BrokerCallLogger)
        bl_mock.before_status_query = AsyncMock(return_value=MagicMock())
        bl_mock.after_status_query = AsyncMock()

        exec_id_1 = f"EXEC-P1-{uuid.uuid4().hex[:8]}"
        exec_id_2 = f"EXEC-P2-{uuid.uuid4().hex[:8]}"

        # ─── 1回目: PARTIAL 30株 ──────────────────────────────────────────
        mock_partial = MagicMock()
        mock_partial.status = OrderStatus.PARTIAL
        mock_partial.filled_quantity = 30
        mock_partial.filled_price = 2500.0
        mock_partial.broker_execution_id = exec_id_1

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_partial

        await poller._process_order(
            db=db_session, order=order, broker=mock_broker,
            broker_logger=bl_mock, audit=audit,
        )
        assert order.filled_quantity == 30

        # ─── 2回目: FILLED 100株 ─────────────────────────────────────────
        mock_filled = MagicMock()
        mock_filled.status = OrderStatus.FILLED
        mock_filled.filled_quantity = 100
        mock_filled.filled_price = 2510.0
        mock_filled.broker_execution_id = exec_id_2

        mock_broker.get_order_status.return_value = mock_filled

        await poller._process_order(
            db=db_session, order=order, broker=mock_broker,
            broker_logger=bl_mock, audit=audit,
        )

        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 2, f"Execution が {len(executions)} 件（期待: 2件）"

        qtys = sorted(e.quantity for e in executions)
        assert qtys == [30, 70], f"数量が期待と異なる: {qtys}"
        assert order.filled_quantity == 100

    @pytest.mark.asyncio
    async def test_duplicate_broker_exec_id_no_double_execution(self, db_session):
        """broker_execution_id 重複ポーリングで Execution が重複作成されないこと"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger

        signal, order = _make_entry_order_submitted(db_session)
        await db_session.flush()
        order.signal_id = signal.id
        await db_session.flush()

        exec_id = f"EXEC-DUP-{uuid.uuid4().hex[:8]}"

        mock_filled = MagicMock()
        mock_filled.status = OrderStatus.FILLED
        mock_filled.filled_quantity = 100
        mock_filled.filled_price = 2500.0
        mock_filled.broker_execution_id = exec_id

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_filled

        poller = OrderPoller()
        audit = AuditLogger(db_session)
        bl_mock = AsyncMock(spec=BrokerCallLogger)
        bl_mock.before_status_query = AsyncMock(return_value=MagicMock())
        bl_mock.after_status_query = AsyncMock()

        # 1回目
        await poller._process_order(
            db=db_session, order=order, broker=mock_broker,
            broker_logger=bl_mock, audit=audit,
        )
        # 2回目（同じ broker_execution_id）— 重複チェックが走る
        # order を SUBMITTED に戻して再テスト
        order.status = OrderStatus.SUBMITTED.value
        await db_session.flush()

        await poller._process_order(
            db=db_session, order=order, broker=mock_broker,
            broker_logger=bl_mock, audit=audit,
        )

        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 1, f"Execution が重複した: {len(executions)} 件"

    @pytest.mark.asyncio
    async def test_submitted_to_filled_delta_equals_full_quantity(self, db_session):
        """SUBMITTED → FILLED 直接（PARTIAL なし）の場合 delta == full quantity"""
        from trade_app.services.order_poller import OrderPoller
        from trade_app.services.audit_logger import AuditLogger
        from trade_app.services.broker_call_logger import BrokerCallLogger

        signal, order = _make_entry_order_submitted(db_session, qty=50)
        await db_session.flush()
        order.signal_id = signal.id
        await db_session.flush()

        mock_filled = MagicMock()
        mock_filled.status = OrderStatus.FILLED
        mock_filled.filled_quantity = 50
        mock_filled.filled_price = 2500.0
        mock_filled.broker_execution_id = f"EXEC-DIRECT-{uuid.uuid4().hex[:8]}"

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_filled

        poller = OrderPoller()
        audit = AuditLogger(db_session)
        bl_mock = AsyncMock(spec=BrokerCallLogger)
        bl_mock.before_status_query = AsyncMock(return_value=MagicMock())
        bl_mock.after_status_query = AsyncMock()

        await poller._process_order(
            db=db_session, order=order, broker=mock_broker,
            broker_logger=bl_mock, audit=audit,
        )

        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 1
        assert executions[0].quantity == 50


# ─── C. exit weighted average ────────────────────────────────────────────────

class TestExitWeightedAverage:
    """finalize_exit() の exit_price が Execution 加重平均になること"""

    @pytest.mark.asyncio
    async def test_single_execution_exit_price(self, db_session):
        """単一 Execution の場合 exit_price == execution.price"""
        from trade_app.services.position_manager import PositionManager
        from trade_app.services.audit_logger import AuditLogger

        # ポジション作成
        entry_order = _make_entry_order_filled(db_session)
        position = Position(
            order_id=entry_order.id,
            ticker="7203",
            side="buy",
            quantity=100,
            entry_price=2500.0,
            status=PositionStatus.CLOSING.value,
            exit_reason="tp_hit",
            remaining_qty=0,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(position)

        # exit 注文
        exit_order = Order(
            signal_id=None,
            position_id=position.id if hasattr(position, 'id') else None,
            is_exit_order=True,
            ticker="7203",
            order_type="market",
            side="sell",
            quantity=100,
            status=OrderStatus.FILLED.value,
            filled_quantity=100,
            filled_price=2700.0,
            filled_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(exit_order)
        await db_session.flush()

        # position_id を更新
        exit_order.position_id = position.id
        await db_session.flush()

        # Execution を1件作成
        execution = Execution(
            order_id=exit_order.id,
            ticker="7203",
            side="sell",
            quantity=100,
            price=2700.0,
            executed_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(execution)
        await db_session.flush()

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)

        result = await pos_mgr.finalize_exit(position=position, exit_order=exit_order)

        assert position.exit_price == 2700.0
        assert result.exit_price == 2700.0

    @pytest.mark.asyncio
    async def test_two_executions_weighted_average(self, db_session):
        """PARTIAL 2段階: exit_price が加重平均になること

        30株 @ 2600 + 70株 @ 2700 → 加重平均 = (30*2600 + 70*2700) / 100 = 2670.0
        """
        from trade_app.services.position_manager import PositionManager
        from trade_app.services.audit_logger import AuditLogger

        entry_order = _make_entry_order_filled(db_session)
        position = Position(
            order_id=entry_order.id,
            ticker="7203",
            side="buy",
            quantity=100,
            entry_price=2500.0,
            status=PositionStatus.CLOSING.value,
            exit_reason="tp_hit",
            remaining_qty=0,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(position)

        exit_order = Order(
            signal_id=None,
            is_exit_order=True,
            ticker="7203",
            order_type="market",
            side="sell",
            quantity=100,
            status=OrderStatus.FILLED.value,
            filled_quantity=100,
            filled_price=2700.0,  # ブローカーが返す最終価格（加重平均ではない可能性）
            filled_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(exit_order)
        await db_session.flush()

        exit_order.position_id = position.id
        await db_session.flush()

        # 2件の Execution
        db_session.add(Execution(
            order_id=exit_order.id, ticker="7203", side="sell",
            quantity=30, price=2600.0,
            executed_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
        ))
        db_session.add(Execution(
            order_id=exit_order.id, ticker="7203", side="sell",
            quantity=70, price=2700.0,
            executed_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
        ))
        await db_session.flush()

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)
        result = await pos_mgr.finalize_exit(position=position, exit_order=exit_order)

        expected_weighted = (30 * 2600 + 70 * 2700) / 100  # = 2670.0
        assert abs(position.exit_price - expected_weighted) < 0.01, (
            f"exit_price={position.exit_price} 期待値={expected_weighted}"
        )
        assert abs(result.exit_price - expected_weighted) < 0.01

    @pytest.mark.asyncio
    async def test_fallback_to_order_filled_price_when_no_executions(self, db_session):
        """Execution が存在しない場合 order.filled_price にフォールバックすること"""
        from trade_app.services.position_manager import PositionManager
        from trade_app.services.audit_logger import AuditLogger

        entry_order = _make_entry_order_filled(db_session)
        position = Position(
            order_id=entry_order.id,
            ticker="7203",
            side="buy",
            quantity=100,
            entry_price=2500.0,
            status=PositionStatus.CLOSING.value,
            exit_reason="tp_hit",
            remaining_qty=0,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(position)

        exit_order = Order(
            signal_id=None,
            is_exit_order=True,
            ticker="7203",
            order_type="market",
            side="sell",
            quantity=100,
            status=OrderStatus.FILLED.value,
            filled_quantity=100,
            filled_price=2650.0,
            filled_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(exit_order)
        await db_session.flush()
        exit_order.position_id = position.id
        await db_session.flush()

        # Execution は作らない

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)
        result = await pos_mgr.finalize_exit(position=position, exit_order=exit_order)

        # フォールバック: order.filled_price を使用
        assert position.exit_price == 2650.0
        assert result.exit_price == 2650.0
