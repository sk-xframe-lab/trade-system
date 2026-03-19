"""
ポジション exit フローのインテグレーションテスト

検証内容:
  A. exit PARTIAL 約定
     - 100株中30株約定 → remaining_qty=70, status=CLOSING
     - さらに70株約定 → remaining_qty=0, status=CLOSED
     - 過剰約定 → remaining_qty が負にならないこと
  B. RecoveryManager exit FILLED
     - UNKNOWN exit order が FILLED になったとき finalize 相当が走ること
     - PARTIAL済み exit order が FILLED になっても二重計上しないこと
     - broker_execution_id 重複時に Execution が増えないこと
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
from trade_app.models.position_exit_transition import PositionExitTransition
from trade_app.models.signal import TradeSignal
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.position_manager import PositionManager


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _make_entry_order(db_session, ticker="7203", qty=100) -> Order:
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


def _make_open_position(db_session, ticker="7203", qty=100) -> Position:
    entry_order = _make_entry_order(db_session, ticker=ticker, qty=qty)
    position = Position(
        id=str(uuid.uuid4()),
        order_id=entry_order.id,
        ticker=ticker,
        side="buy",
        quantity=qty,
        entry_price=2500.0,
        status=PositionStatus.OPEN.value,
        opened_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(position)
    return position


async def _initiate_exit(db_session, position, qty) -> Order:
    """テスト用: CLOSING 状態に遷移させた exit 注文を返す"""
    exit_order = Order(
        signal_id=None,
        position_id=position.id,
        is_exit_order=True,
        ticker=position.ticker,
        order_type="market",
        side="sell",
        quantity=qty,
        status=OrderStatus.SUBMITTED.value,
        broker_order_id=f"MOCK-EXIT-{uuid.uuid4().hex[:8].upper()}",
        filled_quantity=0,
        submitted_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(exit_order)
    position.status = PositionStatus.CLOSING.value
    position.exit_reason = "tp_hit"
    position.remaining_qty = position.quantity
    position.updated_at = datetime.now(timezone.utc)
    await db_session.flush()
    return exit_order


# ─── A. exit PARTIAL 約定 ─────────────────────────────────────────────────────

class TestExitPartialFill:
    """exit 注文の PARTIAL 約定 → CLOSING 維持 → 全量約定 → CLOSED"""

    @pytest.mark.asyncio
    async def test_partial_fill_keeps_closing(self, db_session):
        """30/100 株約定後は remaining_qty=70 で CLOSING を維持すること"""
        position = _make_open_position(db_session)
        await db_session.flush()
        exit_order = await _initiate_exit(db_session, position, qty=100)

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)

        is_flat = await pos_mgr.apply_exit_execution(
            position=position,
            executed_qty=30,
            executed_price=2600.0,
        )

        assert is_flat is False
        assert position.remaining_qty == 70
        assert position.status == PositionStatus.CLOSING.value

    @pytest.mark.asyncio
    async def test_second_fill_closes_position(self, db_session):
        """30+70=100株の2段階約定後に status=CLOSED になること"""
        position = _make_open_position(db_session)
        await db_session.flush()
        exit_order = await _initiate_exit(db_session, position, qty=100)
        exit_order.filled_quantity = 30  # 既に30株約定済み

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)

        # 1回目: 30株
        await pos_mgr.apply_exit_execution(position=position, executed_qty=30, executed_price=2600.0)
        assert position.remaining_qty == 70

        # 2回目: 70株 → フラット
        exit_order.status = OrderStatus.FILLED.value
        exit_order.filled_quantity = 100
        exit_order.filled_price = 2610.0
        exit_order.filled_at = datetime.now(timezone.utc)
        await db_session.flush()

        is_flat = await pos_mgr.apply_exit_execution(position=position, executed_qty=70, executed_price=2620.0)

        assert is_flat is True
        assert position.remaining_qty == 0

        # finalize_exit を呼び出して CLOSED に遷移
        await pos_mgr.finalize_exit(position=position, exit_order=exit_order)
        assert position.status == PositionStatus.CLOSED.value

    @pytest.mark.asyncio
    async def test_overfill_clamps_to_zero(self, db_session):
        """過剰約定(110株) で remaining_qty が負にならないこと"""
        position = _make_open_position(db_session)
        await db_session.flush()
        await _initiate_exit(db_session, position, qty=100)

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)

        # 110株の約定（過剰）
        is_flat = await pos_mgr.apply_exit_execution(
            position=position,
            executed_qty=110,
            executed_price=2600.0,
        )

        assert position.remaining_qty == 0  # クランプされていること
        assert position.remaining_qty >= 0  # 負にならない
        assert is_flat is True

    @pytest.mark.asyncio
    async def test_apply_exit_execution_raises_if_not_closing(self, db_session):
        """CLOSING でないポジションに apply_exit_execution を呼ぶと ValueError"""
        position = _make_open_position(db_session)
        await db_session.flush()

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)

        with pytest.raises(ValueError, match="CLOSING でない"):
            await pos_mgr.apply_exit_execution(
                position=position,
                executed_qty=50,
                executed_price=2600.0,
            )

    @pytest.mark.asyncio
    async def test_remaining_qty_initialized_in_initiate_exit(self, db_session):
        """initiate_exit() 後に remaining_qty = quantity で初期化されること"""
        position = _make_open_position(db_session, qty=200)
        await db_session.flush()
        assert position.remaining_qty is None  # 初期は未設定

        exit_order = await _initiate_exit(db_session, position, qty=200)

        assert position.remaining_qty == 200

    @pytest.mark.asyncio
    async def test_partial_exit_via_order_poller_handle_partial(self, db_session):
        """OrderPoller._handle_partial() が exit 注文の場合に remaining_qty を更新すること"""
        from trade_app.services.order_poller import OrderPoller

        position = _make_open_position(db_session)
        await db_session.flush()
        exit_order = await _initiate_exit(db_session, position, qty=100)

        # ブローカーが PARTIAL (50株) を返すモック
        mock_status_resp = MagicMock()
        mock_status_resp.status = OrderStatus.PARTIAL
        mock_status_resp.filled_quantity = 50
        mock_status_resp.filled_price = 2600.0
        mock_status_resp.broker_execution_id = f"EXEC-{uuid.uuid4().hex[:8]}"

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        audit_mock = AsyncMock()
        audit_mock.log = AsyncMock()

        from trade_app.services.broker_call_logger import BrokerCallLogger
        bl_mock = AsyncMock(spec=BrokerCallLogger)
        bl_mock.before_status_query = AsyncMock(return_value=MagicMock())
        bl_mock.after_status_query = AsyncMock()

        poller = OrderPoller()
        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=mock_broker,
            broker_logger=bl_mock,
            audit=audit_mock,
        )

        await db_session.refresh(position)
        assert position.status == PositionStatus.CLOSING.value
        assert position.remaining_qty == 50  # 100 - 50 = 50

    @pytest.mark.asyncio
    async def test_full_exit_via_order_poller_handle_filled(self, db_session):
        """OrderPoller が exit FILLED を検出して CLOSED に遷移させること"""
        from trade_app.services.order_poller import OrderPoller

        position = _make_open_position(db_session)
        await db_session.flush()
        exit_order = await _initiate_exit(db_session, position, qty=100)

        mock_status_resp = MagicMock()
        mock_status_resp.status = OrderStatus.FILLED
        mock_status_resp.filled_quantity = 100
        mock_status_resp.filled_price = 2600.0
        mock_status_resp.broker_execution_id = f"EXEC-{uuid.uuid4().hex[:8]}"

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        audit_mock = AsyncMock()
        audit_mock.log = AsyncMock()

        from trade_app.services.broker_call_logger import BrokerCallLogger
        bl_mock = AsyncMock(spec=BrokerCallLogger)
        bl_mock.before_status_query = AsyncMock(return_value=MagicMock())
        bl_mock.after_status_query = AsyncMock()

        poller = OrderPoller()
        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=mock_broker,
            broker_logger=bl_mock,
            audit=audit_mock,
        )

        await db_session.refresh(position)
        assert position.status == PositionStatus.CLOSED.value
        assert position.remaining_qty == 0


# ─── B. RecoveryManager exit FILLED ─────────────────────────────────────────

class TestRecoveryExitFilled:
    """RecoveryManager で exit 注文が FILLED と判明した場合のフロー"""

    @pytest.mark.asyncio
    async def test_unknown_exit_filled_in_recovery_closes_position(self, db_session):
        """UNKNOWN だった exit 注文が recovery で FILLED → finalize_exit が走ること"""
        from trade_app.services.recovery_manager import RecoveryManager

        position = _make_open_position(db_session)
        await db_session.flush()
        exit_order = await _initiate_exit(db_session, position, qty=100)
        exit_order.status = OrderStatus.UNKNOWN.value
        exit_order.broker_order_id = "MOCK-RECOVERY-001"
        await db_session.flush()

        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-RECOVERY-001",
            status=OrderStatus.FILLED,
            filled_quantity=100,
            filled_price=2600.0,
        )
        mock_status_resp.broker_execution_id = f"EXEC-REC-{uuid.uuid4().hex[:8]}"

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()
        with patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker):
            await rm._run_recovery(db_session)

        await db_session.refresh(position)
        assert position.status == PositionStatus.CLOSED.value

    @pytest.mark.asyncio
    async def test_partial_then_filled_recovery_no_double_execution(self, db_session):
        """PARTIAL済み exit order が recovery で FILLED になっても Execution が二重作成されないこと"""
        from trade_app.services.recovery_manager import RecoveryManager

        position = _make_open_position(db_session)
        await db_session.flush()
        exit_order = await _initiate_exit(db_session, position, qty=100)

        # 既に30株分の Execution が存在
        exec_id = f"EXEC-PARTIAL-{uuid.uuid4().hex[:8]}"
        existing_exec = Execution(
            order_id=exit_order.id,
            broker_execution_id=exec_id,
            ticker="7203",
            side="sell",
            quantity=30,
            price=2600.0,
            executed_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing_exec)
        exit_order.status = OrderStatus.PARTIAL.value
        exit_order.filled_quantity = 30
        exit_order.broker_order_id = "MOCK-RECOVERY-002"
        position.remaining_qty = 70  # 30株分は適用済み
        await db_session.flush()

        # broker が FILLED を返す（新しい execution ID）
        new_exec_id = f"EXEC-FINAL-{uuid.uuid4().hex[:8]}"
        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-RECOVERY-002",
            status=OrderStatus.FILLED,
            filled_quantity=100,
            filled_price=2610.0,
        )
        mock_status_resp.broker_execution_id = new_exec_id

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()
        with patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker):
            await rm._run_recovery(db_session)

        await db_session.refresh(position)
        assert position.status == PositionStatus.CLOSED.value

        # Execution は2件（30株 + 70株 delta）
        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == exit_order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 2
        qtys = sorted(e.quantity for e in executions)
        assert qtys == [30, 70]

    @pytest.mark.asyncio
    async def test_duplicate_broker_exec_id_no_new_execution(self, db_session):
        """broker_execution_id が重複する場合 Execution が新規作成されないこと"""
        from trade_app.services.recovery_manager import RecoveryManager

        position = _make_open_position(db_session)
        await db_session.flush()
        exit_order = await _initiate_exit(db_session, position, qty=100)

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
        exit_order.status = OrderStatus.UNKNOWN.value
        exit_order.filled_quantity = 100
        exit_order.broker_order_id = "MOCK-RECOVERY-003"
        # remaining_qty を手動で 0 に設定（重複検出後のフロー検証）
        position.remaining_qty = 0
        await db_session.flush()

        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-RECOVERY-003",
            status=OrderStatus.FILLED,
            filled_quantity=100,
            filled_price=2600.0,
        )
        mock_status_resp.broker_execution_id = dup_exec_id  # 重複 ID

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()
        with patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker):
            await rm._run_recovery(db_session)

        # Execution が増えていないこと
        exec_result = await db_session.execute(
            select(Execution).where(Execution.order_id == exit_order.id)
        )
        executions = exec_result.scalars().all()
        assert len(executions) == 1, f"Execution が重複作成された: {len(executions)} 件"
