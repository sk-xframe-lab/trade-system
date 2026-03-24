"""
Phase V — price_stale hard guard テスト

確認項目:
  A. hard guard の reject 条件
     1.  guard_level=blocking かつ price_stale を含む → reject
     2.  stale_bid_ask のみ blocking → reject しない
     3.  quote_only のみ blocking → reject しない
     4.  wide_spread のみ warning → reject しない
     5.  blocking + price_stale + 他の理由でも reject する
     6.  blocking だが price_stale を含まない → reject しない

  B. reject 時の挙動
     7.  planned_qty = 0 になること
     8.  planning_status = "rejected" になること
     9.  rejection_reason_code = "execution_guard_price_stale" になること
    10.  SignalPlanRejectedError が送出されること
    11.  SignalPlanRejectedError.reason_code.value が正しい

  C. trace
    12. hard_guard_decision ステージが trace に含まれること
    13. hard_guard_decision.decision = "reject"
    14. hard_guard_decision.reason = "execution_guard_price_stale"
    15. execution_guard_hints ステージも trace に含まれること
    16. advisory_guard_assessment ステージも trace に含まれること

  D. pass 時（price_stale 以外）の不変確認
    17. stale_bid_ask blocking で planned_qty は変わらない
    18. wide_spread warning で planning_status は変わらない
    19. hard_guard_decision ステージは trace に含まれない（reject しない場合）

  E. PlanningReasonCode 定数
    20. EXECUTION_GUARD_PRICE_STALE が PlanningReasonCode に存在する
    21. 値が "execution_guard_price_stale"
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from trade_app.services.planning.context import PlannerContext
from trade_app.services.planning.reasons import PlanningReasonCode
from trade_app.services.planning.service import (
    SignalPlanningService,
    SignalPlanRejectedError,
)

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _make_signal(signal_type: str = "entry") -> MagicMock:
    s = MagicMock()
    s.id = str(uuid.uuid4())
    s.ticker = "7203"
    s.signal_type = signal_type
    s.quantity = 100
    s.limit_price = 3000.0
    s.side = "buy"
    return s


def _make_context(hints: dict) -> PlannerContext:
    return PlannerContext(
        signal=_make_signal(),
        size_ratio=1.0,
        signal_strategy_decision_id=str(uuid.uuid4()),
        decision_evaluation_time=_NOW,
        execution_guard_hints=hints,
    )


async def _run_plan(ctx: PlannerContext):
    """SignalPlanningService.plan() を最小限 mock で実行"""
    db_mock = AsyncMock()
    db_mock.flush = AsyncMock()
    db_mock.add = MagicMock()
    audit_mock = AsyncMock()
    service = SignalPlanningService(db=db_mock, audit=audit_mock)

    service._validate_decision = MagicMock(return_value=None)

    sizer_result = MagicMock()
    sizer_result.base_qty = 100
    sizer_result.applied_size_ratio = 1.0
    sizer_result.after_ratio_qty = 100
    service._sizer.calculate = MagicMock(return_value=sizer_result)
    service._sizer.round_to_lot = MagicMock(return_value=100)

    for adj_name in ("_tradability", "_liquidity", "_spread", "_volatility"):
        adj_result = MagicMock()
        adj_result.as_trace_entry.return_value = {"stage": adj_name}
        adj_result.rejected = False
        adj_result.was_reduced = False
        adj_result.output_qty = 100
        getattr(service, adj_name).check = MagicMock(return_value=adj_result)
        getattr(service, adj_name).adjust = MagicMock(return_value=adj_result)

    exec_params = MagicMock()
    exec_params.as_dict.return_value = {}
    exec_params.order_type_candidate = "market"
    exec_params.limit_price = None
    exec_params.stop_price = None
    exec_params.max_slippage_bps = 30.0
    exec_params.participation_rate_cap = None
    exec_params.entry_timeout_seconds = None
    service._params_builder.build = MagicMock(return_value=exec_params)

    return await service.plan(ctx.signal, ctx)


def _get_stage(plan, stage: str) -> dict | None:
    return next(
        (e for e in plan.planning_trace_json if e.get("stage") == stage),
        None,
    )


# ─── A. hard guard の reject 条件 ─────────────────────────────────────────────

class TestHardGuardCondition:
    @pytest.mark.asyncio
    async def test_price_stale_blocking_rejects(self):
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        with pytest.raises(SignalPlanRejectedError):
            await _run_plan(ctx)

    @pytest.mark.asyncio
    async def test_stale_bid_ask_only_does_not_reject(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.planning_status == "accepted"

    @pytest.mark.asyncio
    async def test_quote_only_blocking_does_not_reject(self):
        ctx = _make_context({"blocking_reasons": ["quote_only"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.planning_status == "accepted"

    @pytest.mark.asyncio
    async def test_wide_spread_warning_does_not_reject(self):
        ctx = _make_context({"blocking_reasons": [], "warning_reasons": ["wide_spread"]})
        plan = await _run_plan(ctx)
        assert plan.planning_status == "accepted"

    @pytest.mark.asyncio
    async def test_blocking_price_stale_with_other_reasons_rejects(self):
        ctx = _make_context({
            "blocking_reasons": ["stale_bid_ask", "price_stale", "quote_only"],
            "warning_reasons": ["wide_spread"],
        })
        with pytest.raises(SignalPlanRejectedError):
            await _run_plan(ctx)

    @pytest.mark.asyncio
    async def test_blocking_without_price_stale_does_not_reject(self):
        ctx = _make_context({
            "blocking_reasons": ["stale_bid_ask", "quote_only"],
            "warning_reasons": [],
        })
        plan = await _run_plan(ctx)
        assert plan.planning_status == "accepted"

    @pytest.mark.asyncio
    async def test_no_hints_does_not_reject(self):
        ctx = _make_context({})
        plan = await _run_plan(ctx)
        assert plan.planning_status == "accepted"


# ─── B. reject 時の挙動 ───────────────────────────────────────────────────────

class TestHardGuardRejectBehavior:
    @pytest.mark.asyncio
    async def test_planned_qty_is_zero(self):
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await _run_plan(ctx)
        err = exc_info.value
        # plan_id を使って plan を確認できないが、reason_code が正しいことを確認
        assert err.reason_code == PlanningReasonCode.EXECUTION_GUARD_PRICE_STALE

    @pytest.mark.asyncio
    async def test_planning_status_rejected(self):
        """_save_plan() に REJECTED が渡されること — plan オブジェクトで確認"""
        from trade_app.services.planning.service import SignalPlanningService
        from trade_app.services.planning.reasons import PlanningStatus

        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})

        db_mock = AsyncMock()
        db_mock.flush = AsyncMock()
        saved_plans = []

        def _capture_add(obj):
            from trade_app.models.signal_plan import SignalPlan
            if isinstance(obj, SignalPlan):
                saved_plans.append(obj)

        db_mock.add = MagicMock(side_effect=_capture_add)
        audit_mock = AsyncMock()
        service = SignalPlanningService(db=db_mock, audit=audit_mock)
        service._validate_decision = MagicMock(return_value=None)

        try:
            await service.plan(ctx.signal, ctx)
        except SignalPlanRejectedError:
            pass

        assert len(saved_plans) > 0
        assert saved_plans[0].planning_status == "rejected"
        assert saved_plans[0].planned_order_qty == 0

    @pytest.mark.asyncio
    async def test_rejection_reason_code(self):
        """rejection_reason_code が execution_guard_price_stale であること"""
        from trade_app.services.planning.service import SignalPlanningService

        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})

        db_mock = AsyncMock()
        db_mock.flush = AsyncMock()
        saved_plans = []

        def _capture_add(obj):
            from trade_app.models.signal_plan import SignalPlan
            if isinstance(obj, SignalPlan):
                saved_plans.append(obj)

        db_mock.add = MagicMock(side_effect=_capture_add)
        audit_mock = AsyncMock()
        service = SignalPlanningService(db=db_mock, audit=audit_mock)
        service._validate_decision = MagicMock(return_value=None)

        try:
            await service.plan(ctx.signal, ctx)
        except SignalPlanRejectedError:
            pass

        assert saved_plans[0].rejection_reason_code == "execution_guard_price_stale"

    @pytest.mark.asyncio
    async def test_raises_signal_plan_rejected_error(self):
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await _run_plan(ctx)
        assert isinstance(exc_info.value, SignalPlanRejectedError)

    @pytest.mark.asyncio
    async def test_error_reason_code_value(self):
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        with pytest.raises(SignalPlanRejectedError) as exc_info:
            await _run_plan(ctx)
        assert exc_info.value.reason_code.value == "execution_guard_price_stale"


# ─── C. trace ────────────────────────────────────────────────────────────────

class TestHardGuardTrace:
    async def _get_rejected_plan_trace(self) -> list[dict]:
        """hard guard reject 時の planning_trace_json を返す"""
        from trade_app.services.planning.service import SignalPlanningService

        ctx = _make_context({
            "blocking_reasons": ["price_stale"],
            "warning_reasons": ["wide_spread"],
        })
        db_mock = AsyncMock()
        db_mock.flush = AsyncMock()
        saved_plans = []

        def _capture_add(obj):
            from trade_app.models.signal_plan import SignalPlan
            if isinstance(obj, SignalPlan):
                saved_plans.append(obj)

        db_mock.add = MagicMock(side_effect=_capture_add)
        audit_mock = AsyncMock()
        service = SignalPlanningService(db=db_mock, audit=audit_mock)
        service._validate_decision = MagicMock(return_value=None)

        try:
            await service.plan(ctx.signal, ctx)
        except SignalPlanRejectedError:
            pass

        return saved_plans[0].planning_trace_json

    @pytest.mark.asyncio
    async def test_hard_guard_stage_in_trace(self):
        trace = await self._get_rejected_plan_trace()
        stages = [e.get("stage") for e in trace]
        assert "hard_guard_decision" in stages

    @pytest.mark.asyncio
    async def test_hard_guard_decision_is_reject(self):
        trace = await self._get_rejected_plan_trace()
        entry = next(e for e in trace if e.get("stage") == "hard_guard_decision")
        assert entry["decision"] == "reject"

    @pytest.mark.asyncio
    async def test_hard_guard_reason_value(self):
        trace = await self._get_rejected_plan_trace()
        entry = next(e for e in trace if e.get("stage") == "hard_guard_decision")
        assert entry["reason"] == "execution_guard_price_stale"

    @pytest.mark.asyncio
    async def test_execution_guard_hints_also_in_trace(self):
        trace = await self._get_rejected_plan_trace()
        stages = [e.get("stage") for e in trace]
        assert "execution_guard_hints" in stages

    @pytest.mark.asyncio
    async def test_advisory_also_in_trace(self):
        trace = await self._get_rejected_plan_trace()
        stages = [e.get("stage") for e in trace]
        assert "advisory_guard_assessment" in stages


# ─── D. pass 時（price_stale 以外）の不変確認 ─────────────────────────────────

class TestPassBehaviorUnchanged:
    @pytest.mark.asyncio
    async def test_stale_bid_ask_planned_qty_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.planned_order_qty == 100

    @pytest.mark.asyncio
    async def test_wide_spread_planning_status_accepted(self):
        ctx = _make_context({"blocking_reasons": [], "warning_reasons": ["wide_spread"]})
        plan = await _run_plan(ctx)
        assert plan.planning_status == "accepted"

    @pytest.mark.asyncio
    async def test_no_hard_guard_stage_when_pass(self):
        """price_stale がない場合は hard_guard_decision ステージは trace に含まれない"""
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        stages = [e.get("stage") for e in plan.planning_trace_json]
        assert "hard_guard_decision" not in stages

    @pytest.mark.asyncio
    async def test_no_hints_accepted(self):
        ctx = _make_context({})
        plan = await _run_plan(ctx)
        assert plan.planning_status == "accepted"
        assert plan.planned_order_qty == 100


# ─── E. PlanningReasonCode 定数 ───────────────────────────────────────────────

class TestReasonCodeStructure:
    def test_execution_guard_price_stale_exists(self):
        assert hasattr(PlanningReasonCode, "EXECUTION_GUARD_PRICE_STALE")

    def test_execution_guard_price_stale_value(self):
        assert PlanningReasonCode.EXECUTION_GUARD_PRICE_STALE.value == "execution_guard_price_stale"
