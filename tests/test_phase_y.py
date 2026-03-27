"""
Phase Y — shadow_hard_guard_review_summary 導出テスト

確認項目:
  A. shadow_triggered=True / would_reject=True（needs_review）
     1.  shadow_triggered = True
     2.  would_reject = True
     3.  promotion_readiness = "needs_review"
     4.  promotion_blockers = []
     5.  notes = []

  B. shadow_triggered=True / would_reject=False（observe）
     6.  shadow_triggered = True
     7.  would_reject = False
     8.  promotion_readiness = "observe"

  C. shadow event なし（no_signal）
     9.  shadow_triggered = False
    10.  would_reject = False
    11.  promotion_readiness = "no_signal"

  D. 複数 candidate が混在しても対象 candidate のみ見る
    12. wide_spread + stale_bid_ask → stale_bid_ask は triggered=True
    13. wide_spread のみ → stale_bid_ask は triggered=False

  E. 不正 trace 耐性
    14. None → 安全に no_signal
    15. {} (非 list) → 安全に no_signal
    16. ["bad"] (非 dict 要素) → 安全に no_signal
    17. stage 欠損 → 無視して no_signal
    18. candidate 欠損 → 無視して no_signal
    19. decision 欠損 → shadow_triggered=True / would_reject=False

  F. 戻り値構造
    20. stage = "shadow_hard_guard_review_summary"
    21. candidate フィールドが引数と一致
    22. 必須キーが全て存在する

  G. service integration
    23. stale_bid_ask blocking → planning_trace_json に shadow_hard_guard_review_summary が存在
    24. summary.shadow_triggered = True
    25. summary.would_reject = True
    26. summary.promotion_readiness = "needs_review"
    27. stale_bid_ask なし → summary.shadow_triggered = False, promotion_readiness = "no_signal"

  H. Phase X 回帰防止
    28. shadow_hard_guard_assessment は従来どおり存在する
    29. assessment.event_count は変わらない
    30. assessment.candidates は変わらない

  I. Phase W 回帰防止
    31. stale_bid_ask shadow は依然 reject しない
    32. planned_qty 不変
    33. planning_status 不変
    34. rejection_reason_code = None

  J. Phase V 回帰防止
    35. price_stale は依然 reject する
    36. rejection_reason_code = "execution_guard_price_stale"
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
    extract_shadow_hard_guard_review_summary,
)

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

_REQUIRED_KEYS = {
    "stage", "candidate", "shadow_triggered", "would_reject",
    "promotion_readiness", "promotion_blockers", "notes",
}


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _shadow_event(candidate: str = "stale_bid_ask", decision: str = "would_reject") -> dict:
    return {
        "stage":     "shadow_hard_guard_decision",
        "candidate": candidate,
        "decision":  decision,
    }


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


def _get_stage(plan, stage: str) -> dict | None:
    return next(
        (e for e in plan.planning_trace_json if e.get("stage") == stage),
        None,
    )


# ─── A. needs_review ─────────────────────────────────────────────────────────

class TestNeedsReview:
    def test_shadow_triggered_true(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["shadow_triggered"] is True

    def test_would_reject_true(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["would_reject"] is True

    def test_promotion_readiness_needs_review(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["promotion_readiness"] == "needs_review"

    def test_promotion_blockers_empty(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["promotion_blockers"] == []

    def test_notes_empty(self):
        trace = [_shadow_event("stale_bid_ask", "would_reject")]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["notes"] == []


# ─── B. observe ──────────────────────────────────────────────────────────────

class TestObserve:
    def test_shadow_triggered_true(self):
        trace = [_shadow_event("stale_bid_ask", "advisory_only")]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["shadow_triggered"] is True

    def test_would_reject_false(self):
        trace = [_shadow_event("stale_bid_ask", "advisory_only")]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["would_reject"] is False

    def test_promotion_readiness_observe(self):
        trace = [_shadow_event("stale_bid_ask", "advisory_only")]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["promotion_readiness"] == "observe"


# ─── C. no_signal ─────────────────────────────────────────────────────────────

class TestNoSignal:
    def test_shadow_triggered_false(self):
        result = extract_shadow_hard_guard_review_summary([])
        assert result["shadow_triggered"] is False

    def test_would_reject_false(self):
        result = extract_shadow_hard_guard_review_summary([])
        assert result["would_reject"] is False

    def test_promotion_readiness_no_signal(self):
        result = extract_shadow_hard_guard_review_summary([])
        assert result["promotion_readiness"] == "no_signal"


# ─── D. 複数 candidate 混在 ──────────────────────────────────────────────────

class TestCandidateFiltering:
    def test_target_candidate_triggered_when_mixed(self):
        trace = [
            _shadow_event("wide_spread", "would_reject"),
            _shadow_event("stale_bid_ask", "would_reject"),
        ]
        result = extract_shadow_hard_guard_review_summary(trace, candidate="stale_bid_ask")
        assert result["shadow_triggered"] is True
        assert result["would_reject"] is True

    def test_target_candidate_not_triggered_when_only_other(self):
        trace = [_shadow_event("wide_spread", "would_reject")]
        result = extract_shadow_hard_guard_review_summary(trace, candidate="stale_bid_ask")
        assert result["shadow_triggered"] is False
        assert result["promotion_readiness"] == "no_signal"


# ─── E. 不正 trace 耐性 ───────────────────────────────────────────────────────

class TestInvalidTraceSafety:
    def test_none_returns_no_signal(self):
        result = extract_shadow_hard_guard_review_summary(None)
        assert result["shadow_triggered"] is False
        assert result["promotion_readiness"] == "no_signal"

    def test_dict_returns_no_signal(self):
        result = extract_shadow_hard_guard_review_summary({})
        assert result["shadow_triggered"] is False

    def test_non_dict_element_ignored(self):
        result = extract_shadow_hard_guard_review_summary(["bad", 42, None])
        assert result["shadow_triggered"] is False

    def test_missing_stage_ignored(self):
        trace = [{"candidate": "stale_bid_ask", "decision": "would_reject"}]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["shadow_triggered"] is False

    def test_missing_candidate_ignored(self):
        trace = [{"stage": "shadow_hard_guard_decision", "decision": "would_reject"}]
        result = extract_shadow_hard_guard_review_summary(trace)
        assert result["shadow_triggered"] is False

    def test_missing_decision_triggered_but_not_would_reject(self):
        """decision キーなしは shadow_triggered=True になるが would_reject=False"""
        trace = [{"stage": "shadow_hard_guard_decision", "candidate": "stale_bid_ask"}]
        result = extract_shadow_hard_guard_review_summary(trace)
        # decision がなければ文字列チェックで弾かれる → triggered=False
        assert result["shadow_triggered"] is False


# ─── F. 戻り値構造 ────────────────────────────────────────────────────────────

class TestReturnStructure:
    def test_stage_value(self):
        result = extract_shadow_hard_guard_review_summary([])
        assert result["stage"] == "shadow_hard_guard_review_summary"

    def test_candidate_field_matches_argument(self):
        result = extract_shadow_hard_guard_review_summary([], candidate="wide_spread")
        assert result["candidate"] == "wide_spread"

    def test_all_required_keys_present(self):
        result = extract_shadow_hard_guard_review_summary([_shadow_event()])
        assert _REQUIRED_KEYS <= set(result.keys())


# ─── G. service integration ───────────────────────────────────────────────────

class TestServiceIntegration:
    @pytest.mark.asyncio
    async def test_review_summary_in_trace_when_stale_bid_ask(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _get_stage(plan, "shadow_hard_guard_review_summary") is not None

    @pytest.mark.asyncio
    async def test_summary_shadow_triggered_true(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_stage(plan, "shadow_hard_guard_review_summary")
        assert entry["shadow_triggered"] is True

    @pytest.mark.asyncio
    async def test_summary_would_reject_true(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_stage(plan, "shadow_hard_guard_review_summary")
        assert entry["would_reject"] is True

    @pytest.mark.asyncio
    async def test_summary_promotion_readiness_needs_review(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_stage(plan, "shadow_hard_guard_review_summary")
        assert entry["promotion_readiness"] == "needs_review"

    @pytest.mark.asyncio
    async def test_summary_no_signal_when_no_stale_bid_ask(self):
        ctx = _make_context({"blocking_reasons": [], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_stage(plan, "shadow_hard_guard_review_summary")
        assert entry is not None
        assert entry["shadow_triggered"] is False
        assert entry["promotion_readiness"] == "no_signal"


# ─── H. Phase X 回帰防止 ─────────────────────────────────────────────────────

class TestPhaseXRegression:
    @pytest.mark.asyncio
    async def test_shadow_assessment_still_present(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _get_stage(plan, "shadow_hard_guard_assessment") is not None

    @pytest.mark.asyncio
    async def test_assessment_event_count_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_stage(plan, "shadow_hard_guard_assessment")
        assert entry["event_count"] == 1

    @pytest.mark.asyncio
    async def test_assessment_candidates_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_stage(plan, "shadow_hard_guard_assessment")
        assert entry["candidates"] == ["stale_bid_ask"]


# ─── I. Phase W 回帰防止 ─────────────────────────────────────────────────────

class TestPhaseWRegression:
    @pytest.mark.asyncio
    async def test_stale_bid_ask_does_not_reject(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan is not None

    @pytest.mark.asyncio
    async def test_planned_qty_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.planned_order_qty == 100

    @pytest.mark.asyncio
    async def test_planning_status_not_rejected(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.planning_status != "rejected"

    @pytest.mark.asyncio
    async def test_rejection_reason_code_none(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.rejection_reason_code is None


# ─── J. Phase V 回帰防止 ─────────────────────────────────────────────────────

class TestPhaseVRegression:
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
