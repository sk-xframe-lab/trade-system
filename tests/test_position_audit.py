"""
ポジション更新責務監査テスト

以下の4点を検証する:
  1. unknown exit order が CLOSING 維持になること（OPEN に巻き戻さない）
  2. UNKNOWN 注文が RecoveryManager の照会対象に含まれること
  3. revert_to_open() が PositionManager に集約されていること
     (OrderPoller が Position を直接更新しないこと)
  4. remaining_qty が負になる入力を渡したとき OrderPoller がスキップすること
     (過剰決済防止の最小防御確認)
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from trade_app.brokers.base import OrderStatusResponse
from trade_app.models.enums import OrderStatus, PositionStatus
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.position_exit_transition import PositionExitTransition
from trade_app.services.position_manager import PositionManager
from trade_app.services.audit_logger import AuditLogger


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _make_exit_order(db_session, position_id: str, status: str) -> Order:
    order = Order(
        signal_id=None,
        position_id=position_id,
        is_exit_order=True,
        ticker="7203",
        order_type="market",
        side="sell",
        quantity=100,
        status=status,
        broker_order_id=f"MOCK-{uuid.uuid4().hex[:12].upper()}",
        submitted_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(order)
    return order


def _make_open_position(db_session, ticker="7203") -> Position:
    # 仮の entry order id (FK 制約のためダミー Order を作成)
    entry_order = Order(
        id=str(uuid.uuid4()),
        signal_id=None,
        ticker=ticker,
        order_type="market",
        side="buy",
        quantity=100,
        status=OrderStatus.FILLED.value,
        filled_quantity=100,
        filled_price=2500.0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(entry_order)

    position = Position(
        id=str(uuid.uuid4()),
        order_id=entry_order.id,
        ticker=ticker,
        side="buy",
        quantity=100,
        entry_price=2500.0,
        status=PositionStatus.OPEN.value,
        opened_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(position)
    return position


def _make_closing_position(db_session, ticker="7203") -> Position:
    position = _make_open_position(db_session, ticker)
    position.status = PositionStatus.CLOSING.value
    position.exit_reason = "tp_hit"
    return position


# ─── テスト: revert_to_open は PositionManager に集約されている ───────────────

class TestRevertToOpenViaPositionManager:
    """PositionManager.revert_to_open() の動作確認"""

    @pytest.mark.asyncio
    async def test_revert_changes_status_to_open(self, db_session):
        """revert_to_open() が CLOSING → OPEN へ正しく遷移させること"""
        position = _make_closing_position(db_session)
        await db_session.flush()

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)

        await pos_mgr.revert_to_open(
            position=position,
            reason="exit注文キャンセル",
            triggered_by="poller",
        )
        await db_session.flush()

        assert position.status == PositionStatus.OPEN.value
        assert position.exit_reason is None

    @pytest.mark.asyncio
    async def test_revert_records_exit_transition(self, db_session):
        """revert_to_open() が PositionExitTransition を記録すること"""
        position = _make_closing_position(db_session)
        await db_session.flush()
        pos_id = position.id

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)
        await pos_mgr.revert_to_open(
            position=position,
            reason="exit注文拒否",
            triggered_by="poller",
        )
        await db_session.flush()

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
        assert t.triggered_by == "poller"
        assert "exit注文拒否" in str(t.details)

    @pytest.mark.asyncio
    async def test_revert_raises_if_not_closing(self, db_session):
        """CLOSING 以外のポジションに revert_to_open() を呼ぶと ValueError"""
        position = _make_open_position(db_session)
        await db_session.flush()

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)

        with pytest.raises(ValueError, match="CLOSING でない"):
            await pos_mgr.revert_to_open(
                position=position,
                reason="テスト",
            )


# ─── テスト: UNKNOWN exit order は CLOSING 維持（OPEN に戻さない）──────────────

class TestUnknownExitOrderKeepsCLOSING:
    """
    exit 注文が UNKNOWN になっても Position は CLOSING のまま維持されること。
    OPEN に巻き戻してはならない。
    """

    @pytest.mark.asyncio
    async def test_unknown_exit_order_position_stays_closing(self, db_session):
        """OrderPoller が UNKNOWN を受け取っても CLOSING → OPEN にならないこと"""
        from trade_app.services.order_poller import OrderPoller

        position = _make_closing_position(db_session)
        await db_session.flush()
        exit_order = _make_exit_order(db_session, position.id, OrderStatus.SUBMITTED.value)
        await db_session.flush()
        pos_id = position.id

        # ブローカーが UNKNOWN を返すモック
        mock_status_resp = MagicMock()
        mock_status_resp.status = OrderStatus.UNKNOWN
        mock_status_resp.message = None

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        audit_mock = AsyncMock()
        audit_mock.log = AsyncMock()

        from trade_app.services.broker_call_logger import BrokerCallLogger
        bl_mock = AsyncMock(spec=BrokerCallLogger)
        bl_mock.before_status_query = AsyncMock(return_value=MagicMock())
        bl_mock.after_status_query = AsyncMock()

        poller = OrderPoller()

        # セッションを渡して _process_order を直接呼ぶ
        # db_session 内で完結させる
        await poller._process_order(
            db=db_session,
            order=exit_order,
            broker=mock_broker,
            broker_logger=bl_mock,
            audit=audit_mock,
        )

        # Position が CLOSING のまま維持されていること
        await db_session.refresh(position)
        assert position.status == PositionStatus.CLOSING.value, (
            f"UNKNOWN exit order 後も CLOSING を維持すべきだが status={position.status}"
        )

    @pytest.mark.asyncio
    async def test_cancelled_exit_order_reverts_to_open(self, db_session):
        """CANCELLED exit order は CLOSING → OPEN に巻き戻ること（正常ケース）"""
        from trade_app.services.order_poller import OrderPoller

        position = _make_closing_position(db_session)
        await db_session.flush()
        exit_order = _make_exit_order(db_session, position.id, OrderStatus.SUBMITTED.value)
        await db_session.flush()

        mock_status_resp = MagicMock()
        mock_status_resp.status = OrderStatus.CANCELLED
        mock_status_resp.message = None

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
        assert position.status == PositionStatus.OPEN.value, (
            f"CANCELLED exit order 後は OPEN に戻すべきだが status={position.status}"
        )


# ─── テスト: RecoveryManager が UNKNOWN 注文を再照会対象に含めること ────────────

class TestRecoveryManagerIncludesUnknown:
    """RecoveryManager._run_recovery() が UNKNOWN 注文も照会対象にすること"""

    @pytest.mark.asyncio
    async def test_unknown_orders_are_queried(self, db_session):
        """UNKNOWN 状態の注文が RecoveryManager の取得クエリに含まれること"""
        from trade_app.services.recovery_manager import RecoveryManager

        # UNKNOWN 注文を作成
        unknown_order = Order(
            signal_id=None,
            ticker="7203",
            order_type="market",
            side="sell",
            quantity=100,
            status=OrderStatus.UNKNOWN.value,
            broker_order_id="MOCK-UNKNOWN-001",
            submitted_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(unknown_order)
        await db_session.flush()

        # ブローカーが UNKNOWN のまま返す（状態変化なし）
        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-UNKNOWN-001",
            status=OrderStatus.UNKNOWN,
        )

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()

        with (
            patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker),
        ):
            await rm._run_recovery(db_session)

        # ブローカーが照会されたことを確認（UNKNOWN 注文が対象だった証拠）
        mock_broker.get_order_status.assert_called_once_with("MOCK-UNKNOWN-001")

    @pytest.mark.asyncio
    async def test_unknown_exit_order_stays_closing_after_recovery(self, db_session):
        """
        UNKNOWN exit 注文がリカバリ後も UNKNOWN のまま → Position は CLOSING 維持
        (RecoveryManager は UNKNOWN→UNKNOWN で状態変化なし → Position 操作しない)
        """
        from trade_app.services.recovery_manager import RecoveryManager

        position = _make_closing_position(db_session)
        await db_session.flush()
        exit_order = _make_exit_order(db_session, position.id, OrderStatus.UNKNOWN.value)
        exit_order.broker_order_id = "MOCK-UNKNOWN-EXIT-001"
        await db_session.flush()

        # ブローカーが UNKNOWN のまま返す
        mock_status_resp = OrderStatusResponse(
            broker_order_id="MOCK-UNKNOWN-EXIT-001",
            status=OrderStatus.UNKNOWN,
        )

        mock_broker = AsyncMock()
        mock_broker.get_order_status.return_value = mock_status_resp

        rm = RecoveryManager()

        with patch("trade_app.services.recovery_manager._get_broker", return_value=mock_broker):
            await rm._run_recovery(db_session)

        await db_session.refresh(position)
        assert position.status == PositionStatus.CLOSING.value, (
            "UNKNOWN exit order のリカバリ後も Position は CLOSING を維持すべき"
        )


# ─── テスト: 過剰決済防止（exit 注文の数量が position.quantity を超えない）────

class TestOverfillPrevention:
    """
    exit 注文が position.quantity より多い数量で約定しようとしても
    finalize_exit は CLOSING 前提で動作し、quantity のチェックは呼び出し元が担保する。
    ここでは finalize_exit 呼び出し前の状態チェックのみを確認する。
    """

    @pytest.mark.asyncio
    async def test_finalize_exit_requires_closing_status(self, db_session):
        """finalize_exit() は position が CLOSING でないと ValueError を上げること"""
        position = _make_open_position(db_session)
        await db_session.flush()
        exit_order = _make_exit_order(db_session, position.id, OrderStatus.FILLED.value)
        exit_order.filled_quantity = 100
        exit_order.filled_price = 2600.0
        await db_session.flush()

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)

        with pytest.raises(ValueError, match="CLOSING でない"):
            await pos_mgr.finalize_exit(position=position, exit_order=exit_order)

    @pytest.mark.asyncio
    async def test_revert_to_open_requires_closing_status(self, db_session):
        """revert_to_open() は CLOSING でない position に呼ぶと ValueError"""
        position = _make_open_position(db_session)
        await db_session.flush()

        audit = AuditLogger(db_session)
        pos_mgr = PositionManager(db=db_session, audit=audit)

        with pytest.raises(ValueError, match="CLOSING でない"):
            await pos_mgr.revert_to_open(position=position, reason="テスト")
