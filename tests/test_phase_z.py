"""
Phase Z — trace 正規化・読み出し helper テスト

確認項目:
  A. upsert_trace_stage — 基本
     1.  同一 stage がない場合は末尾に append される
     2.  同一 stage が存在する場合は除去されて末尾に1件だけ残る
     3.  元の trace は変更されない（新しい list が返る）
     4.  entry が dict でない場合は何もしない
     5.  entry に stage キーがない場合は何もしない
     6.  trace が list でない場合でも安全（空 list として扱う）

  B. decision event 非破壊
     7.  shadow_hard_guard_decision が存在する trace に
         shadow_hard_guard_assessment を upsert しても decision は残る
     8.  decision が複数あっても全て残る

  C. 同一 stage 複数件の正規化
     9.  advisory_guard_assessment が2件ある不正状態で upsert → 1件のみ残る
    10.  既存2件は全て除去されて新 entry だけが残る

  D. get_latest_stage_entry
    11. 該当 stage がある → dict を返す
    12. 該当 stage がない → None
    13. 同一 stage が複数ある → 最後の1件を返す
    14. trace = None → None
    15. trace = {} → None
    16. trace に非 dict 要素があっても安全

  E. get_shadow_hard_guard_assessment
    17. assessment entry がある → dict を返す
    18. ない → None
    19. 不正 trace → None

  F. get_shadow_hard_guard_review_summary
    20. candidate 一致 → dict を返す
    21. candidate 不一致 → None
    22. entry がない → None
    23. 不正 trace → None

  G. service integration — 派生 stage 重複なし
    24. _save_plan() 後に advisory_guard_assessment が1件のみ
    25. _save_plan() 後に shadow_hard_guard_assessment が1件のみ
    26. _save_plan() 後に shadow_hard_guard_review_summary が1件のみ

  H. Phase Y 回帰防止
    27. review summary の shadow_triggered / would_reject / readiness は変わらない
    28. promotion_blockers = [] / notes = [] は変わらない

  I. Phase X 回帰防止
    29. shadow assessment の event_count は変わらない
    30. candidates / would_reject_candidates は変わらない

  J. Phase W / V 回帰防止
    31. stale_bid_ask は reject しない
    32. price_stale は reject する
    33. planned_qty / planning_status / rejection_reason_code 不変
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
    get_latest_stage_entry,
    get_shadow_hard_guard_assessment,
    get_shadow_hard_guard_review_summary,
    upsert_trace_stage,
)

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _advisory(level: str = "none") -> dict:
    return {"stage": "advisory_guard_assessment", "guard_level": level, "guard_reasons": []}


def _shadow_decision(candidate: str = "stale_bid_ask", decision: str = "would_reject") -> dict:
    return {"stage": "shadow_hard_guard_decision", "candidate": candidate, "decision": decision}


def _shadow_assessment(event_count: int = 1) -> dict:
    return {
        "stage": "shadow_hard_guard_assessment",
        "has_shadow_candidate": event_count > 0,
        "candidates": ["stale_bid_ask"] if event_count > 0 else [],
        "would_reject_candidates": ["stale_bid_ask"] if event_count > 0 else [],
        "event_count": event_count,
    }


def _review_summary(candidate: str = "stale_bid_ask", readiness: str = "needs_review") -> dict:
    return {
        "stage": "shadow_hard_guard_review_summary",
        "candidate": candidate,
        "shadow_triggered": True,
        "would_reject": True,
        "promotion_readiness": readiness,
        "promotion_blockers": [],
        "notes": [],
    }


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


# ─── A. upsert_trace_stage — 基本 ────────────────────────────────────────────

class TestUpsertTraceStageBasic:
    def test_appends_when_stage_absent(self):
        trace = [{"stage": "base_size", "qty": 100}]
        result = upsert_trace_stage(trace, _advisory())
        assert _count_stage(result, "advisory_guard_assessment") == 1

    def test_replaces_existing_same_stage(self):
        trace = [_advisory("warning"), {"stage": "base_size"}]
        result = upsert_trace_stage(trace, _advisory("none"))
        assert _count_stage(result, "advisory_guard_assessment") == 1
        assert result[-1]["guard_level"] == "none"

    def test_does_not_mutate_original(self):
        original = [_advisory("warning")]
        _ = upsert_trace_stage(original, _advisory("none"))
        assert original[0]["guard_level"] == "warning"

    def test_entry_not_dict_returns_unchanged(self):
        trace = [{"stage": "base_size"}]
        result = upsert_trace_stage(trace, "bad")
        assert result == trace

    def test_entry_missing_stage_returns_unchanged(self):
        trace = [{"stage": "base_size"}]
        result = upsert_trace_stage(trace, {"no_stage": True})
        assert result == trace

    def test_non_list_trace_treated_as_empty(self):
        result = upsert_trace_stage(None, _advisory())
        assert result == [_advisory()]


# ─── B. decision event 非破壊 ────────────────────────────────────────────────

class TestDecisionEventPreserved:
    def test_decision_remains_after_assessment_upsert(self):
        trace = [_shadow_decision()]
        result = upsert_trace_stage(trace, _shadow_assessment())
        assert _count_stage(result, "shadow_hard_guard_decision") == 1

    def test_multiple_decisions_all_remain(self):
        trace = [_shadow_decision("stale_bid_ask"), _shadow_decision("wide_spread")]
        result = upsert_trace_stage(trace, _shadow_assessment())
        assert _count_stage(result, "shadow_hard_guard_decision") == 2


# ─── C. 同一 stage 複数件の正規化 ────────────────────────────────────────────

class TestMultipleSameStageNormalized:
    def test_two_existing_entries_become_one(self):
        trace = [_advisory("warning"), _advisory("blocking")]
        result = upsert_trace_stage(trace, _advisory("none"))
        assert _count_stage(result, "advisory_guard_assessment") == 1

    def test_new_entry_is_the_remaining_one(self):
        trace = [_advisory("warning"), _advisory("blocking")]
        result = upsert_trace_stage(trace, _advisory("none"))
        entry = next(e for e in result if e.get("stage") == "advisory_guard_assessment")
        assert entry["guard_level"] == "none"


# ─── D. get_latest_stage_entry ───────────────────────────────────────────────

class TestGetLatestStageEntry:
    def test_returns_dict_when_found(self):
        trace = [_advisory("none")]
        result = get_latest_stage_entry(trace, "advisory_guard_assessment")
        assert result is not None
        assert result["guard_level"] == "none"

    def test_returns_none_when_absent(self):
        trace = [{"stage": "base_size"}]
        assert get_latest_stage_entry(trace, "advisory_guard_assessment") is None

    def test_returns_last_when_multiple(self):
        trace = [_advisory("warning"), _advisory("none")]
        result = get_latest_stage_entry(trace, "advisory_guard_assessment")
        assert result["guard_level"] == "none"

    def test_none_trace_returns_none(self):
        assert get_latest_stage_entry(None, "advisory_guard_assessment") is None

    def test_dict_trace_returns_none(self):
        assert get_latest_stage_entry({}, "advisory_guard_assessment") is None

    def test_non_dict_element_skipped_safely(self):
        trace = ["bad", 42, _advisory("none")]
        result = get_latest_stage_entry(trace, "advisory_guard_assessment")
        assert result is not None


# ─── E. get_shadow_hard_guard_assessment ─────────────────────────────────────

class TestGetShadowHardGuardAssessment:
    def test_returns_entry_when_present(self):
        trace = [_shadow_assessment(1)]
        result = get_shadow_hard_guard_assessment(trace)
        assert result is not None
        assert result["event_count"] == 1

    def test_returns_none_when_absent(self):
        assert get_shadow_hard_guard_assessment([]) is None

    def test_invalid_trace_returns_none(self):
        assert get_shadow_hard_guard_assessment(None) is None


# ─── F. get_shadow_hard_guard_review_summary ─────────────────────────────────

class TestGetShadowHardGuardReviewSummary:
    def test_returns_entry_when_candidate_matches(self):
        trace = [_review_summary("stale_bid_ask")]
        result = get_shadow_hard_guard_review_summary(trace, "stale_bid_ask")
        assert result is not None
        assert result["candidate"] == "stale_bid_ask"

    def test_returns_none_when_candidate_mismatch(self):
        trace = [_review_summary("stale_bid_ask")]
        result = get_shadow_hard_guard_review_summary(trace, "wide_spread")
        assert result is None

    def test_returns_none_when_absent(self):
        assert get_shadow_hard_guard_review_summary([], "stale_bid_ask") is None

    def test_invalid_trace_returns_none(self):
        assert get_shadow_hard_guard_review_summary(None, "stale_bid_ask") is None


# ─── G. service integration — 派生 stage 重複なし ────────────────────────────

class TestServiceIntegrationNoDuplicates:
    @pytest.mark.asyncio
    async def test_advisory_stage_appears_once(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _count_stage(plan.planning_trace_json, "advisory_guard_assessment") == 1

    @pytest.mark.asyncio
    async def test_shadow_assessment_stage_appears_once(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _count_stage(plan.planning_trace_json, "shadow_hard_guard_assessment") == 1

    @pytest.mark.asyncio
    async def test_review_summary_stage_appears_once(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _count_stage(plan.planning_trace_json, "shadow_hard_guard_review_summary") == 1


# ─── H. Phase Y 回帰防止 ─────────────────────────────────────────────────────

class TestPhaseYRegression:
    @pytest.mark.asyncio
    async def test_review_summary_values_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_review_summary(plan.planning_trace_json)
        assert entry["shadow_triggered"] is True
        assert entry["would_reject"] is True
        assert entry["promotion_readiness"] == "needs_review"

    @pytest.mark.asyncio
    async def test_review_summary_blockers_and_notes_empty(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_review_summary(plan.planning_trace_json)
        assert entry["promotion_blockers"] == []
        assert entry["notes"] == []


# ─── I. Phase X 回帰防止 ─────────────────────────────────────────────────────

class TestPhaseXRegression:
    @pytest.mark.asyncio
    async def test_assessment_event_count_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_assessment(plan.planning_trace_json)
        assert entry["event_count"] == 1

    @pytest.mark.asyncio
    async def test_assessment_candidates_unchanged(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = get_shadow_hard_guard_assessment(plan.planning_trace_json)
        assert entry["candidates"] == ["stale_bid_ask"]
        assert entry["would_reject_candidates"] == ["stale_bid_ask"]


# ─── J. Phase W / V 回帰防止 ─────────────────────────────────────────────────

class TestPhaseWVRegression:
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
