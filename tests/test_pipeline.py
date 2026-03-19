"""
SignalPipeline のテスト

処理フロー全体（リスクチェック → 発注 → SUBMITTED まで）を
インメモリ SQLite + MockBroker で検証する。
OrderPoller が担当する約定確認はここでは検証しない。
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from trade_app.models.enums import OrderStatus, SignalStatus
from trade_app.models.order import Order
from trade_app.models.order_state_transition import OrderStateTransition
from trade_app.models.signal import TradeSignal
from trade_app.services.signal_strategy_gate import SignalStrategyGateResult


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

_GATE_PASS = AsyncMock(return_value=SignalStrategyGateResult(
    entry_allowed=True,
    size_ratio=1.0,
    blocking_reasons=[],
    matched_strategy_codes=[],
    decision_ids=[],
    evaluation_time=datetime.now(timezone.utc),
    signal_direction="long",
))


def _make_plan_pass(quantity: int = 100):
    """Planning Layer の通過モックを返す"""
    from trade_app.models.signal_plan import SignalPlan
    plan = MagicMock(spec=SignalPlan)
    plan.planned_order_qty = quantity
    plan.planning_status = "accepted"
    return AsyncMock(return_value=plan)


def _make_signal(db_session, ticker="7203", quantity=100, limit_price=2500.0) -> TradeSignal:
    """テスト用シグナルを作成して DB に保存する"""
    signal = TradeSignal(
        idempotency_key=str(uuid.uuid4()),
        source_system="test",
        ticker=ticker,
        signal_type="entry",
        order_type="limit",
        side="buy",
        quantity=quantity,
        limit_price=limit_price,
        status=SignalStatus.RECEIVED.value,
        generated_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(signal)
    return signal


# ─── テスト ────────────────────────────────────────────────────────────────────

class TestSignalPipeline:
    """SignalPipeline._run() の直接テスト（DB セッション注入版）"""

    @pytest.mark.asyncio
    async def test_normal_flow_reaches_submitted(self, db_session, mock_redis):
        """
        正常フロー: シグナル → リスクチェック通過 → SUBMITTED まで到達
        """
        from trade_app.services.pipeline import SignalPipeline
        from trade_app.brokers.mock_broker import MockBrokerAdapter, FillBehavior

        signal = _make_signal(db_session)
        await db_session.flush()
        signal_id = signal.id

        broker = MockBrokerAdapter(default_behavior=FillBehavior.IMMEDIATE)

        with (
            patch("trade_app.services.pipeline._get_broker", return_value=broker),
            patch("trade_app.services.pipeline._get_redis", return_value=mock_redis),
            patch(
                "trade_app.services.risk_manager.RiskManager._check_market_hours",
                return_value=None,
            ),
            patch(
                "trade_app.services.signal_strategy_gate.SignalStrategyGate.check",
                _GATE_PASS,
            ),
            patch(
                "trade_app.services.planning.service.SignalPlanningService.plan",
                _make_plan_pass(quantity=100),
            ),
        ):
            await SignalPipeline._run(db_session, signal_id)

        # シグナルは PROCESSING 以降になっているはず
        result = await db_session.execute(
            select(TradeSignal).where(TradeSignal.id == signal_id)
        )
        sig = result.scalar_one()
        assert sig.status in (SignalStatus.PROCESSING.value, SignalStatus.EXECUTED.value)

        # Order が作成されていること
        order_result = await db_session.execute(
            select(Order).where(Order.signal_id == signal_id)
        )
        order = order_result.scalar_one()
        assert order.status in (
            OrderStatus.SUBMITTED.value,
            OrderStatus.FILLED.value,  # IMMEDIATE の場合 mock が即時約定する可能性
        )
        assert order.broker_order_id is not None
        assert order.broker_order_id.startswith("MOCK-")

    @pytest.mark.asyncio
    async def test_state_transitions_recorded(self, db_session, mock_redis):
        """
        状態遷移が order_state_transitions テーブルに記録されること
        """
        from trade_app.services.pipeline import SignalPipeline
        from trade_app.brokers.mock_broker import MockBrokerAdapter, FillBehavior

        signal = _make_signal(db_session, ticker="6758")
        await db_session.flush()
        signal_id = signal.id

        broker = MockBrokerAdapter(default_behavior=FillBehavior.NEVER_FILL)

        with (
            patch("trade_app.services.pipeline._get_broker", return_value=broker),
            patch("trade_app.services.pipeline._get_redis", return_value=mock_redis),
            patch(
                "trade_app.services.risk_manager.RiskManager._check_market_hours",
                return_value=None,
            ),
            patch(
                "trade_app.services.signal_strategy_gate.SignalStrategyGate.check",
                _GATE_PASS,
            ),
            patch(
                "trade_app.services.planning.service.SignalPlanningService.plan",
                _make_plan_pass(quantity=100),
            ),
        ):
            await SignalPipeline._run(db_session, signal_id)

        # Order を特定
        order_result = await db_session.execute(
            select(Order).where(Order.signal_id == signal_id)
        )
        order = order_result.scalar_one()

        # 遷移レコードを確認
        trans_result = await db_session.execute(
            select(OrderStateTransition)
            .where(OrderStateTransition.order_id == order.id)
            .order_by(OrderStateTransition.created_at)
        )
        transitions = trans_result.scalars().all()
        assert len(transitions) >= 2, "PENDING + SUBMITTED の遷移が最低2件必要"

        # 最初の遷移: None → PENDING
        first = transitions[0]
        assert first.from_status is None
        assert first.to_status == OrderStatus.PENDING.value
        assert first.triggered_by == "pipeline"

        # 2番目の遷移: PENDING → SUBMITTED
        second = transitions[1]
        assert second.from_status == OrderStatus.PENDING.value
        assert second.to_status == OrderStatus.SUBMITTED.value

    @pytest.mark.asyncio
    async def test_risk_rejected_signal_becomes_rejected(self, db_session, mock_redis):
        """
        リスクチェック失敗 → シグナルが REJECTED になること
        """
        from trade_app.services.pipeline import SignalPipeline
        from trade_app.services.risk_manager import RiskRejectedError
        from trade_app.brokers.mock_broker import MockBrokerAdapter

        signal = _make_signal(db_session, ticker="9984", limit_price=50000.0, quantity=1000)
        await db_session.flush()
        signal_id = signal.id

        broker = MockBrokerAdapter()

        with (
            patch("trade_app.services.pipeline._get_broker", return_value=broker),
            patch("trade_app.services.pipeline._get_redis", return_value=mock_redis),
            patch(
                "trade_app.services.risk_manager.RiskManager._check_market_hours",
                return_value=None,
            ),
            patch(
                "trade_app.services.risk_manager.RiskManager._check_position_size",
                side_effect=RiskRejectedError("発注金額超過"),
            ),
            patch(
                "trade_app.services.signal_strategy_gate.SignalStrategyGate.check",
                _GATE_PASS,
            ),
            patch(
                "trade_app.services.planning.service.SignalPlanningService.plan",
                _make_plan_pass(quantity=1000),
            ),
        ):
            await SignalPipeline._run(db_session, signal_id)

        result = await db_session.execute(
            select(TradeSignal).where(TradeSignal.id == signal_id)
        )
        sig = result.scalar_one()
        assert sig.status == SignalStatus.REJECTED.value
        assert "発注金額超過" in (sig.reject_reason or "")

    @pytest.mark.asyncio
    async def test_broker_rejection_marks_order_rejected(self, db_session, mock_redis):
        """
        ブローカーが発注拒否 → Order が REJECTED になること
        """
        from trade_app.services.pipeline import SignalPipeline
        from trade_app.brokers.mock_broker import MockBrokerAdapter, FillBehavior

        signal = _make_signal(db_session, ticker="7267")
        await db_session.flush()
        signal_id = signal.id

        broker = MockBrokerAdapter(default_behavior=FillBehavior.REJECT_IMMEDIATELY)

        with (
            patch("trade_app.services.pipeline._get_broker", return_value=broker),
            patch("trade_app.services.pipeline._get_redis", return_value=mock_redis),
            patch(
                "trade_app.services.risk_manager.RiskManager._check_market_hours",
                return_value=None,
            ),
            patch(
                "trade_app.services.signal_strategy_gate.SignalStrategyGate.check",
                _GATE_PASS,
            ),
            patch(
                "trade_app.services.planning.service.SignalPlanningService.plan",
                _make_plan_pass(quantity=100),
            ),
        ):
            await SignalPipeline._run(db_session, signal_id)

        order_result = await db_session.execute(
            select(Order).where(Order.signal_id == signal_id)
        )
        order = order_result.scalar_one_or_none()
        # REJECT_IMMEDIATELY の場合 broker_order_id が空なので Order が REJECTED
        assert order is not None
        assert order.status == OrderStatus.REJECTED.value

    @pytest.mark.asyncio
    async def test_planning_rejected_signal_becomes_rejected(self, db_session, mock_redis):
        """
        Planning 拒否 → シグナルが REJECTED になること
        - SignalPlanRejectedError が送出されても pipeline が異常終了しない
        - signal.status が REJECTED になる
        - reject_reason に planning の理由コードが反映される
        - broker 発注に進まない（Order が作成されない）
        """
        from trade_app.services.pipeline import SignalPipeline
        from trade_app.services.planning.service import SignalPlanRejectedError
        from trade_app.services.planning.reasons import PlanningReasonCode
        from trade_app.brokers.mock_broker import MockBrokerAdapter

        signal = _make_signal(db_session)
        await db_session.flush()
        signal_id = signal.id

        broker = MockBrokerAdapter()
        plan_reject = AsyncMock(side_effect=SignalPlanRejectedError(
            plan_id=str(uuid.uuid4()),
            reason_code=PlanningReasonCode.MARKET_NOT_TRADABLE,
            detail="市場が取引不可状態です",
        ))

        with (
            patch("trade_app.services.pipeline._get_broker", return_value=broker),
            patch("trade_app.services.pipeline._get_redis", return_value=mock_redis),
            patch(
                "trade_app.services.risk_manager.RiskManager._check_market_hours",
                return_value=None,
            ),
            patch(
                "trade_app.services.signal_strategy_gate.SignalStrategyGate.check",
                _GATE_PASS,
            ),
            patch(
                "trade_app.services.planning.service.SignalPlanningService.plan",
                plan_reject,
            ),
        ):
            # 例外なく完了すること
            await SignalPipeline._run(db_session, signal_id)

        # signal が REJECTED になっていること
        result = await db_session.execute(
            select(TradeSignal).where(TradeSignal.id == signal_id)
        )
        sig = result.scalar_one()
        assert sig.status == SignalStatus.REJECTED.value
        assert "market_not_tradable" in (sig.reject_reason or "")

        # broker 発注に進んでいないこと（Order が作成されていない）
        order_result = await db_session.execute(
            select(Order).where(Order.signal_id == signal_id)
        )
        assert order_result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_planning_rejected_reason_detail_saved(self, db_session, mock_redis):
        """
        Planning 拒否時に reject_reason に detail が含まれること
        """
        from trade_app.services.pipeline import SignalPipeline
        from trade_app.services.planning.service import SignalPlanRejectedError
        from trade_app.services.planning.reasons import PlanningReasonCode
        from trade_app.brokers.mock_broker import MockBrokerAdapter

        signal = _make_signal(db_session)
        await db_session.flush()
        signal_id = signal.id

        plan_reject = AsyncMock(side_effect=SignalPlanRejectedError(
            plan_id=str(uuid.uuid4()),
            reason_code=PlanningReasonCode.PLANNED_SIZE_ZERO,
            detail="planned_qty=0 after lot rounding (lot_size=100)",
        ))

        with (
            patch("trade_app.services.pipeline._get_broker", return_value=MockBrokerAdapter()),
            patch("trade_app.services.pipeline._get_redis", return_value=mock_redis),
            patch(
                "trade_app.services.signal_strategy_gate.SignalStrategyGate.check",
                _GATE_PASS,
            ),
            patch(
                "trade_app.services.planning.service.SignalPlanningService.plan",
                plan_reject,
            ),
        ):
            await SignalPipeline._run(db_session, signal_id)

        result = await db_session.execute(
            select(TradeSignal).where(TradeSignal.id == signal_id)
        )
        sig = result.scalar_one()
        assert sig.status == SignalStatus.REJECTED.value
        assert "planned_size_zero" in (sig.reject_reason or "")
        assert "lot rounding" in (sig.reject_reason or "")

    @pytest.mark.asyncio
    async def test_duplicate_signal_skipped(self, db_session, mock_redis):
        """
        既に PROCESSING 以降のシグナルは処理をスキップすること
        """
        from trade_app.services.pipeline import SignalPipeline
        from trade_app.brokers.mock_broker import MockBrokerAdapter

        signal = _make_signal(db_session)
        signal.status = SignalStatus.EXECUTED.value  # 既に処理済み
        await db_session.flush()
        signal_id = signal.id

        broker = MockBrokerAdapter()

        with (
            patch("trade_app.services.pipeline._get_broker", return_value=broker),
            patch("trade_app.services.pipeline._get_redis", return_value=mock_redis),
        ):
            await SignalPipeline._run(db_session, signal_id)

        # Order は作成されていないはず
        order_result = await db_session.execute(
            select(Order).where(Order.signal_id == signal_id)
        )
        order = order_result.scalar_one_or_none()
        assert order is None, "既処理シグナルに対して Order が作成されてはいけない"
