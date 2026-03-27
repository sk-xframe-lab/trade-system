"""
Phase AB — shadow_hard_guard_promotion_decision 導出テスト

確認項目:
  A. no_signal
     1.  shadow_triggered = False → decision = "no_signal"
     2.  decision_reasons = ["shadow_not_triggered"]

  B. observe
     3.  shadow_triggered = True / would_reject = False → decision = "observe"
     4.  decision_reasons = ["shadow_triggered_without_would_reject"]

  C. hold
     5.  would_reject = True / overlaps_with_price_stale = True → decision = "hold"
     6.  decision_reasons = ["overlaps_with_price_stale"]

  D. review_priority
     7.  would_reject = True / overlaps_with_price_stale = False → decision = "review_priority"
     8.  decision_reasons = ["shadow_would_reject"]

  E. evidence の整合性
     9.  evidence の各フィールドが Phase AA metrics と同じ値
    10. evidence に必須フィールドが全て含まれる

  F. candidate filtering
    11. 他 candidate の shadow event は stale_bid_ask decision に影響しない

  G. 不正入力安全性
    12. trace = None → 安全に no_signal
    13. trace = {} → 安全に no_signal
    14. trace = ["bad"] → 安全に no_signal
    15. execution_guard_hints = None → overlaps_with_price_stale = False
    16. execution_guard_hints = [] → overlaps_with_price_stale = False

  H. get_shadow_hard_guard_promotion_decision accessor
    17. decision entry がある → dict を返す
    18. candidate 不一致 → None
    19. entry なし → None
    20. 不正 trace → None

  I. service integration
    21. _save_plan() 後に shadow_hard_guard_promotion_decision が1件のみ存在
    22. stale_bid_ask blocking → decision = "review_priority"
    23. blocking なし → decision = "no_signal"

  J. Phase AA 回帰防止
    24. promotion metrics が従来どおり存在・event_count 不変
    25. metrics の values は変わらない

  K. Phase Z / Y / X / W / V 回帰防止
    26. 全派生 stage が重複しない（7 stage 全て各 1 件）
    27. stale_bid_ask は reject しない
    28. price_stale は reject する
    29. planned_qty / planning_status / rejection_reason_code 不変
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from trade_app.services.planning.context import PlannerContext
from trade_app.services.planning.service import (
    SignalPlanningService,
    SignalPlanRejectedError,
)
from trade_app.services.planning.trace_helpers import (
    extract_shadow_hard_guard_promotion_decision,
    extract_shadow_hard_guard_promotion_metrics,
    get_shadow_hard_guard_promotion_decision,
    get_shadow_hard_guard_promotion_metrics,
)

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

_EVIDENCE_KEYS = {
    "shadow_triggered", "would_reject", "overlaps_with_price_stale",
    "has_advisory_guard", "advisory_guard_level", "promotion_signal_weight",
}

_DERIVED_STAGES = (
    "advisory_guard_assessment",
    "shadow_hard_guard_assessment",
    "shadow_hard_guard_review_summary",
    "shadow_hard_guard_promotion_metrics",
    "shadow_hard_guard_promotion_decision",
)


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _shadow_event(candidate: str = "stale_bid_ask", decision: str = "would_reject") -> dict:
    return {"stage": "shadow_hard_guard_decision", "candidate": candidate, "decision": decision}


def _advisory_entry(level: str = "blocking") -> dict:
    return {"stage": "advisory_guard_assessment", "guard_level": level, "guard_reasons": []}


def _trace_with_hints(
    shadow_candidate: str = "stale_bid_ask",
    shadow_decision: str = "would_reject",
    price_stale: bool = False,
    advisory_level: str = "blocking",
) -> tuple[list, dict]:
    hints = {
        "blocking_reasons": (["price_stale"] if price_stale else []),
        "warning_reasons": [],
    }
    trace = [
        _shadow_event(shadow_candidate, shadow_decision),
        _advisory_entry(advisory_level),
    ]
    return trace, hints


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


# ─── A. no_signal ─────────────────────────────────────────────────────────────

class TestNoSignal:
    def test_decision(self):
        result = extract_shadow_hard_guard_promotion_decision([])
        assert result["decision"] == "no_signal"

    def test_decision_reasons(self):
        result = extract_shadow_hard_guard_promotion_decision([])
        assert result["decision_reasons"] == ["shadow_not_triggered"]


# ─── B. observe ──────────────────────────────────────────────────────────────

class TestObserve:
    def test_decision(self):
        trace = [_shadow_event("stale_bid_ask", "advisory_only")]
        result = extract_shadow_hard_guard_promotion_decision(trace)
        assert result["decision"] == "observe"

    def test_decision_reasons(self):
        trace = [_shadow_event("stale_bid_ask", "advisory_only")]
        result = extract_shadow_hard_guard_promotion_decision(trace)
        assert result["decision_reasons"] == ["shadow_triggered_without_would_reject"]


# ─── C. hold ─────────────────────────────────────────────────────────────────

class TestHold:
    def test_decision(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        hints = {"blocking_reasons": ["price_stale", "stale_bid_ask"], "warning_reasons": []}
        result = extract_shadow_hard_guard_promotion_decision(trace, execution_guard_hints=hints)
        assert result["decision"] == "hold"

    def test_decision_reasons(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        hints = {"blocking_reasons": ["price_stale"], "warning_reasons": []}
        result = extract_shadow_hard_guard_promotion_decision(trace, execution_guard_hints=hints)
        assert result["decision_reasons"] == ["overlaps_with_price_stale"]


# ─── D. review_priority ───────────────────────────────────────────────────────

class TestReviewPriority:
    def test_decision(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        result = extract_shadow_hard_guard_promotion_decision(trace, execution_guard_hints=hints)
        assert result["decision"] == "review_priority"

    def test_decision_reasons(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        hints = {"blocking_reasons": [], "warning_reasons": []}
        result = extract_shadow_hard_guard_promotion_decision(trace, execution_guard_hints=hints)
        assert result["decision_reasons"] == ["shadow_would_reject"]


# ─── E. evidence の整合性 ─────────────────────────────────────────────────────

class TestEvidenceConsistency:
    def test_evidence_matches_metrics(self):
        trace = [_shadow_event(), _advisory_entry("blocking")]
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        metrics = extract_shadow_hard_guard_promotion_metrics(
            trace, execution_guard_hints=hints
        )
        decision_entry = extract_shadow_hard_guard_promotion_decision(
            trace, execution_guard_hints=hints
        )
        ev = decision_entry["evidence"]
        assert ev["shadow_triggered"] == metrics["shadow_triggered"]
        assert ev["would_reject"] == metrics["would_reject"]
        assert ev["overlaps_with_price_stale"] == metrics["overlaps_with_price_stale"]
        assert ev["has_advisory_guard"] == metrics["has_advisory_guard"]
        assert ev["advisory_guard_level"] == metrics["advisory_guard_level"]
        assert ev["promotion_signal_weight"] == metrics["promotion_signal_weight"]

    def test_evidence_has_all_required_keys(self):
        result = extract_shadow_hard_guard_promotion_decision([_shadow_event()])
        assert _EVIDENCE_KEYS <= set(result["evidence"].keys())


# ─── F. candidate filtering ───────────────────────────────────────────────────

class TestCandidateFiltering:
    def test_other_candidate_not_counted(self):
        trace = [_shadow_event("wide_spread", "would_reject")]
        result = extract_shadow_hard_guard_promotion_decision(trace, candidate="stale_bid_ask")
        assert result["decision"] == "no_signal"


# ─── G. 不正入力安全性 ────────────────────────────────────────────────────────

class TestInvalidInputSafety:
    def test_trace_none(self):
        result = extract_shadow_hard_guard_promotion_decision(None)
        assert result["decision"] == "no_signal"

    def test_trace_dict(self):
        result = extract_shadow_hard_guard_promotion_decision({})
        assert result["decision"] == "no_signal"

    def test_trace_non_dict_elements(self):
        result = extract_shadow_hard_guard_promotion_decision(["bad", 42])
        assert result["decision"] == "no_signal"

    def test_hints_none(self):
        trace = [_shadow_event()]
        result = extract_shadow_hard_guard_promotion_decision(trace, execution_guard_hints=None)
        assert result["evidence"]["overlaps_with_price_stale"] is False

    def test_hints_list(self):
        trace = [_shadow_event()]
        result = extract_shadow_hard_guard_promotion_decision(trace, execution_guard_hints=[])
        assert result["evidence"]["overlaps_with_price_stale"] is False


# ─── H. accessor ─────────────────────────────────────────────────────────────

class TestAccessor:
    def test_returns_entry_when_present(self):
        trace = [
            _shadow_event(),
            {
                "stage": "shadow_hard_guard_promotion_decision",
                "candidate": "stale_bid_ask",
                "decision": "review_priority",
                "decision_reasons": ["shadow_would_reject"],
                "evidence": {},
            },
        ]
        result = get_shadow_hard_guard_promotion_decision(trace)
        assert result is not None
        assert result["decision"] == "review_priority"

    def test_returns_none_candidate_mismatch(self):
        trace = [{
            "stage": "shadow_hard_guard_promotion_decision",
            "candidate": "stale_bid_ask",
            "decision": "review_priority",
            "decision_reasons": [],
            "evidence": {},
        }]
        assert get_shadow_hard_guard_promotion_decision(trace, candidate="wide_spread") is None

    def test_returns_none_when_absent(self):
        assert get_shadow_hard_guard_promotion_decision([]) is None

    def test_invalid_trace_returns_none(self):
        assert get_shadow_hard_guard_promotion_decision(None) is None


# ─── I. service integration ───────────────────────────────────────────────────

class TestServiceIntegration:
    @pytest.mark.asyncio
    async def test_decision_stage_appears_once(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _count_stage(plan.planning_trace_json, "shadow_hard_guard_promotion_decision") == 1

    @pytest.mark.asyncio
    async def test_decision_review_priority_when_stale_bid_ask(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_promotion_decision(plan.planning_trace_json)
        assert entry["decision"] == "review_priority"

    @pytest.mark.asyncio
    async def test_decision_no_signal_when_no_blocking(self):
        ctx = _make_context({"blocking_reasons": [], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_promotion_decision(plan.planning_trace_json)
        assert entry["decision"] == "no_signal"


# ─── J. Phase AA 回帰防止 ─────────────────────────────────────────────────────

class TestPhaseAARegression:
    @pytest.mark.asyncio
    async def test_promotion_metrics_still_present(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_promotion_metrics(plan.planning_trace_json)
        assert entry is not None

    @pytest.mark.asyncio
    async def test_promotion_metrics_values_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_promotion_metrics(plan.planning_trace_json)
        assert entry["shadow_triggered"] is True
        assert entry["would_reject"] is True
        assert entry["promotion_signal_weight"] == 1


# ─── K. Phase Z / Y / X / W / V 回帰防止 ────────────────────────────────────

class TestPreviousPhaseRegression:
    @pytest.mark.asyncio
    async def test_no_duplicate_derived_stages(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        trace = plan.planning_trace_json
        for stage in _DERIVED_STAGES:
            assert _count_stage(trace, stage) == 1, f"{stage} が重複している"

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
        plan, _ = await _run_plan_capture(ctx)
        assert plan.rejection_reason_code == "execution_guard_price_stale"
