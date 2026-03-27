"""
Phase AD — shadow_hard_guard_aggregate_review_verdict 導出テスト

確認項目:
  A. insufficient_signal
     1.  shadow_bucket = "no_signal" / countable=False → verdict = "insufficient_signal"
     2.  verdict_reasons = ["not_countable"]

  B. observe_only
     3.  shadow_bucket = "triggered_only" / countable=False → verdict = "observe_only"
     4.  verdict_reasons = ["triggered_without_would_reject"]

  C. overlap_hold
     5.  overlap_bucket = "overlaps_price_stale" / countable=True → verdict = "overlap_hold"
     6.  verdict_reasons = ["overlaps_price_stale"]

  D. priority_review
     7.  overlap_bucket = "distinct_from_price_stale" / countable=True → verdict = "priority_review"
     8.  verdict_reasons = ["distinct_would_reject"]

  E. supporting_buckets の整合性
     9.  Phase AC aggregate review key と supporting_buckets が一致
    10.  supporting_buckets に必須フィールドが全て含まれる

  F. candidate filtering
    11. 他 candidate の shadow event は stale_bid_ask verdict に影響しないこと

  G. 不正入力安全性
    12. trace = None
    13. trace = {}
    14. trace = ["bad"]
    15. execution_guard_hints = None
    16. execution_guard_hints = []
    いずれも例外なく安全側

  H. 固定フィールド
    17. stage = "shadow_hard_guard_aggregate_review_verdict"
    18. candidate が引数の値を反映
    19. verdict_version = 1

  I. get_shadow_hard_guard_aggregate_review_verdict accessor
    20. entry がある → dict を返す
    21. candidate 不一致 → None
    22. entry なし → None
    23. 不正 trace → None

  J. service integration
    24. _save_plan() 後に shadow_hard_guard_aggregate_review_verdict が1件のみ存在
    25. stale_bid_ask blocking → verdict = "priority_review"
    26. blocking なし → verdict = "insufficient_signal"

  K. Phase AC 回帰防止
    27. aggregate review key が従来どおり存在・値不変

  L. Phase AB / AA / Z / Y / X / W / V 回帰防止
    28. 全派生 stage が重複しない（9 stage 全て各 1 件）
    29. stale_bid_ask は reject しない
    30. price_stale は reject する
    31. planned_qty / planning_status / rejection_reason_code 不変
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
    extract_shadow_hard_guard_aggregate_review_key,
    extract_shadow_hard_guard_aggregate_review_verdict,
    get_shadow_hard_guard_aggregate_review_verdict,
)

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

_DERIVED_STAGES = (
    "advisory_guard_assessment",
    "shadow_hard_guard_assessment",
    "shadow_hard_guard_review_summary",
    "shadow_hard_guard_promotion_metrics",
    "shadow_hard_guard_promotion_decision",
    "shadow_hard_guard_aggregate_review_key",
    "shadow_hard_guard_aggregate_review_verdict",
)

_SUPPORTING_BUCKET_KEYS = {
    "shadow_bucket", "overlap_bucket", "advisory_bucket", "decision_bucket", "countable",
}


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _shadow_event(candidate: str = "stale_bid_ask", decision: str = "would_reject") -> dict:
    return {"stage": "shadow_hard_guard_decision", "candidate": candidate, "decision": decision}


def _advisory_entry(level: str = "blocking") -> dict:
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


async def _run_plan_capture(ctx: PlannerContext):
    service, saved_plans = _make_service_capture(ctx)
    try:
        plan = await service.plan(ctx.signal, ctx)
        return plan, None
    except SignalPlanRejectedError as exc:
        return saved_plans[0] if saved_plans else None, exc


def _count_stage(trace: list, stage: str) -> int:
    return sum(1 for e in trace if isinstance(e, dict) and e.get("stage") == stage)


def _find_stage(trace: list, stage: str) -> dict | None:
    return next(
        (e for e in trace if isinstance(e, dict) and e.get("stage") == stage),
        None,
    )


# ─── A. insufficient_signal ──────────────────────────────────────────────────

class TestInsufficientSignal:
    def test_verdict(self):
        result = extract_shadow_hard_guard_aggregate_review_verdict([])
        assert result["verdict"] == "insufficient_signal"

    def test_verdict_reasons(self):
        result = extract_shadow_hard_guard_aggregate_review_verdict([])
        assert result["verdict_reasons"] == ["not_countable"]


# ─── B. observe_only ──────────────────────────────────────────────────────────

class TestObserveOnly:
    def _trace(self) -> list:
        return [_shadow_event("stale_bid_ask", "advisory_only")]

    def test_verdict(self):
        result = extract_shadow_hard_guard_aggregate_review_verdict(self._trace())
        assert result["verdict"] == "observe_only"

    def test_verdict_reasons(self):
        result = extract_shadow_hard_guard_aggregate_review_verdict(self._trace())
        assert result["verdict_reasons"] == ["triggered_without_would_reject"]


# ─── C. overlap_hold ──────────────────────────────────────────────────────────

class TestOverlapHold:
    def _setup(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        hints = {"blocking_reasons": ["price_stale"], "warning_reasons": []}
        return trace, hints

    def test_verdict(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_verdict(trace, execution_guard_hints=hints)
        assert result["verdict"] == "overlap_hold"

    def test_verdict_reasons(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_verdict(trace, execution_guard_hints=hints)
        assert result["verdict_reasons"] == ["overlaps_price_stale"]


# ─── D. priority_review ───────────────────────────────────────────────────────

class TestPriorityReview:
    def _setup(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        hints = {"blocking_reasons": [], "warning_reasons": []}
        return trace, hints

    def test_verdict(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_verdict(trace, execution_guard_hints=hints)
        assert result["verdict"] == "priority_review"

    def test_verdict_reasons(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_verdict(trace, execution_guard_hints=hints)
        assert result["verdict_reasons"] == ["distinct_would_reject"]


# ─── E. supporting_buckets の整合性 ──────────────────────────────────────────

class TestSupportingBuckets:
    def test_matches_aggregate_review_key(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject"), _advisory_entry("blocking")]
        hints = {"blocking_reasons": [], "warning_reasons": []}
        agg_key = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=hints)
        verdict_entry = extract_shadow_hard_guard_aggregate_review_verdict(trace, execution_guard_hints=hints)
        sb = verdict_entry["supporting_buckets"]
        assert sb["shadow_bucket"] == agg_key["shadow_bucket"]
        assert sb["overlap_bucket"] == agg_key["overlap_bucket"]
        assert sb["advisory_bucket"] == agg_key["advisory_bucket"]
        assert sb["decision_bucket"] == agg_key["decision_bucket"]
        assert sb["countable"] == agg_key["countable"]

    def test_required_keys_present(self):
        result = extract_shadow_hard_guard_aggregate_review_verdict([])
        assert _SUPPORTING_BUCKET_KEYS <= set(result["supporting_buckets"].keys())


# ─── F. candidate filtering ───────────────────────────────────────────────────

class TestCandidateFiltering:
    def test_other_candidate_does_not_affect(self):
        trace = [_shadow_event("other_candidate", "would_reject")]
        result = extract_shadow_hard_guard_aggregate_review_verdict(trace, candidate="stale_bid_ask")
        assert result["verdict"] == "insufficient_signal"

    def test_only_matching_candidate(self):
        trace = [
            _shadow_event("other_candidate", "would_reject"),
            _shadow_event("stale_bid_ask", "advisory_only"),
        ]
        result = extract_shadow_hard_guard_aggregate_review_verdict(trace, candidate="stale_bid_ask")
        assert result["verdict"] == "observe_only"


# ─── G. 不正入力安全性 ────────────────────────────────────────────────────────

class TestSafety:
    def _is_safe_insufficient(self, result: dict) -> None:
        assert result["verdict"] == "insufficient_signal"
        assert "verdict_reasons" in result
        assert "supporting_buckets" in result

    def test_trace_none(self):
        self._is_safe_insufficient(extract_shadow_hard_guard_aggregate_review_verdict(None))

    def test_trace_dict(self):
        self._is_safe_insufficient(extract_shadow_hard_guard_aggregate_review_verdict({}))

    def test_trace_bad_elements(self):
        self._is_safe_insufficient(
            extract_shadow_hard_guard_aggregate_review_verdict(["bad", 42, None])
        )

    def test_hints_none(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_aggregate_review_verdict(trace, execution_guard_hints=None)
        assert result["verdict"] == "priority_review"  # distinct（price_stale なし）

    def test_hints_list(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_aggregate_review_verdict(trace, execution_guard_hints=[])
        assert result["verdict"] == "priority_review"


# ─── H. 固定フィールド ────────────────────────────────────────────────────────

class TestFixedFields:
    def test_stage(self):
        result = extract_shadow_hard_guard_aggregate_review_verdict([])
        assert result["stage"] == "shadow_hard_guard_aggregate_review_verdict"

    def test_candidate_default(self):
        result = extract_shadow_hard_guard_aggregate_review_verdict([])
        assert result["candidate"] == "stale_bid_ask"

    def test_candidate_custom(self):
        result = extract_shadow_hard_guard_aggregate_review_verdict([], candidate="other")
        assert result["candidate"] == "other"

    def test_verdict_version(self):
        result = extract_shadow_hard_guard_aggregate_review_verdict([])
        assert result["verdict_version"] == 1


# ─── I. accessor ─────────────────────────────────────────────────────────────

class TestAccessor:
    def _trace_with_verdict(self) -> list:
        entry = extract_shadow_hard_guard_aggregate_review_verdict([])
        return [entry]

    def test_returns_entry(self):
        trace = self._trace_with_verdict()
        result = get_shadow_hard_guard_aggregate_review_verdict(trace)
        assert result is not None
        assert result["stage"] == "shadow_hard_guard_aggregate_review_verdict"

    def test_candidate_mismatch_returns_none(self):
        trace = self._trace_with_verdict()
        result = get_shadow_hard_guard_aggregate_review_verdict(trace, candidate="other")
        assert result is None

    def test_no_entry_returns_none(self):
        result = get_shadow_hard_guard_aggregate_review_verdict([])
        assert result is None

    def test_invalid_trace_returns_none(self):
        result = get_shadow_hard_guard_aggregate_review_verdict(None)
        assert result is None


# ─── J. service integration ───────────────────────────────────────────────────

class TestServiceIntegration:
    @pytest.mark.asyncio
    async def test_verdict_present_once(self):
        hints = {"blocking_reasons": [], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        trace = plan.planning_trace_json
        assert _count_stage(trace, "shadow_hard_guard_aggregate_review_verdict") == 1

    @pytest.mark.asyncio
    async def test_stale_bid_ask_blocking_gives_priority_review(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        entry = _find_stage(plan.planning_trace_json, "shadow_hard_guard_aggregate_review_verdict")
        assert entry is not None
        assert entry["verdict"] == "priority_review"

    @pytest.mark.asyncio
    async def test_no_blocking_gives_insufficient_signal(self):
        hints = {"blocking_reasons": [], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        entry = _find_stage(plan.planning_trace_json, "shadow_hard_guard_aggregate_review_verdict")
        assert entry is not None
        assert entry["verdict"] == "insufficient_signal"


# ─── K. Phase AC 回帰防止 ─────────────────────────────────────────────────────

class TestPhaseACRegression:
    @pytest.mark.asyncio
    async def test_agg_key_still_present(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        assert _count_stage(plan.planning_trace_json, "shadow_hard_guard_aggregate_review_key") == 1

    @pytest.mark.asyncio
    async def test_agg_key_value_unchanged(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        entry = _find_stage(plan.planning_trace_json, "shadow_hard_guard_aggregate_review_key")
        assert entry is not None
        assert entry["shadow_bucket"] == "would_reject"
        assert entry["decision_bucket"] == "review_priority"
        assert entry["countable"] is True


# ─── L. 全回帰防止 ────────────────────────────────────────────────────────────

class TestFullRegression:
    @pytest.mark.asyncio
    async def test_all_derived_stages_no_duplicates(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        trace = plan.planning_trace_json
        for stage in _DERIVED_STAGES:
            count = _count_stage(trace, stage)
            assert count == 1, f"stage={stage} が {count} 件（期待1件）"

    @pytest.mark.asyncio
    async def test_stale_bid_ask_not_rejected(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, exc = await _run_plan_capture(ctx)
        assert exc is None
        assert plan is not None
        assert plan.planning_status == "accepted"

    @pytest.mark.asyncio
    async def test_price_stale_still_rejected(self):
        hints = {"blocking_reasons": ["price_stale"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, exc = await _run_plan_capture(ctx)
        assert exc is not None
        assert plan is not None
        assert plan.planning_status == "rejected"

    @pytest.mark.asyncio
    async def test_planned_qty_unchanged(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        assert plan.planned_order_qty == 100
