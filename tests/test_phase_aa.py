"""
Phase AA — shadow_hard_guard_promotion_metrics 導出テスト

確認項目:
  A. 基本ケース（would_reject=True）
     1.  shadow_triggered = True
     2.  would_reject = True
     3.  promotion_signal_weight = 1

  B. shadow あり・would_reject=False
     4.  shadow_triggered = True
     5.  would_reject = False
     6.  promotion_signal_weight = 0

  C. shadow なし
     7.  shadow_triggered = False
     8.  would_reject = False
     9.  promotion_signal_weight = 0

  D. overlaps_with_price_stale
    10. blocking_reasons に "price_stale" あり → true
    11. blocking_reasons に "price_stale" なし → false
    12. blocking_reasons キーなし → false
    13. execution_guard_hints = None → false

  E. has_advisory_guard / advisory_guard_level
    14. guard_level = "warning" → has=True / level="warning"
    15. guard_level = "blocking" → has=True / level="blocking"
    16. guard_level = "none" → has=False / level="none"
    17. advisory entry なし → has=False / level="none"
    18. advisory entry 不正 → 安全側 has=False / level="none"

  F. candidate filtering
    19. wide_spread の shadow event だけある場合、stale_bid_ask metrics は triggered=False

  G. 不正入力安全性
    20. trace = None
    21. trace = {} (非 list)
    22. trace = ["bad"] (非 dict 要素)
    23. execution_guard_hints = None
    24. execution_guard_hints = [] (非 dict)
    25. execution_guard_hints.blocking_reasons = None

  H. 戻り値構造
    26. stage = "shadow_hard_guard_promotion_metrics"
    27. candidate フィールドが引数と一致
    28. 必須キーが全て存在する

  I. service integration
    29. planning_trace_json に shadow_hard_guard_promotion_metrics が1件だけ存在
    30. stale_bid_ask blocking → metrics の値が正しい

  J. Phase Z 回帰防止
    31. advisory/shadow/review の派生 entry が重複しないこと
    32. get_shadow_hard_guard_assessment の既存挙動が変わらない
    33. get_shadow_hard_guard_review_summary の既存挙動が変わらない

  K. Phase V / W / X / Y 回帰防止
    34. stale_bid_ask は reject しない
    35. price_stale は reject する
    36. planned_qty / planning_status / rejection_reason_code 不変
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
from trade_app.services.planning.trace_helpers import (
    extract_shadow_hard_guard_promotion_metrics,
    get_shadow_hard_guard_assessment,
    get_shadow_hard_guard_promotion_metrics,
    get_shadow_hard_guard_review_summary,
)

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

_REQUIRED_KEYS = {
    "stage", "candidate", "shadow_triggered", "would_reject",
    "overlaps_with_price_stale", "has_advisory_guard",
    "advisory_guard_level", "promotion_signal_weight",
}


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _shadow_event(candidate: str = "stale_bid_ask", decision: str = "would_reject") -> dict:
    return {"stage": "shadow_hard_guard_decision", "candidate": candidate, "decision": decision}


def _advisory_entry(level: str = "none") -> dict:
    return {"stage": "advisory_guard_assessment", "guard_level": level, "guard_reasons": []}


def _make_signal() -> MagicMock:
    s = MagicMock()
    s.id = str(uuid.uuid4())
    s.ticker = "7203"
    s.signal_type = "entry"
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


def _make_service_capture(ctx: PlannerContext):
    db_mock = AsyncMock()
    db_mock.flush = AsyncMock()
    saved_plans = []

    def _capture_add(obj):
        from trade_app.models.signal_plan import SignalPlan
        if isinstance(obj, SignalPlan):
            saved_plans.append(obj)

    db_mock.add = MagicMock(side_effect=_capture_add)
    service = SignalPlanningService(db=db_mock, audit=AsyncMock())
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
    service, _ = _make_service_capture(ctx)
    return await service.plan(ctx.signal, ctx)


async def _run_plan_capture(ctx: PlannerContext):
    service, saved_plans = _make_service_capture(ctx)
    try:
        plan = await service.plan(ctx.signal, ctx)
        return plan, None
    except SignalPlanRejectedError as exc:
        return saved_plans[0] if saved_plans else None, exc


def _count_stage(trace: list, stage: str) -> int:
    return sum(1 for e in trace if isinstance(e, dict) and e.get("stage") == stage)


# ─── A. 基本ケース（would_reject=True）──────────────────────────────────────

class TestWouldRejectTrue:
    def test_shadow_triggered(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["shadow_triggered"] is True

    def test_would_reject(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["would_reject"] is True

    def test_promotion_signal_weight(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["promotion_signal_weight"] == 1


# ─── B. shadow あり・would_reject=False ──────────────────────────────────────

class TestShadowTriggeredNoReject:
    def test_shadow_triggered(self):
        trace = [_shadow_event("stale_bid_ask", "advisory_only")]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["shadow_triggered"] is True

    def test_would_reject_false(self):
        trace = [_shadow_event("stale_bid_ask", "advisory_only")]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["would_reject"] is False

    def test_promotion_signal_weight_zero(self):
        trace = [_shadow_event("stale_bid_ask", "advisory_only")]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["promotion_signal_weight"] == 0


# ─── C. shadow なし ───────────────────────────────────────────────────────────

class TestNoShadow:
    def test_shadow_triggered_false(self):
        result = extract_shadow_hard_guard_promotion_metrics([])
        assert result["shadow_triggered"] is False

    def test_would_reject_false(self):
        result = extract_shadow_hard_guard_promotion_metrics([])
        assert result["would_reject"] is False

    def test_promotion_signal_weight_zero(self):
        result = extract_shadow_hard_guard_promotion_metrics([])
        assert result["promotion_signal_weight"] == 0


# ─── D. overlaps_with_price_stale ────────────────────────────────────────────

class TestOverlapsWithPriceStale:
    def test_true_when_price_stale_in_blocking(self):
        hints = {"blocking_reasons": ["price_stale", "stale_bid_ask"]}
        result = extract_shadow_hard_guard_promotion_metrics([], execution_guard_hints=hints)
        assert result["overlaps_with_price_stale"] is True

    def test_false_when_price_stale_not_in_blocking(self):
        hints = {"blocking_reasons": ["stale_bid_ask"]}
        result = extract_shadow_hard_guard_promotion_metrics([], execution_guard_hints=hints)
        assert result["overlaps_with_price_stale"] is False

    def test_false_when_blocking_reasons_missing(self):
        hints = {"warning_reasons": ["price_stale"]}
        result = extract_shadow_hard_guard_promotion_metrics([], execution_guard_hints=hints)
        assert result["overlaps_with_price_stale"] is False

    def test_false_when_hints_none(self):
        result = extract_shadow_hard_guard_promotion_metrics([], execution_guard_hints=None)
        assert result["overlaps_with_price_stale"] is False


# ─── E. has_advisory_guard / advisory_guard_level ────────────────────────────

class TestAdvisoryGuardFields:
    def test_warning_level(self):
        trace = [_advisory_entry("warning")]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["has_advisory_guard"] is True
        assert result["advisory_guard_level"] == "warning"

    def test_blocking_level(self):
        trace = [_advisory_entry("blocking")]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["has_advisory_guard"] is True
        assert result["advisory_guard_level"] == "blocking"

    def test_none_level(self):
        trace = [_advisory_entry("none")]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["has_advisory_guard"] is False
        assert result["advisory_guard_level"] == "none"

    def test_no_advisory_entry(self):
        result = extract_shadow_hard_guard_promotion_metrics([])
        assert result["has_advisory_guard"] is False
        assert result["advisory_guard_level"] == "none"

    def test_advisory_entry_guard_level_not_str(self):
        trace = [{"stage": "advisory_guard_assessment", "guard_level": 123}]
        result = extract_shadow_hard_guard_promotion_metrics(trace)
        assert result["has_advisory_guard"] is False
        assert result["advisory_guard_level"] == "none"


# ─── F. candidate filtering ───────────────────────────────────────────────────

class TestCandidateFiltering:
    def test_other_candidate_not_counted(self):
        trace = [_shadow_event("wide_spread", "would_reject")]
        result = extract_shadow_hard_guard_promotion_metrics(trace, candidate="stale_bid_ask")
        assert result["shadow_triggered"] is False
        assert result["would_reject"] is False
        assert result["promotion_signal_weight"] == 0


# ─── G. 不正入力安全性 ────────────────────────────────────────────────────────

class TestInvalidInputSafety:
    def test_trace_none(self):
        result = extract_shadow_hard_guard_promotion_metrics(None)
        assert result["shadow_triggered"] is False

    def test_trace_dict(self):
        result = extract_shadow_hard_guard_promotion_metrics({})
        assert result["shadow_triggered"] is False

    def test_trace_non_dict_elements(self):
        result = extract_shadow_hard_guard_promotion_metrics(["bad", 42])
        assert result["shadow_triggered"] is False

    def test_hints_none(self):
        result = extract_shadow_hard_guard_promotion_metrics([], execution_guard_hints=None)
        assert result["overlaps_with_price_stale"] is False

    def test_hints_list(self):
        result = extract_shadow_hard_guard_promotion_metrics([], execution_guard_hints=[])
        assert result["overlaps_with_price_stale"] is False

    def test_hints_blocking_reasons_none(self):
        result = extract_shadow_hard_guard_promotion_metrics(
            [], execution_guard_hints={"blocking_reasons": None}
        )
        assert result["overlaps_with_price_stale"] is False


# ─── H. 戻り値構造 ────────────────────────────────────────────────────────────

class TestReturnStructure:
    def test_stage_value(self):
        result = extract_shadow_hard_guard_promotion_metrics([])
        assert result["stage"] == "shadow_hard_guard_promotion_metrics"

    def test_candidate_field_matches_argument(self):
        result = extract_shadow_hard_guard_promotion_metrics([], candidate="wide_spread")
        assert result["candidate"] == "wide_spread"

    def test_all_required_keys_present(self):
        result = extract_shadow_hard_guard_promotion_metrics([_shadow_event()])
        assert _REQUIRED_KEYS <= set(result.keys())


# ─── I. service integration ───────────────────────────────────────────────────

class TestServiceIntegration:
    @pytest.mark.asyncio
    async def test_metrics_in_trace_once(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _count_stage(plan.planning_trace_json, "shadow_hard_guard_promotion_metrics") == 1

    @pytest.mark.asyncio
    async def test_metrics_values_when_stale_bid_ask(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_promotion_metrics(plan.planning_trace_json)
        assert entry is not None
        assert entry["shadow_triggered"] is True
        assert entry["would_reject"] is True
        assert entry["promotion_signal_weight"] == 1
        assert entry["overlaps_with_price_stale"] is False

    @pytest.mark.asyncio
    async def test_metrics_values_no_shadow(self):
        ctx = _make_context({"blocking_reasons": [], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_promotion_metrics(plan.planning_trace_json)
        assert entry is not None
        assert entry["shadow_triggered"] is False
        assert entry["promotion_signal_weight"] == 0


# ─── J. Phase Z 回帰防止 ─────────────────────────────────────────────────────

class TestPhaseZRegression:
    @pytest.mark.asyncio
    async def test_derived_stages_not_duplicated(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        trace = plan.planning_trace_json
        for stage in (
            "advisory_guard_assessment",
            "shadow_hard_guard_assessment",
            "shadow_hard_guard_review_summary",
            "shadow_hard_guard_promotion_metrics",
        ):
            assert _count_stage(trace, stage) == 1, f"{stage} が重複している"

    @pytest.mark.asyncio
    async def test_get_shadow_hard_guard_assessment_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_assessment(plan.planning_trace_json)
        assert entry is not None
        assert entry["event_count"] == 1
        assert entry["candidates"] == ["stale_bid_ask"]

    @pytest.mark.asyncio
    async def test_get_shadow_hard_guard_review_summary_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_review_summary(plan.planning_trace_json)
        assert entry is not None
        assert entry["promotion_readiness"] == "needs_review"


# ─── K. Phase V / W / X / Y 回帰防止 ────────────────────────────────────────

class TestPhaseVWXYRegression:
    @pytest.mark.asyncio
    async def test_stale_bid_ask_does_not_reject(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.planning_status != "rejected"
        assert plan.rejection_reason_code is None
        assert plan.planned_order_qty == 100

    @pytest.mark.asyncio
    async def test_price_stale_still_rejects(self):
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        with pytest.raises(SignalPlanRejectedError):
            await _run_plan(ctx)

    @pytest.mark.asyncio
    async def test_price_stale_rejection_reason_code(self):
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        service, saved_plans = _make_service_capture(ctx)
        try:
            await service.plan(ctx.signal, ctx)
        except SignalPlanRejectedError:
            pass
        assert saved_plans[0].rejection_reason_code == "execution_guard_price_stale"
