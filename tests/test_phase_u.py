"""
Phase U — advisory guard assessment テスト

確認項目:
  A. _build_advisory_guard_assessment() 単体テスト
     1.  hints が空 / has_quote_risk=False → guard_level="none"
     2.  warning のみ → guard_level="warning" / guard_reasons=warning
     3.  blocking のみ → guard_level="blocking" / guard_reasons=blocking
     4.  blocking + warning → guard_level="blocking" / guard_reasons=blocking+warning
     5.  blocking + warning で重複なし
     6.  空 hints（キーなし）→ guard_level="none"
     7.  guard_reasons の順序は blocking → warning

  B. planning_trace_json への追記
     8.  trace に "advisory_guard_assessment" ステージが含まれる
     9.  execution_guard_hints ステージより後に配置される
    10.  guard_level が正しく trace に入る
    11.  guard_reasons が正しく trace に入る

  C. 売買判断への影響なし
    12. planned_qty は advisory に関わらず変わらない
    13. planning_status は advisory に関わらず変わらない
    14. guard_level="blocking" でも SignalPlanRejectedError は送出されない

  D. 継続性
    15. advisory 生成が例外を起こしても _save_plan() が継続する
    16. guard_level="none" でもアセスメントステージは trace に存在する

  E. 定数 / 構造テスト
    17. _build_advisory_guard_assessment が module レベルに存在する
    18. 戻り値は dict 型
    19. 戻り値のキーは stage / guard_level / guard_reasons
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trade_app.services.planning import service as _svc_mod
from trade_app.services.planning.service import (
    _build_advisory_guard_assessment,
)
from trade_app.services.planning.context import PlannerContext

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


async def _run_save_plan(ctx: PlannerContext):
    """_save_plan を最小限 mock で呼び出して SignalPlan を返す"""
    from trade_app.services.planning.service import SignalPlanningService
    from trade_app.services.planning.reasons import PlanningStatus

    db_mock = AsyncMock()
    db_mock.flush = AsyncMock()
    db_mock.add = MagicMock()
    audit_mock = AsyncMock()
    service = SignalPlanningService(db=db_mock, audit=audit_mock)

    exec_params = MagicMock()
    exec_params.limit_price = ctx.signal.limit_price
    exec_params.stop_price = None
    exec_params.order_type_candidate = "market"
    exec_params.max_slippage_bps = 30.0
    exec_params.participation_rate_cap = None
    exec_params.entry_timeout_seconds = None

    return await service._save_plan(
        signal=ctx.signal,
        ctx=ctx,
        status=PlanningStatus.ACCEPTED,
        planned_qty=100,
        exec_params=exec_params,
        rejection_reason_code=None,
        trace=[{"stage": "base_size"}, {"stage": "execution_params"}],
        reasons=[],
        now=_NOW,
    )


async def _run_full_plan(ctx: PlannerContext):
    """SignalPlanningService.plan() を最小限 mock で呼び出して SignalPlan を返す"""
    from trade_app.services.planning.service import SignalPlanningService

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


def _get_advisory(plan) -> dict:
    return next(
        e for e in plan.planning_trace_json
        if e.get("stage") == "advisory_guard_assessment"
    )


# ─── A. _build_advisory_guard_assessment() 単体テスト ─────────────────────────

class TestBuildAdvisoryGuardAssessment:
    def test_empty_hints_none(self):
        a = _build_advisory_guard_assessment({})
        assert a["guard_level"] == "none"
        assert a["guard_reasons"] == []

    def test_has_quote_risk_false_none(self):
        a = _build_advisory_guard_assessment({"has_quote_risk": False, "blocking_reasons": [], "warning_reasons": []})
        assert a["guard_level"] == "none"

    def test_warning_only(self):
        a = _build_advisory_guard_assessment({"blocking_reasons": [], "warning_reasons": ["wide_spread"]})
        assert a["guard_level"] == "warning"
        assert a["guard_reasons"] == ["wide_spread"]

    def test_blocking_only(self):
        a = _build_advisory_guard_assessment({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        assert a["guard_level"] == "blocking"
        assert "price_stale" in a["guard_reasons"]

    def test_blocking_and_warning_blocking_wins(self):
        a = _build_advisory_guard_assessment({
            "blocking_reasons": ["stale_bid_ask"],
            "warning_reasons":  ["wide_spread"],
        })
        assert a["guard_level"] == "blocking"

    def test_blocking_and_warning_both_in_reasons(self):
        a = _build_advisory_guard_assessment({
            "blocking_reasons": ["price_stale"],
            "warning_reasons":  ["wide_spread"],
        })
        assert "price_stale" in a["guard_reasons"]
        assert "wide_spread" in a["guard_reasons"]

    def test_no_duplicates_in_reasons(self):
        a = _build_advisory_guard_assessment({
            "blocking_reasons": ["price_stale", "wide_spread"],
            "warning_reasons":  ["wide_spread"],
        })
        assert a["guard_reasons"].count("wide_spread") == 1

    def test_no_key_defaults_to_none(self):
        """blocking_reasons / warning_reasons キーがない → none"""
        a = _build_advisory_guard_assessment({"has_quote_risk": True})
        assert a["guard_level"] == "none"

    def test_blocking_before_warning_in_reasons(self):
        a = _build_advisory_guard_assessment({
            "blocking_reasons": ["price_stale"],
            "warning_reasons":  ["wide_spread"],
        })
        blocking_idx = a["guard_reasons"].index("price_stale")
        warning_idx  = a["guard_reasons"].index("wide_spread")
        assert blocking_idx < warning_idx


# ─── B. planning_trace_json への追記 ──────────────────────────────────────────

class TestAdvisoryInTrace:
    @pytest.mark.asyncio
    async def test_trace_has_advisory_stage(self):
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        plan = await _run_save_plan(ctx)
        stages = [e.get("stage") for e in plan.planning_trace_json]
        assert "advisory_guard_assessment" in stages

    @pytest.mark.asyncio
    async def test_advisory_after_guard_hints_in_trace(self):
        """advisory_guard_assessment は execution_guard_hints より後"""
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})
        plan = await _run_save_plan(ctx)
        stages = [e.get("stage") for e in plan.planning_trace_json]
        hints_idx    = stages.index("execution_guard_hints")
        advisory_idx = stages.index("advisory_guard_assessment")
        assert advisory_idx > hints_idx

    @pytest.mark.asyncio
    async def test_guard_level_in_trace_blocking(self):
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        plan = await _run_save_plan(ctx)
        advisory = _get_advisory(plan)
        assert advisory["guard_level"] == "blocking"

    @pytest.mark.asyncio
    async def test_guard_level_in_trace_warning(self):
        ctx = _make_context({"blocking_reasons": [], "warning_reasons": ["wide_spread"]})
        plan = await _run_save_plan(ctx)
        advisory = _get_advisory(plan)
        assert advisory["guard_level"] == "warning"

    @pytest.mark.asyncio
    async def test_guard_reasons_in_trace(self):
        ctx = _make_context({"blocking_reasons": ["quote_only"], "warning_reasons": ["wide_spread"]})
        plan = await _run_save_plan(ctx)
        advisory = _get_advisory(plan)
        assert "quote_only" in advisory["guard_reasons"]
        assert "wide_spread" in advisory["guard_reasons"]

    @pytest.mark.asyncio
    async def test_none_level_advisory_still_in_trace(self):
        """guard_level=none でも advisory ステージは trace に存在する"""
        ctx = _make_context({})
        plan = await _run_save_plan(ctx)
        stages = [e.get("stage") for e in plan.planning_trace_json]
        assert "advisory_guard_assessment" in stages

    @pytest.mark.asyncio
    async def test_none_level_guard_reasons_empty(self):
        ctx = _make_context({})
        plan = await _run_save_plan(ctx)
        advisory = _get_advisory(plan)
        assert advisory["guard_level"] == "none"
        assert advisory["guard_reasons"] == []


# ─── C. 売買判断への影響なし ──────────────────────────────────────────────────

class TestNoTradingImpact:
    @pytest.mark.asyncio
    async def test_planned_qty_unchanged_blocking(self):
        ctx = _make_context({
            "has_quote_risk": True,
            "blocking_reasons": ["stale_bid_ask", "quote_only"],
            "warning_reasons": ["wide_spread"],
        })
        plan = await _run_full_plan(ctx)
        assert plan.planned_order_qty == 100

    @pytest.mark.asyncio
    async def test_planning_status_unchanged_blocking(self):
        ctx = _make_context({
            "blocking_reasons": ["stale_bid_ask"],
            "warning_reasons": [],
        })
        plan = await _run_full_plan(ctx)
        assert plan.planning_status == "accepted"

    @pytest.mark.asyncio
    async def test_no_rejected_error_for_blocking(self):
        """price_stale 以外の guard_level=blocking では SignalPlanRejectedError は送出されない"""
        from trade_app.services.planning.service import SignalPlanRejectedError
        ctx = _make_context({"blocking_reasons": ["stale_bid_ask"], "warning_reasons": []})
        # 例外が出なければ OK
        plan = await _run_full_plan(ctx)
        assert plan is not None


# ─── D. 継続性 ────────────────────────────────────────────────────────────────

class TestAdvisoryContinuity:
    @pytest.mark.asyncio
    async def test_advisory_exception_does_not_stop_save_plan(self):
        """_build_advisory_guard_assessment が例外を起こしても _save_plan() が継続する"""
        ctx = _make_context({"blocking_reasons": ["price_stale"], "warning_reasons": []})

        with patch(
            "trade_app.services.planning.service._build_advisory_guard_assessment",
            side_effect=RuntimeError("assessment error"),
        ):
            plan = await _run_save_plan(ctx)

        # plan が生成されている（継続した証拠）
        assert plan is not None
        assert plan.planned_order_qty == 100

    @pytest.mark.asyncio
    async def test_execution_guard_hints_stage_still_present_after_advisory_error(self):
        """advisory が失敗しても execution_guard_hints stage は trace に残る"""
        ctx = _make_context({})

        with patch(
            "trade_app.services.planning.service._build_advisory_guard_assessment",
            side_effect=RuntimeError("assessment error"),
        ):
            plan = await _run_save_plan(ctx)

        stages = [e.get("stage") for e in plan.planning_trace_json]
        assert "execution_guard_hints" in stages


# ─── E. 定数 / 構造テスト ─────────────────────────────────────────────────────

class TestStructure:
    def test_function_exists_at_module_level(self):
        assert hasattr(_svc_mod, "_build_advisory_guard_assessment")
        assert callable(_svc_mod._build_advisory_guard_assessment)

    def test_return_type_is_dict(self):
        result = _build_advisory_guard_assessment({})
        assert isinstance(result, dict)

    def test_return_keys(self):
        result = _build_advisory_guard_assessment({})
        assert set(result.keys()) == {"stage", "guard_level", "guard_reasons"}

    def test_stage_value(self):
        result = _build_advisory_guard_assessment({})
        assert result["stage"] == "advisory_guard_assessment"
