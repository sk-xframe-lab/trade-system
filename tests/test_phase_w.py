"""
Phase W — stale_bid_ask shadow hard guard テスト

確認項目:
  A. stale_bid_ask が blocking_reasons に含まれる場合
     1.  plan() は reject されない（SignalPlanRejectedError を送出しない）
     2.  planned_qty は shadow 判定によって 0 化しない
     3.  planning_status は "rejected" にならない
     4.  rejection_reason_code は設定されない
     5.  shadow_hard_guard_decision ステージが trace に含まれる
     6.  trace.stage = "shadow_hard_guard_decision"
     7.  trace.candidate = "stale_bid_ask"
     8.  trace.decision = "would_reject"
     9.  trace.reason = "execution_guard_stale_bid_ask_shadow"

  B. stale_bid_ask が blocking_reasons に含まれない場合
    10. shadow_hard_guard_decision ステージは trace に含まれない
    11. blocking_reasons が空でも trace に含まれない
    12. stale_bid_ask が warning_reasons のみの場合も trace に含まれない

  C. price_stale との関係（Phase V 回帰防止）
    13. price_stale が blocking にある場合は既存 hard guard が優先されて reject される
    14. price_stale + stale_bid_ask が同時に blocking の場合は reject される
    15. reject 後の rejection_reason_code は price_stale であること（Phase W のものではない）

  D. EXECUTION_GUARD_STALE_BID_ASK_SHADOW 定数
    16. reasons モジュールに存在する
    17. 値が "execution_guard_stale_bid_ask_shadow"
    18. PlanningReasonCode enum には含まれない（shadow trace 専用）
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


def _make_service_with_mocks(ctx: PlannerContext):
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

    return service, saved_plans


async def _run_plan(ctx: PlannerContext):
    service, _ = _make_service_with_mocks(ctx)
    return await service.plan(ctx.signal, ctx)


async def _run_plan_capture(ctx: PlannerContext):
    service, saved_plans = _make_service_with_mocks(ctx)
    try:
        plan = await service.plan(ctx.signal, ctx)
        return plan, None
    except SignalPlanRejectedError as exc:
        return saved_plans[0] if saved_plans else None, exc


def _get_shadow_entry(plan) -> dict | None:
    return next(
        (e for e in plan.planning_trace_json if e.get("stage") == "shadow_hard_guard_decision"),
        None,
    )


# ─── A. stale_bid_ask が blocking_reasons に含まれる場合 ──────────────────────

class TestStaleBidAskShadow:
    @pytest.mark.asyncio
    async def test_does_not_reject(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        # SignalPlanRejectedError が送出されないこと
        plan = await _run_plan(ctx)
        assert plan is not None

    @pytest.mark.asyncio
    async def test_planned_qty_not_zeroed(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.planned_order_qty == 100

    @pytest.mark.asyncio
    async def test_planning_status_not_rejected(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.planning_status != "rejected"

    @pytest.mark.asyncio
    async def test_rejection_reason_code_not_set(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.rejection_reason_code is None

    @pytest.mark.asyncio
    async def test_shadow_stage_in_trace(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_shadow_entry(plan)
        assert entry is not None

    @pytest.mark.asyncio
    async def test_trace_stage_value(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_shadow_entry(plan)
        assert entry["stage"] == "shadow_hard_guard_decision"

    @pytest.mark.asyncio
    async def test_trace_candidate(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_shadow_entry(plan)
        assert entry["candidate"] == "stale_bid_ask"

    @pytest.mark.asyncio
    async def test_trace_decision(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_shadow_entry(plan)
        assert entry["decision"] == "would_reject"

    @pytest.mark.asyncio
    async def test_trace_reason(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_shadow_entry(plan)
        assert entry["reason"] == "execution_guard_stale_bid_ask_shadow"


# ─── B. stale_bid_ask が blocking_reasons に含まれない場合 ────────────────────

class TestNoShadowWhenAbsent:
    @pytest.mark.asyncio
    async def test_no_shadow_when_no_hints(self):
        ctx = _make_context({})
        plan = await _run_plan(ctx)
        assert _get_shadow_entry(plan) is None

    @pytest.mark.asyncio
    async def test_no_shadow_when_empty_blocking(self):
        ctx = _make_context({"blocking_reasons": [], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _get_shadow_entry(plan) is None

    @pytest.mark.asyncio
    async def test_no_shadow_when_stale_bid_ask_in_warning_only(self):
        ctx = _make_context({"blocking_reasons": [], "warning_reasons": ["stale_bid_ask"]})
        plan = await _run_plan(ctx)
        assert _get_shadow_entry(plan) is None

    @pytest.mark.asyncio
    async def test_no_shadow_when_other_blocking_only(self):
        ctx = _make_context({"blocking_reasons": ["quote_only"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _get_shadow_entry(plan) is None


# ─── C. price_stale との関係（Phase V 回帰防止）───────────────────────────────

class TestPriceStaleRegression:
    @pytest.mark.asyncio
    async def test_price_stale_still_rejects(self):
        """Phase V の hard guard は Phase W の実装後も変わらず reject すること"""
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        with pytest.raises(SignalPlanRejectedError):
            await _run_plan(ctx)

    @pytest.mark.asyncio
    async def test_price_stale_and_stale_bid_ask_rejects(self):
        """price_stale + stale_bid_ask が同時の場合も price_stale hard guard が優先される"""
        ctx = _make_context({
            "blocking_reasons": ["stale_bid_ask", "price_stale"],
            "warning_reasons": [],
        })
        with pytest.raises(SignalPlanRejectedError):
            await _run_plan(ctx)

    @pytest.mark.asyncio
    async def test_price_stale_rejection_reason_code_unchanged(self):
        """price_stale reject 時の rejection_reason_code は Phase V のまま変わらない"""
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        assert plan.rejection_reason_code == "execution_guard_price_stale"


# ─── D. EXECUTION_GUARD_STALE_BID_ASK_SHADOW 定数 ───────────────────────────

class TestShadowConstant:
    def test_constant_exists_in_reasons_module(self):
        import trade_app.services.planning.reasons as reasons_mod
        assert hasattr(reasons_mod, "EXECUTION_GUARD_STALE_BID_ASK_SHADOW")

    def test_constant_value(self):
        from trade_app.services.planning.reasons import EXECUTION_GUARD_STALE_BID_ASK_SHADOW
        assert EXECUTION_GUARD_STALE_BID_ASK_SHADOW == "execution_guard_stale_bid_ask_shadow"

    def test_not_in_planning_reason_code_enum(self):
        """shadow 定数は PlanningReasonCode enum のメンバーではないこと"""
        values = {member.value for member in PlanningReasonCode}
        assert "execution_guard_stale_bid_ask_shadow" not in values
