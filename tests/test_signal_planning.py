"""
tests/test_signal_planning.py — Signal Planning Layer テスト

テスト対象:
  - BaseSizer (sizer.py)
  - LiquidityAdjuster / SpreadAdjuster / VolatilityAdjuster (adjusters.py)
  - ExecutionParamsBuilder (execution_params.py)
  - SignalPlanningService (service.py)
  - pipeline 統合（planning → risk → order）
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from trade_app.models.signal import TradeSignal
from trade_app.models.signal_plan import SignalPlan
from trade_app.models.signal_plan_reason import SignalPlanReason
from trade_app.models.signal_strategy_decision import SignalStrategyDecision
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.planning.adjusters import (
    LiquidityAdjuster,
    MarketTradabilityChecker,
    SpreadAdjuster,
    VolatilityAdjuster,
)
from trade_app.services.planning.context import PlannerContext, PlannerContextBuilder
from trade_app.services.planning.execution_params import ExecutionParamsBuilder
from trade_app.services.planning.reasons import PlanningReasonCode, PlanningStatus
from trade_app.services.planning.service import SignalPlanRejectedError, SignalPlanningService
from trade_app.services.planning.sizer import BaseSizer

_NOW = datetime.now(timezone.utc)


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _make_signal(
    quantity: int = 1000,
    signal_type: str = "entry",
    order_type: str = "limit",
    limit_price: float | None = 1000.0,
    ticker: str = "7203",
    side: str = "buy",
) -> TradeSignal:
    return TradeSignal(
        id=str(uuid.uuid4()),
        idempotency_key=str(uuid.uuid4()),
        source_system="test",
        ticker=ticker,
        signal_type=signal_type,
        order_type=order_type,
        side=side,
        quantity=quantity,
        limit_price=limit_price,
        stop_price=None,
        generated_at=_NOW,
        received_at=_NOW,
        status="received",
    )


def _make_context(
    signal: TradeSignal | None = None,
    size_ratio: float = 1.0,
    ssd_id: str | None = None,
    decision_time: datetime | None = None,
    is_market_tradable: bool = True,
    is_symbol_tradable: bool = True,
    symbol_lot_size: int = 100,
    market_price: float | None = None,
    spread_bps: float = 0.0,
    volume_ratio: float = 1.0,
    atr: float | None = None,
    volatility: float | None = None,
) -> PlannerContext:
    sig = signal or _make_signal()
    return PlannerContext(
        signal=sig,
        size_ratio=size_ratio,
        signal_strategy_decision_id=ssd_id or str(uuid.uuid4()),
        decision_evaluation_time=decision_time or _NOW,
        is_market_tradable=is_market_tradable,
        is_symbol_tradable=is_symbol_tradable,
        symbol_lot_size=symbol_lot_size,
        market_price=market_price,
        spread_bps=spread_bps,
        volume_ratio=volume_ratio,
        atr=atr,
        volatility=volatility,
    )


# ─── 1. BaseSizer ──────────────────────────────────────────────────────────────

class TestBaseSizer:
    def test_size_ratio_100_percent(self):
        """size_ratio=1.0 のとき base_qty がそのまま返る"""
        sizer = BaseSizer()
        result = sizer.calculate(1000, 1.0)
        assert result.base_qty == 1000
        assert result.after_ratio_qty == 1000
        assert result.applied_size_ratio == 1.0

    def test_size_ratio_50_percent(self):
        """size_ratio=0.5 のとき half に切り捨て"""
        sizer = BaseSizer()
        result = sizer.calculate(1000, 0.5)
        assert result.after_ratio_qty == 500

    def test_size_ratio_clamped_above_1(self):
        """size_ratio > 1.0 は 1.0 にクランプ（増量なし）"""
        sizer = BaseSizer()
        result = sizer.calculate(1000, 2.0)
        assert result.after_ratio_qty == 1000
        assert result.applied_size_ratio == 1.0

    def test_size_ratio_zero(self):
        """size_ratio=0.0 のとき 0 になる"""
        sizer = BaseSizer()
        result = sizer.calculate(1000, 0.0)
        assert result.after_ratio_qty == 0

    def test_round_to_lot_floor(self):
        """lot 丸め: 150 → 100 (lot_size=100)"""
        sizer = BaseSizer()
        assert sizer.round_to_lot(150, 100) == 100

    def test_round_to_lot_below_min(self):
        """lot 丸め: 50 → 0 (lot_size=100)"""
        sizer = BaseSizer()
        assert sizer.round_to_lot(50, 100) == 0

    def test_round_to_lot_exact(self):
        """lot 丸め: ちょうど倍数はそのまま"""
        sizer = BaseSizer()
        assert sizer.round_to_lot(200, 100) == 200


# ─── 2. LiquidityAdjuster ─────────────────────────────────────────────────────

class TestLiquidityAdjuster:
    def test_normal_volume_no_adjustment(self):
        """volume_ratio >= 0.3 → 調整なし"""
        adj = LiquidityAdjuster()
        ctx = _make_context(volume_ratio=1.0)
        result = adj.adjust(1000, ctx)
        assert result.output_qty == 1000
        assert result.ratio_applied == 1.0
        assert result.reason_code is None

    def test_low_volume_50_percent(self):
        """volume_ratio=0.2 (0.1〜0.3) → 50% に縮小"""
        adj = LiquidityAdjuster()
        ctx = _make_context(volume_ratio=0.2)
        result = adj.adjust(1000, ctx)
        assert result.output_qty == 500
        assert result.ratio_applied == 0.5
        assert result.reason_code == PlanningReasonCode.LIQUIDITY_REDUCTION

    def test_very_low_volume_25_percent(self):
        """volume_ratio=0.05 (< 0.1) → 25% に縮小"""
        adj = LiquidityAdjuster()
        ctx = _make_context(volume_ratio=0.05)
        result = adj.adjust(1000, ctx)
        assert result.output_qty == 250
        assert result.ratio_applied == 0.25


# ─── 3. SpreadAdjuster ───────────────────────────────────────────────────────

class TestSpreadAdjuster:
    def test_normal_spread_no_adjustment(self):
        """spread_bps=20 → 調整なし"""
        adj = SpreadAdjuster()
        ctx = _make_context(spread_bps=20.0)
        result = adj.adjust(1000, ctx)
        assert result.output_qty == 1000
        assert result.reason_code is None

    def test_wide_spread_reduction(self):
        """spread_bps=60 (>= 50) → 50% 縮小"""
        adj = SpreadAdjuster()
        ctx = _make_context(spread_bps=60.0)
        result = adj.adjust(1000, ctx)
        assert result.output_qty == 500
        assert result.reason_code == PlanningReasonCode.SPREAD_REDUCTION

    def test_too_wide_spread_reject(self):
        """spread_bps=120 (>= 100) → reject"""
        adj = SpreadAdjuster()
        ctx = _make_context(spread_bps=120.0)
        result = adj.adjust(1000, ctx)
        assert result.rejected is True
        assert result.output_qty == 0
        assert result.reason_code == PlanningReasonCode.SPREAD_TOO_WIDE

    def test_zero_spread_no_adjustment(self):
        """spread_bps=0 → データなしとして通過"""
        adj = SpreadAdjuster()
        ctx = _make_context(spread_bps=0.0)
        result = adj.adjust(1000, ctx)
        assert result.output_qty == 1000


# ─── 4. VolatilityAdjuster ───────────────────────────────────────────────────

class TestVolatilityAdjuster:
    def test_atr_high_reduces_size(self):
        """ATR/price > 3% → 50% 縮小"""
        adj = VolatilityAdjuster()
        sig = _make_signal(limit_price=1000.0)
        ctx = _make_context(signal=sig, atr=40.0, market_price=1000.0)
        result = adj.adjust(1000, ctx)
        assert result.output_qty == 500
        assert result.reason_code == PlanningReasonCode.ATR_REDUCTION

    def test_volatility_high_reduces_size(self):
        """volatility > 4% → 50% 縮小（ATR なし）"""
        adj = VolatilityAdjuster()
        ctx = _make_context(volatility=0.05)
        result = adj.adjust(1000, ctx)
        assert result.output_qty == 500
        assert result.reason_code == PlanningReasonCode.VOLATILITY_REDUCTION

    def test_no_data_no_adjustment(self):
        """ATR / volatility ともに None → 調整なし"""
        adj = VolatilityAdjuster()
        ctx = _make_context()
        result = adj.adjust(1000, ctx)
        assert result.output_qty == 1000
        assert result.reason_code is None


# ─── 5. ExecutionParamsBuilder ───────────────────────────────────────────────

class TestExecutionParamsBuilder:
    def test_market_order_sets_slippage(self):
        """market 発注 → max_slippage_bps が設定される"""
        builder = ExecutionParamsBuilder()
        sig = _make_signal(order_type="market")
        ctx = _make_context(signal=sig)
        params = builder.build(ctx)
        assert params.order_type_candidate == "market"
        assert params.max_slippage_bps == ExecutionParamsBuilder.DEFAULT_SLIPPAGE_BPS

    def test_limit_order_no_slippage(self):
        """limit 発注 → max_slippage_bps は None"""
        builder = ExecutionParamsBuilder()
        ctx = _make_context()
        params = builder.build(ctx)
        assert params.order_type_candidate == "limit"
        assert params.max_slippage_bps is None

    def test_low_liquidity_lower_participation_cap(self):
        """volume_ratio < 0.3 → LOW_LIQUIDITY_PARTICIPATION_CAP"""
        builder = ExecutionParamsBuilder()
        ctx = _make_context(volume_ratio=0.2)
        params = builder.build(ctx)
        assert params.participation_rate_cap == ExecutionParamsBuilder.LOW_LIQUIDITY_PARTICIPATION_CAP

    def test_normal_liquidity_normal_participation_cap(self):
        """volume_ratio >= 0.3 → NORMAL_PARTICIPATION_CAP"""
        builder = ExecutionParamsBuilder()
        ctx = _make_context(volume_ratio=1.0)
        params = builder.build(ctx)
        assert params.participation_rate_cap == ExecutionParamsBuilder.NORMAL_PARTICIPATION_CAP

    def test_entry_timeout_default(self):
        """entry_timeout_seconds は DEFAULT_TIMEOUT_SEC (300)"""
        builder = ExecutionParamsBuilder()
        ctx = _make_context()
        params = builder.build(ctx)
        assert params.entry_timeout_seconds == ExecutionParamsBuilder.DEFAULT_TIMEOUT_SEC


# ─── 6. SignalPlanningService ─────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSignalPlanningService:
    async def test_accepted_plan(self, db_session):
        """正常シグナル → ACCEPTED、signal_plans に保存される"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal(quantity=500)
        db_session.add(signal)
        ctx = _make_context(signal=signal)

        plan = await service.plan(signal, ctx)

        assert plan.planning_status == PlanningStatus.ACCEPTED.value
        assert plan.planned_order_qty == 500
        assert plan.rejection_reason_code is None

        # DB に保存されていること
        result = await db_session.execute(
            select(SignalPlan).where(SignalPlan.signal_id == signal.id)
        )
        saved = result.scalar_one()
        assert saved.planning_status == "accepted"

    async def test_reduced_plan_by_size_ratio(self, db_session):
        """size_ratio=0.5 → REDUCED、planned_qty が縮小される"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal(quantity=1000)
        db_session.add(signal)
        ctx = _make_context(signal=signal, size_ratio=0.5)

        plan = await service.plan(signal, ctx)

        assert plan.planning_status == PlanningStatus.REDUCED.value
        assert plan.planned_order_qty == 500

    async def test_rejected_decision_missing(self, db_session):
        """signal_strategy_decision_id=None → DECISION_MISSING で reject"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal()
        db_session.add(signal)
        ctx = _make_context(signal=signal, ssd_id=None)
        # ssd_id=None にするため直接上書き
        ctx.signal_strategy_decision_id = None

        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await service.plan(signal, ctx)

        assert exc_info.value.reason_code == PlanningReasonCode.DECISION_MISSING

    async def test_rejected_decision_stale(self, db_session):
        """decision が古すぎる → DECISION_STALE で reject"""
        from datetime import timedelta
        from unittest.mock import patch

        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal()
        db_session.add(signal)
        stale_time = _NOW - timedelta(seconds=9999)
        ctx = _make_context(signal=signal, decision_time=stale_time)

        # MAX_DECISION_AGE_SEC を小さく設定
        with patch("trade_app.services.planning.service._get_max_decision_age_sec", return_value=60):
            with pytest.raises(SignalPlanRejectedError) as exc_info:
                await service.plan(signal, ctx)

        assert exc_info.value.reason_code == PlanningReasonCode.DECISION_STALE

    async def test_rejected_market_not_tradable(self, db_session):
        """is_market_tradable=False → MARKET_NOT_TRADABLE で reject"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal()
        db_session.add(signal)
        ctx = _make_context(signal=signal, is_market_tradable=False)

        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await service.plan(signal, ctx)

        assert exc_info.value.reason_code == PlanningReasonCode.MARKET_NOT_TRADABLE

    async def test_rejected_symbol_not_tradable(self, db_session):
        """is_symbol_tradable=False → SYMBOL_NOT_TRADABLE で reject"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal()
        db_session.add(signal)
        ctx = _make_context(signal=signal, is_symbol_tradable=False)

        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await service.plan(signal, ctx)

        assert exc_info.value.reason_code == PlanningReasonCode.SYMBOL_NOT_TRADABLE

    async def test_rejected_spread_too_wide(self, db_session):
        """spread_bps >= 100 → SPREAD_TOO_WIDE で reject"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal()
        db_session.add(signal)
        ctx = _make_context(signal=signal, spread_bps=150.0)

        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await service.plan(signal, ctx)

        assert exc_info.value.reason_code == PlanningReasonCode.SPREAD_TOO_WIDE

    async def test_rejected_lot_rounding_to_zero(self, db_session):
        """size_ratio 縮小後に lot 丸めで 0 → PLANNED_SIZE_ZERO で reject"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal(quantity=100)
        db_session.add(signal)
        # 50% 縮小 → 50 株 → lot_size=100 で丸めると 0
        ctx = _make_context(signal=signal, size_ratio=0.5, symbol_lot_size=100)

        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await service.plan(signal, ctx)

        assert exc_info.value.reason_code == PlanningReasonCode.PLANNED_SIZE_ZERO

    async def test_exit_signal_bypass(self, db_session):
        """exit シグナル → planning バイパス、ACCEPTED でそのまま保存"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal(signal_type="exit", quantity=300)
        db_session.add(signal)
        # exit bypass では decision_id が None でも通過
        ctx = _make_context(signal=signal)
        ctx.signal_strategy_decision_id = None

        plan = await service.plan(signal, ctx)

        assert plan.planning_status == PlanningStatus.ACCEPTED.value
        assert plan.planned_order_qty == 300

    async def test_plan_reasons_saved_on_reduction(self, db_session):
        """縮小理由が signal_plan_reasons に保存されること"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal(quantity=1000)
        db_session.add(signal)
        ctx = _make_context(signal=signal, size_ratio=0.5)

        plan = await service.plan(signal, ctx)

        result = await db_session.execute(
            select(SignalPlanReason).where(SignalPlanReason.signal_plan_id == plan.id)
        )
        reasons = result.scalars().all()
        assert len(reasons) >= 1
        reason_codes = [r.reason_code for r in reasons]
        assert PlanningReasonCode.SIZE_RATIO_APPLIED.value in reason_codes

    async def test_plan_reasons_saved_on_rejection(self, db_session):
        """拒否時も signal_plan_reasons が保存されること"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal()
        db_session.add(signal)
        ctx = _make_context(signal=signal, is_market_tradable=False)

        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await service.plan(signal, ctx)

        plan_id = exc_info.value.plan_id
        result = await db_session.execute(
            select(SignalPlanReason).where(SignalPlanReason.signal_plan_id == plan_id)
        )
        reasons = result.scalars().all()
        assert len(reasons) >= 1
        assert reasons[0].reason_code == PlanningReasonCode.MARKET_NOT_TRADABLE.value

    async def test_liquidity_reduces_size(self, db_session):
        """volume_ratio=0.05 (< 0.1) → 25% 縮小で REDUCED"""
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        signal = _make_signal(quantity=1000)
        db_session.add(signal)
        ctx = _make_context(signal=signal, volume_ratio=0.05)

        plan = await service.plan(signal, ctx)

        assert plan.planning_status == PlanningStatus.REDUCED.value
        # 1000 * 0.25 = 250, lot 丸め 100 → 200
        assert plan.planned_order_qty == 200

    async def test_lot_rounding_reduction_uses_lot_size_below_min_reason(self, db_session):
        """
        lot 丸めで縮小(150→100)が起きた場合、
        reason_code が LOT_SIZE_BELOW_MIN になること（SIZE_RATIO_APPLIED でないこと）
        """
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        # 150 株 → lot_size=100 で丸めると 100 株（縮小 = 50 株）
        signal = _make_signal(quantity=150)
        db_session.add(signal)
        ctx = _make_context(signal=signal, size_ratio=1.0, symbol_lot_size=100)

        plan = await service.plan(signal, ctx)

        assert plan.planning_status == PlanningStatus.REDUCED.value
        assert plan.planned_order_qty == 100

        result = await db_session.execute(
            select(SignalPlanReason).where(SignalPlanReason.signal_plan_id == plan.id)
        )
        reasons = result.scalars().all()
        reason_codes = [r.reason_code for r in reasons]
        assert PlanningReasonCode.LOT_SIZE_BELOW_MIN.value in reason_codes
        assert PlanningReasonCode.SIZE_RATIO_APPLIED.value not in reason_codes

    async def test_lot_rounding_to_zero_uses_lot_size_below_min_reason(self, db_session):
        """
        lot 丸めで 0 になった場合（50→0）、
        signal_plan_reasons の reason_code が LOT_SIZE_BELOW_MIN になること
        """
        audit = AuditLogger(db_session)
        service = SignalPlanningService(db=db_session, audit=audit)
        # 50 株 → lot_size=100 で丸めると 0
        signal = _make_signal(quantity=50)
        db_session.add(signal)
        ctx = _make_context(signal=signal, size_ratio=1.0, symbol_lot_size=100)

        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await service.plan(signal, ctx)

        assert exc_info.value.reason_code == PlanningReasonCode.PLANNED_SIZE_ZERO

        # lot rounding の reason も LOT_SIZE_BELOW_MIN で記録されていること
        result = await db_session.execute(
            select(SignalPlanReason).where(SignalPlanReason.signal_plan_id == exc_info.value.plan_id)
        )
        reasons = result.scalars().all()
        reason_codes = [r.reason_code for r in reasons]
        assert PlanningReasonCode.LOT_SIZE_BELOW_MIN.value in reason_codes


# ─── 7. PlannerContextBuilder ─────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPlannerContextBuilder:
    async def test_finds_signal_strategy_decision(self, db_session):
        """DB に SignalStrategyDecision がある場合、context に ID が入ること"""
        signal = _make_signal()
        db_session.add(signal)
        await db_session.flush()

        ssd = SignalStrategyDecision(
            id=str(uuid.uuid4()),
            signal_id=signal.id,
            ticker=signal.ticker,
            signal_direction="long",
            global_decision_id=None,
            symbol_decision_id=None,
            decision_time=_NOW,
            entry_allowed=True,
            size_ratio=0.8,
            blocking_reasons_json=[],
            evidence_json={},
            created_at=_NOW,
        )
        db_session.add(ssd)
        await db_session.flush()

        builder = PlannerContextBuilder(db=db_session)
        ctx = await builder.build(signal=signal, size_ratio=0.8)

        assert ctx.signal_strategy_decision_id == ssd.id
        assert ctx.size_ratio == 0.8

    async def test_no_signal_strategy_decision_returns_none(self, db_session):
        """DB に SignalStrategyDecision がない場合、decision_id=None"""
        signal = _make_signal()
        db_session.add(signal)
        await db_session.flush()

        builder = PlannerContextBuilder(db=db_session)
        ctx = await builder.build(signal=signal)

        assert ctx.signal_strategy_decision_id is None
