"""
Phase X — shadow_hard_guard_assessment 導出テスト

確認項目:
  A. extract_shadow_hard_guard_assessment — 正常系
     1.  shadow trace 1件（stale_bid_ask / would_reject）の基本結果
     2.  has_shadow_candidate = True
     3.  candidates = ["stale_bid_ask"]
     4.  would_reject_candidates = ["stale_bid_ask"]
     5.  event_count = 1

  B. 重複排除
     6.  同一 candidate 複数回 → candidates は重複排除
     7.  同一 candidate 複数回 → would_reject_candidates は重複排除
     8.  event_count はイベント総数（重複排除しない）

  C. 将来 candidate 互換
     9.  複数 candidate → candidates に全て含まれる（出現順）
    10.  複数 candidate → would_reject_candidates に全て含まれる
    11.  decision != "would_reject" は would_reject_candidates に含まれない

  D. shadow trace なし
    12. shadow イベントなし → has_shadow_candidate = False
    13. shadow イベントなし → candidates = []
    14. shadow イベントなし → would_reject_candidates = []
    15. shadow イベントなし → event_count = 0

  E. 不正 trace 耐性
    16. trace = None → 安全に空評価
    17. trace = {} (非 list) → 安全に空評価
    18. trace = ["bad"] (非 dict 要素) → 安全に空評価
    19. dict だが stage キーなし → 無視して安全側
    20. stage はあるが candidate キーなし → 無視して安全側
    21. stage / candidate はあるが decision キーなし → 無視して安全側
    22. candidate が文字列でない → 無視して安全側

  F. service.py 統合（planning_trace_json への注入）
    23. stale_bid_ask blocking → planning_trace_json に shadow_hard_guard_assessment ステージが存在
    24. shadow_hard_guard_assessment の has_shadow_candidate = True
    25. shadow_hard_guard_assessment の candidates = ["stale_bid_ask"]
    26. shadow_hard_guard_assessment の event_count = 1
    27. shadow trace なし → shadow_hard_guard_assessment の has_shadow_candidate = False

  G. Phase W 回帰防止
    28. stale_bid_ask shadow は依然 reject しない
    29. shadow_hard_guard_assessment 追加後も planned_qty は変わらない
    30. shadow_hard_guard_assessment 追加後も planning_status は変わらない
    31. shadow_hard_guard_assessment 追加後も rejection_reason_code = None

  H. Phase V 回帰防止
    32. price_stale は依然 reject する
    33. price_stale reject 時の rejection_reason_code は "execution_guard_price_stale"
    34. shadow_hard_guard_assessment の追加で price_stale reject 動作は変わらない
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
from trade_app.services.planning.trace_helpers import extract_shadow_hard_guard_assessment

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _shadow_event(candidate: str = "stale_bid_ask", decision: str = "would_reject") -> dict:
    return {
        "stage":     "shadow_hard_guard_decision",
        "candidate": candidate,
        "decision":  decision,
        "reason":    "execution_guard_stale_bid_ask_shadow",
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
    service, _ = _make_service_capture(ctx)
    return await service.plan(ctx.signal, ctx)


async def _run_plan_capture(ctx: PlannerContext):
    service, saved_plans = _make_service_capture(ctx)
    try:
        plan = await service.plan(ctx.signal, ctx)
        return plan, None
    except SignalPlanRejectedError as exc:
        return saved_plans[0] if saved_plans else None, exc


def _get_shadow_assessment(plan) -> dict | None:
    return next(
        (e for e in plan.planning_trace_json if e.get("stage") == "shadow_hard_guard_assessment"),
        None,
    )


# ─── A. 正常系（1件）────────────────────────────────────────────────────────

class TestSingleEvent:
    def test_basic_result_type(self):
        trace = [_shadow_event()]
        result = extract_shadow_hard_guard_assessment(trace)
        assert isinstance(result, dict)

    def test_has_shadow_candidate_true(self):
        trace = [_shadow_event()]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["has_shadow_candidate"] is True

    def test_candidates(self):
        trace = [_shadow_event()]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["candidates"] == ["stale_bid_ask"]

    def test_would_reject_candidates(self):
        trace = [_shadow_event()]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["would_reject_candidates"] == ["stale_bid_ask"]

    def test_event_count(self):
        trace = [_shadow_event()]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["event_count"] == 1


# ─── B. 重複排除 ─────────────────────────────────────────────────────────────

class TestDeduplication:
    def test_candidates_deduplicated(self):
        trace = [_shadow_event(), _shadow_event()]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["candidates"] == ["stale_bid_ask"]

    def test_would_reject_deduplicated(self):
        trace = [_shadow_event(), _shadow_event()]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["would_reject_candidates"] == ["stale_bid_ask"]

    def test_event_count_not_deduplicated(self):
        trace = [_shadow_event(), _shadow_event()]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["event_count"] == 2


# ─── C. 将来 candidate 互換 ──────────────────────────────────────────────────

class TestMultipleCandidates:
    def test_candidates_all_present_in_order(self):
        trace = [_shadow_event("stale_bid_ask"), _shadow_event("wide_spread")]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["candidates"] == ["stale_bid_ask", "wide_spread"]

    def test_would_reject_all_present(self):
        trace = [_shadow_event("stale_bid_ask"), _shadow_event("wide_spread")]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["would_reject_candidates"] == ["stale_bid_ask", "wide_spread"]

    def test_non_would_reject_excluded_from_would_reject_list(self):
        trace = [
            _shadow_event("stale_bid_ask", "would_reject"),
            _shadow_event("wide_spread", "advisory_only"),
        ]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["candidates"] == ["stale_bid_ask", "wide_spread"]
        assert result["would_reject_candidates"] == ["stale_bid_ask"]


# ─── D. shadow trace なし ─────────────────────────────────────────────────────

class TestNoShadowEvents:
    def test_has_shadow_candidate_false(self):
        trace = [{"stage": "advisory_guard_assessment", "guard_level": "none"}]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["has_shadow_candidate"] is False

    def test_candidates_empty(self):
        result = extract_shadow_hard_guard_assessment([])
        assert result["candidates"] == []

    def test_would_reject_candidates_empty(self):
        result = extract_shadow_hard_guard_assessment([])
        assert result["would_reject_candidates"] == []

    def test_event_count_zero(self):
        result = extract_shadow_hard_guard_assessment([])
        assert result["event_count"] == 0


# ─── E. 不正 trace 耐性 ───────────────────────────────────────────────────────

class TestInvalidTraceSafety:
    def test_none_returns_empty(self):
        result = extract_shadow_hard_guard_assessment(None)
        assert result["has_shadow_candidate"] is False
        assert result["event_count"] == 0

    def test_dict_returns_empty(self):
        result = extract_shadow_hard_guard_assessment({})
        assert result["has_shadow_candidate"] is False

    def test_non_dict_element_ignored(self):
        result = extract_shadow_hard_guard_assessment(["bad", 42, None])
        assert result["has_shadow_candidate"] is False
        assert result["event_count"] == 0

    def test_missing_stage_key_ignored(self):
        trace = [{"candidate": "stale_bid_ask", "decision": "would_reject"}]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["event_count"] == 0

    def test_missing_candidate_key_ignored(self):
        trace = [{"stage": "shadow_hard_guard_decision", "decision": "would_reject"}]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["event_count"] == 0

    def test_missing_decision_key_ignored(self):
        trace = [{"stage": "shadow_hard_guard_decision", "candidate": "stale_bid_ask"}]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["event_count"] == 0

    def test_non_string_candidate_ignored(self):
        trace = [{"stage": "shadow_hard_guard_decision", "candidate": 123, "decision": "would_reject"}]
        result = extract_shadow_hard_guard_assessment(trace)
        assert result["event_count"] == 0


# ─── F. service.py 統合 ───────────────────────────────────────────────────────

class TestServiceIntegration:
    @pytest.mark.asyncio
    async def test_shadow_assessment_stage_in_trace_when_stale_bid_ask(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert _get_shadow_assessment(plan) is not None

    @pytest.mark.asyncio
    async def test_shadow_assessment_has_shadow_candidate_true(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_shadow_assessment(plan)
        assert entry["has_shadow_candidate"] is True

    @pytest.mark.asyncio
    async def test_shadow_assessment_candidates(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_shadow_assessment(plan)
        assert entry["candidates"] == ["stale_bid_ask"]

    @pytest.mark.asyncio
    async def test_shadow_assessment_event_count(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_shadow_assessment(plan)
        assert entry["event_count"] == 1

    @pytest.mark.asyncio
    async def test_shadow_assessment_false_when_no_stale_bid_ask(self):
        ctx = _make_context({"blocking_reasons": [], "warning_reasons": []})
        plan = await _run_plan(ctx)
        entry = _get_shadow_assessment(plan)
        assert entry is not None
        assert entry["has_shadow_candidate"] is False


# ─── G. Phase W 回帰防止 ─────────────────────────────────────────────────────

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
    async def test_rejection_reason_code_is_none(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_plan(ctx)
        assert plan.rejection_reason_code is None


# ─── H. Phase V 回帰防止 ─────────────────────────────────────────────────────

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

    @pytest.mark.asyncio
    async def test_shadow_assessment_does_not_change_price_stale_reject(self):
        """shadow_hard_guard_assessment 追加後も price_stale の reject 動作は不変"""
        ctx = _make_context({"blocking_reasons": ["price_stale", "stale_bid_ask"], "warning_reasons": []})
        plan, err = await _run_plan_capture(ctx)
        assert err is not None
        assert isinstance(err, SignalPlanRejectedError)
        assert err.reason_code == PlanningReasonCode.EXECUTION_GUARD_PRICE_STALE
