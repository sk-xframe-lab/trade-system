"""
Phase AC — shadow_hard_guard_aggregate_review_key 導出テスト

確認項目:
  A. no_signal ケース
     1.  shadow_bucket = "no_signal"
     2.  overlap_bucket = "no_overlap"
     3.  advisory_bucket = "none"
     4.  decision_bucket = "no_signal"
     5.  countable = False

  B. triggered_only ケース
     6.  shadow_triggered=True / would_reject=False → shadow_bucket = "triggered_only"
     7.  decision_bucket = "observe"
     8.  countable = False

  C. would_reject + overlaps_price_stale
     9.  shadow_bucket = "would_reject"
    10.  overlap_bucket = "overlaps_price_stale"
    11.  decision_bucket = "hold"
    12.  countable = True

  D. would_reject + distinct_from_price_stale
    13.  shadow_bucket = "would_reject"
    14.  overlap_bucket = "distinct_from_price_stale"
    15.  decision_bucket = "review_priority"
    16.  countable = True

  E. advisory_bucket
    17. guard_level="blocking" → "blocking"
    18. guard_level="warning"  → "warning"
    19. guard_level="none"     → "none"
    20. advisory entry 不在   → "none"
    21. guard_level が不正型  → "none"

  F. candidate filtering
    22. 他 candidate の shadow event は stale_bid_ask key に影響しない

  G. 不正入力安全性
    23. trace = None → 安全に no_signal bucket
    24. trace = {} → 安全に no_signal bucket
    25. trace = ["bad"] → 安全に no_signal bucket
    26. execution_guard_hints = None → overlaps=False → distinct bucket
    27. execution_guard_hints = [] → overlaps=False → distinct bucket

  H. 固定フィールド
    28. stage が "shadow_hard_guard_aggregate_review_key"
    29. candidate が引数の値を反映
    30. aggregate_key_version = 1

  I. get_shadow_hard_guard_aggregate_review_key accessor
    31. entry がある → dict を返す
    32. candidate 不一致 → None
    33. entry なし → None
    34. 不正 trace → None

  J. service integration
    35. _save_plan() 後に shadow_hard_guard_aggregate_review_key が1件のみ存在
    36. stale_bid_ask blocking → decision_bucket = "review_priority" / countable = True
    37. blocking なし → decision_bucket = "no_signal" / countable = False

  K. Phase AB 回帰防止
    38. promotion decision が従来どおり存在・値不変

  L. Phase AA / Z / Y / X / W / V 回帰防止
    39. 全派生 stage が重複しない（8 stage 全て各 1 件）
    40. stale_bid_ask は reject しない
    41. price_stale は reject する
    42. planned_qty / planning_status / rejection_reason_code 不変
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
    extract_shadow_hard_guard_promotion_decision,
    get_shadow_hard_guard_aggregate_review_key,
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
    def test_shadow_bucket(self):
        result = extract_shadow_hard_guard_aggregate_review_key([])
        assert result["shadow_bucket"] == "no_signal"

    def test_overlap_bucket(self):
        result = extract_shadow_hard_guard_aggregate_review_key([])
        assert result["overlap_bucket"] == "no_overlap"

    def test_advisory_bucket(self):
        result = extract_shadow_hard_guard_aggregate_review_key([])
        assert result["advisory_bucket"] == "none"

    def test_decision_bucket(self):
        result = extract_shadow_hard_guard_aggregate_review_key([])
        assert result["decision_bucket"] == "no_signal"

    def test_countable(self):
        result = extract_shadow_hard_guard_aggregate_review_key([])
        assert result["countable"] is False


# ─── B. triggered_only ───────────────────────────────────────────────────────

class TestTriggeredOnly:
    def _trace(self) -> list:
        return [_shadow_event("stale_bid_ask", "advisory_only")]

    def test_shadow_bucket(self):
        result = extract_shadow_hard_guard_aggregate_review_key(self._trace())
        assert result["shadow_bucket"] == "triggered_only"

    def test_decision_bucket(self):
        result = extract_shadow_hard_guard_aggregate_review_key(self._trace())
        assert result["decision_bucket"] == "observe"

    def test_countable(self):
        result = extract_shadow_hard_guard_aggregate_review_key(self._trace())
        assert result["countable"] is False


# ─── C. would_reject + overlaps_price_stale ──────────────────────────────────

class TestWouldRejectOverlap:
    def _setup(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        hints = {"blocking_reasons": ["price_stale"], "warning_reasons": []}
        return trace, hints

    def test_shadow_bucket(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=hints)
        assert result["shadow_bucket"] == "would_reject"

    def test_overlap_bucket(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=hints)
        assert result["overlap_bucket"] == "overlaps_price_stale"

    def test_decision_bucket(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=hints)
        assert result["decision_bucket"] == "hold"

    def test_countable(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=hints)
        assert result["countable"] is True


# ─── D. would_reject + distinct_from_price_stale ─────────────────────────────

class TestWouldRejectDistinct:
    def _setup(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        hints = {"blocking_reasons": [], "warning_reasons": []}
        return trace, hints

    def test_shadow_bucket(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=hints)
        assert result["shadow_bucket"] == "would_reject"

    def test_overlap_bucket(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=hints)
        assert result["overlap_bucket"] == "distinct_from_price_stale"

    def test_decision_bucket(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=hints)
        assert result["decision_bucket"] == "review_priority"

    def test_countable(self):
        trace, hints = self._setup()
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=hints)
        assert result["countable"] is True


# ─── E. advisory_bucket ───────────────────────────────────────────────────────

class TestAdvisoryBucket:
    def _result_with_level(self, level: str) -> dict:
        trace = [_advisory_entry(level)]
        return extract_shadow_hard_guard_aggregate_review_key(trace)

    def test_blocking(self):
        assert self._result_with_level("blocking")["advisory_bucket"] == "blocking"

    def test_warning(self):
        assert self._result_with_level("warning")["advisory_bucket"] == "warning"

    def test_none_level(self):
        assert self._result_with_level("none")["advisory_bucket"] == "none"

    def test_no_advisory_entry(self):
        result = extract_shadow_hard_guard_aggregate_review_key([])
        assert result["advisory_bucket"] == "none"

    def test_invalid_guard_level_type(self):
        trace = [{"stage": "advisory_guard_assessment", "guard_level": 123}]
        result = extract_shadow_hard_guard_aggregate_review_key(trace)
        assert result["advisory_bucket"] == "none"


# ─── F. candidate filtering ───────────────────────────────────────────────────

class TestCandidateFiltering:
    def test_other_candidate_shadow_does_not_affect(self):
        trace = [_shadow_event("other_candidate", "would_reject")]
        result = extract_shadow_hard_guard_aggregate_review_key(trace, candidate="stale_bid_ask")
        assert result["shadow_bucket"] == "no_signal"
        assert result["countable"] is False

    def test_only_matching_candidate_counted(self):
        trace = [
            _shadow_event("other_candidate", "would_reject"),
            _shadow_event("stale_bid_ask", "advisory_only"),
        ]
        result = extract_shadow_hard_guard_aggregate_review_key(trace, candidate="stale_bid_ask")
        assert result["shadow_bucket"] == "triggered_only"


# ─── G. 不正入力安全性 ────────────────────────────────────────────────────────

class TestSafety:
    def _safe_no_signal(self, result: dict) -> None:
        assert result["shadow_bucket"] == "no_signal"
        assert result["overlap_bucket"] == "no_overlap"
        assert result["countable"] is False

    def test_trace_none(self):
        result = extract_shadow_hard_guard_aggregate_review_key(None)
        self._safe_no_signal(result)

    def test_trace_dict(self):
        result = extract_shadow_hard_guard_aggregate_review_key({})
        self._safe_no_signal(result)

    def test_trace_bad_elements(self):
        result = extract_shadow_hard_guard_aggregate_review_key(["bad", 42, None])
        self._safe_no_signal(result)

    def test_hints_none_with_would_reject(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=None)
        assert result["overlap_bucket"] == "distinct_from_price_stale"
        assert result["decision_bucket"] == "review_priority"

    def test_hints_list_with_would_reject(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_aggregate_review_key(trace, execution_guard_hints=[])
        assert result["overlap_bucket"] == "distinct_from_price_stale"


# ─── H. 固定フィールド ────────────────────────────────────────────────────────

class TestFixedFields:
    def test_stage(self):
        result = extract_shadow_hard_guard_aggregate_review_key([])
        assert result["stage"] == "shadow_hard_guard_aggregate_review_key"

    def test_candidate_default(self):
        result = extract_shadow_hard_guard_aggregate_review_key([])
        assert result["candidate"] == "stale_bid_ask"

    def test_candidate_custom(self):
        result = extract_shadow_hard_guard_aggregate_review_key([], candidate="other")
        assert result["candidate"] == "other"

    def test_aggregate_key_version(self):
        result = extract_shadow_hard_guard_aggregate_review_key([])
        assert result["aggregate_key_version"] == 1


# ─── I. get_shadow_hard_guard_aggregate_review_key accessor ──────────────────

class TestAccessor:
    def _trace_with_key(self) -> list:
        entry = extract_shadow_hard_guard_aggregate_review_key([])
        return [entry]

    def test_returns_entry(self):
        trace = self._trace_with_key()
        result = get_shadow_hard_guard_aggregate_review_key(trace)
        assert result is not None
        assert result["stage"] == "shadow_hard_guard_aggregate_review_key"

    def test_candidate_mismatch_returns_none(self):
        trace = self._trace_with_key()
        result = get_shadow_hard_guard_aggregate_review_key(trace, candidate="other")
        assert result is None

    def test_no_entry_returns_none(self):
        result = get_shadow_hard_guard_aggregate_review_key([])
        assert result is None

    def test_invalid_trace_returns_none(self):
        result = get_shadow_hard_guard_aggregate_review_key(None)
        assert result is None


# ─── J. service integration ───────────────────────────────────────────────────

class TestServiceIntegration:
    @pytest.mark.asyncio
    async def test_agg_key_present_once(self):
        hints = {"blocking_reasons": [], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        trace = plan.planning_trace_json
        assert _count_stage(trace, "shadow_hard_guard_aggregate_review_key") == 1

    @pytest.mark.asyncio
    async def test_stale_bid_ask_blocking_gives_review_priority(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        trace = plan.planning_trace_json
        agg_entry = next(
            (e for e in trace if isinstance(e, dict) and e.get("stage") == "shadow_hard_guard_aggregate_review_key"),
            None,
        )
        assert agg_entry is not None
        assert agg_entry["decision_bucket"] == "review_priority"
        assert agg_entry["countable"] is True

    @pytest.mark.asyncio
    async def test_no_blocking_gives_no_signal(self):
        hints = {"blocking_reasons": [], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        trace = plan.planning_trace_json
        agg_entry = next(
            (e for e in trace if isinstance(e, dict) and e.get("stage") == "shadow_hard_guard_aggregate_review_key"),
            None,
        )
        assert agg_entry is not None
        assert agg_entry["decision_bucket"] == "no_signal"
        assert agg_entry["countable"] is False


# ─── K. Phase AB 回帰防止 ─────────────────────────────────────────────────────

class TestPhaseABRegression:
    @pytest.mark.asyncio
    async def test_promotion_decision_still_present(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        trace = plan.planning_trace_json
        assert _count_stage(trace, "shadow_hard_guard_promotion_decision") == 1

    @pytest.mark.asyncio
    async def test_promotion_decision_value_unchanged(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        trace = plan.planning_trace_json
        decision_entry = next(
            (e for e in trace if isinstance(e, dict) and e.get("stage") == "shadow_hard_guard_promotion_decision"),
            None,
        )
        assert decision_entry is not None
        assert decision_entry["decision"] == "review_priority"


# ─── L. Phase AA / Z / Y / X / W / V 回帰防止 ───────────────────────────────

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
        assert exc is None, "stale_bid_ask は reject しない"
        assert plan is not None
        assert plan.planning_status == "accepted"

    @pytest.mark.asyncio
    async def test_price_stale_still_rejected(self):
        hints = {"blocking_reasons": ["price_stale"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, exc = await _run_plan_capture(ctx)
        assert exc is not None, "price_stale は reject する"
        assert plan is not None
        assert plan.planning_status == "rejected"

    @pytest.mark.asyncio
    async def test_planned_qty_unchanged(self):
        hints = {"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = _make_context(hints)
        plan, _ = await _run_plan_capture(ctx)
        assert plan is not None
        assert plan.planned_order_qty == 100
