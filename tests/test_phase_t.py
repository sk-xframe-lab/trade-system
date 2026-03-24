"""
Phase T — execution_guard_hints を signal payload（planning_trace_json）に搭載するテスト

確認項目:
  A. PlannerContext.execution_guard_hints フィールド
     1.  PlannerContext に execution_guard_hints フィールドがある
     2.  デフォルトは空 dict
     3.  任意の dict を設定できる

  B. PlannerContextBuilder: スナップショットから取得
     4.  スナップショットに execution_guard_hints がある → ctx に載る
     5.  スナップショットが存在しない → ctx は空 dict
     6.  スナップショットに execution_guard_hints キーがない → ctx は空 dict
     7.  DB クエリ例外 → ctx は空 dict（安全デフォルト）

  C. SignalPlan.planning_trace_json への搭載
     8.  planning_trace_json に "execution_guard_hints" ステージが含まれる
     9.  hints の内容が正しく搭載される（has_quote_risk / blocking / warning）
    10.  hints が空 dict でも trace エントリは存在する
    11.  exit signal（bypass）でも trace に execution_guard_hints が入る

  D. 既存 payload 項目の不変確認
    12. planning_trace_json の既存 stage（base_size 等）が残っている
    13. planned_order_qty / planning_status は変わらない
    14. 既存の PlannerContext フィールド（size_ratio / is_market_tradable 等）が残っている

  E. execution 側ロジックへの影響なし
    15. RiskManager は execution_guard_hints を参照しない（型のみ確認）
    16. OrderRouter は execution_guard_hints を参照しない（型のみ確認）
"""
from __future__ import annotations

import uuid
from dataclasses import field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trade_app.services.planning.context import PlannerContext, PlannerContextBuilder

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

# ─── テスト用ヘルパー ─────────────────────────────────────────────────────────

def _make_signal(
    ticker: str = "7203",
    signal_type: str = "entry",
    quantity: int = 100,
    limit_price: float | None = 3000.0,
) -> MagicMock:
    s = MagicMock()
    s.id = str(uuid.uuid4())
    s.ticker = ticker
    s.signal_type = signal_type
    s.quantity = quantity
    s.limit_price = limit_price
    s.side = "buy"
    return s


def _make_context(
    signal=None,
    execution_guard_hints: dict | None = None,
) -> PlannerContext:
    return PlannerContext(
        signal=signal or _make_signal(),
        size_ratio=1.0,
        signal_strategy_decision_id=str(uuid.uuid4()),
        decision_evaluation_time=_NOW,
        execution_guard_hints=execution_guard_hints if execution_guard_hints is not None else {},
    )


def _make_snapshot(guard_hints: dict | None = None) -> MagicMock:
    snap = MagicMock()
    summary: dict = {}
    if guard_hints is not None:
        summary["execution_guard_hints"] = guard_hints
    snap.state_summary_json = summary
    return snap


async def _build_context_with_snapshot(
    guard_hints: dict | None,
    ticker: str = "7203",
) -> PlannerContext:
    """PlannerContextBuilder.build() をモックで呼び出して PlannerContext を返す"""
    signal = _make_signal(ticker=ticker)

    db_mock = AsyncMock()

    # SignalStrategyDecision クエリ → None（decision 不要）
    # CurrentStateSnapshot クエリ → スナップショットを返す or None
    snap = _make_snapshot(guard_hints) if guard_hints is not None else None

    call_count = 0

    async def _execute(query):
        nonlocal call_count
        call_count += 1
        mock_result = MagicMock()
        if call_count == 1:
            # 1回目: SignalStrategyDecision
            mock_result.scalar_one_or_none.return_value = None
        else:
            # 2回目: CurrentStateSnapshot
            mock_result.scalar_one_or_none.return_value = snap
        return mock_result

    db_mock.execute.side_effect = _execute

    builder = PlannerContextBuilder(db=db_mock)
    return await builder.build(signal=signal)


# ─── A. PlannerContext.execution_guard_hints フィールド ───────────────────────

class TestPlannerContextField:
    def test_field_exists(self):
        ctx = _make_context()
        assert hasattr(ctx, "execution_guard_hints")

    def test_default_is_empty_dict(self):
        ctx = PlannerContext(
            signal=_make_signal(),
            size_ratio=1.0,
            signal_strategy_decision_id=None,
            decision_evaluation_time=None,
        )
        assert ctx.execution_guard_hints == {}

    def test_can_set_dict(self):
        hints = {"has_quote_risk": True, "blocking_reasons": ["price_stale"], "warning_reasons": []}
        ctx = _make_context(execution_guard_hints=hints)
        assert ctx.execution_guard_hints["has_quote_risk"] is True
        assert ctx.execution_guard_hints["blocking_reasons"] == ["price_stale"]


# ─── B. PlannerContextBuilder: スナップショットから取得 ──────────────────────

class TestContextBuilderGuardHints:
    @pytest.mark.asyncio
    async def test_hints_loaded_from_snapshot(self):
        hints = {
            "has_quote_risk": True,
            "blocking_reasons": ["price_stale"],
            "warning_reasons": [],
        }
        ctx = await _build_context_with_snapshot(guard_hints=hints)
        assert ctx.execution_guard_hints == hints

    @pytest.mark.asyncio
    async def test_no_snapshot_returns_empty_dict(self):
        """スナップショットが存在しない場合 → 空 dict"""
        ctx = await _build_context_with_snapshot(guard_hints=None)
        assert ctx.execution_guard_hints == {}

    @pytest.mark.asyncio
    async def test_snapshot_without_hints_key_returns_empty_dict(self):
        """スナップショットに execution_guard_hints キーがない場合 → 空 dict"""
        # _make_snapshot(None) は state_summary_json = {} を返す（キーなし）
        ctx = await _build_context_with_snapshot(guard_hints=None)
        assert ctx.execution_guard_hints == {}

    @pytest.mark.asyncio
    async def test_db_exception_returns_empty_dict(self):
        """DB クエリ例外 → 空 dict（安全デフォルト）"""
        signal = _make_signal()
        db_mock = AsyncMock()

        call_count = 0

        async def _execute(query):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                mock_result = MagicMock()
                mock_result.scalar_one_or_none.return_value = None
                return mock_result
            raise RuntimeError("DB error")

        db_mock.execute.side_effect = _execute

        builder = PlannerContextBuilder(db=db_mock)
        ctx = await builder.build(signal=signal)
        assert ctx.execution_guard_hints == {}


# ─── C. SignalPlan.planning_trace_json への搭載 ───────────────────────────────

class TestPlanningTraceGuardHints:
    def _make_full_context(self, hints: dict) -> PlannerContext:
        """SignalPlanningService で使える完全な PlannerContext を作る"""
        signal = _make_signal()
        return PlannerContext(
            signal=signal,
            size_ratio=1.0,
            signal_strategy_decision_id=str(uuid.uuid4()),
            decision_evaluation_time=_NOW,
            is_market_tradable=True,
            is_symbol_tradable=True,
            execution_guard_hints=hints,
        )

    async def _run_planning(self, ctx: PlannerContext):
        """SignalPlanningService.plan() を最小限の mock で実行して SignalPlan を返す"""
        from trade_app.services.planning.service import SignalPlanningService

        db_mock = AsyncMock()
        db_mock.flush = AsyncMock()
        db_mock.add = MagicMock()
        audit_mock = AsyncMock()

        service = SignalPlanningService(db=db_mock, audit=audit_mock)

        # validate_decision → None（reject しない）
        service._validate_decision = MagicMock(return_value=None)

        # sizer
        sizer_result = MagicMock()
        sizer_result.base_qty = 100
        sizer_result.applied_size_ratio = 1.0
        sizer_result.after_ratio_qty = 100
        service._sizer.calculate = MagicMock(return_value=sizer_result)
        service._sizer.round_to_lot = MagicMock(return_value=100)

        # adjusters: 全て pass through
        for adj_name in ("_tradability", "_liquidity", "_spread", "_volatility"):
            adj_result = MagicMock()
            adj_result.as_trace_entry.return_value = {"stage": adj_name}
            adj_result.rejected = False
            adj_result.was_reduced = False
            adj_result.output_qty = 100
            getattr(service, adj_name).check = MagicMock(return_value=adj_result)
            getattr(service, adj_name).adjust = MagicMock(return_value=adj_result)

        # exec_params
        exec_params = MagicMock()
        exec_params.as_dict.return_value = {}
        exec_params.order_type_candidate = "market"
        exec_params.limit_price = None
        exec_params.stop_price = None
        exec_params.max_slippage_bps = 30.0
        exec_params.participation_rate_cap = None
        exec_params.entry_timeout_seconds = None
        service._params_builder.build = MagicMock(return_value=exec_params)

        plan = await service.plan(ctx.signal, ctx)
        return plan

    @pytest.mark.asyncio
    async def test_trace_has_guard_hints_stage(self):
        hints = {"has_quote_risk": True, "blocking_reasons": ["stale_bid_ask"], "warning_reasons": []}
        ctx = self._make_full_context(hints)
        plan = await self._run_planning(ctx)

        stages = [entry.get("stage") for entry in plan.planning_trace_json]
        assert "execution_guard_hints" in stages

    @pytest.mark.asyncio
    async def test_hints_content_correct(self):
        hints = {
            "has_quote_risk": True,
            "blocking_reasons": ["stale_bid_ask"],
            "warning_reasons": ["wide_spread"],
        }
        ctx = self._make_full_context(hints)
        plan = await self._run_planning(ctx)

        guard_entry = next(
            e for e in plan.planning_trace_json if e.get("stage") == "execution_guard_hints"
        )
        assert guard_entry["hints"]["has_quote_risk"] is True
        assert "stale_bid_ask" in guard_entry["hints"]["blocking_reasons"]
        assert "wide_spread" in guard_entry["hints"]["warning_reasons"]

    @pytest.mark.asyncio
    async def test_empty_hints_trace_entry_still_present(self):
        """hints が空 dict でも execution_guard_hints ステージが trace に存在する"""
        ctx = self._make_full_context({})
        plan = await self._run_planning(ctx)

        stages = [entry.get("stage") for entry in plan.planning_trace_json]
        assert "execution_guard_hints" in stages

    @pytest.mark.asyncio
    async def test_exit_signal_bypass_has_guard_hints(self):
        """exit signal（bypass）でも execution_guard_hints が trace に入る"""
        hints = {"has_quote_risk": False, "blocking_reasons": [], "warning_reasons": []}
        signal = _make_signal(signal_type="exit")
        ctx = PlannerContext(
            signal=signal,
            size_ratio=1.0,
            signal_strategy_decision_id=None,
            decision_evaluation_time=None,
            execution_guard_hints=hints,
        )
        plan = await self._run_planning(ctx)

        stages = [entry.get("stage") for entry in plan.planning_trace_json]
        assert "execution_guard_hints" in stages


# ─── D. 既存 payload 項目の不変確認 ──────────────────────────────────────────

class TestExistingPayloadUnchanged:
    @pytest.mark.asyncio
    async def test_existing_trace_stages_preserved(self):
        """planning_trace_json の既存 stage が execution_guard_hints 追加後も残る"""
        ctx = TestPlanningTraceGuardHints()._make_full_context({})
        plan = await TestPlanningTraceGuardHints()._run_planning(ctx)

        stages = [entry.get("stage") for entry in plan.planning_trace_json]
        assert "base_size" in stages
        assert "execution_params" in stages

    @pytest.mark.asyncio
    async def test_planned_qty_unchanged(self):
        ctx = TestPlanningTraceGuardHints()._make_full_context({})
        plan = await TestPlanningTraceGuardHints()._run_planning(ctx)
        assert plan.planned_order_qty == 100

    @pytest.mark.asyncio
    async def test_planning_status_unchanged(self):
        ctx = TestPlanningTraceGuardHints()._make_full_context({})
        plan = await TestPlanningTraceGuardHints()._run_planning(ctx)
        assert plan.planning_status == "accepted"

    def test_existing_context_fields_present(self):
        """既存の PlannerContext フィールドが execution_guard_hints 追加後も残る"""
        ctx = _make_context()
        assert ctx.size_ratio == 1.0
        assert ctx.is_market_tradable is True
        assert ctx.is_symbol_tradable is True
        assert ctx.symbol_lot_size == 100
        assert ctx.spread_bps == 0.0
        assert ctx.volume_ratio == 1.0


# ─── E. execution 側ロジックへの影響なし ──────────────────────────────────────

class TestNoExecutionSideEffect:
    def test_risk_manager_does_not_import_guard_hints(self):
        """RiskManager は execution_guard_hints を参照しない"""
        import inspect
        from trade_app.services.risk_manager import RiskManager
        src = inspect.getsource(RiskManager)
        assert "execution_guard_hints" not in src

    def test_order_router_does_not_import_guard_hints(self):
        """OrderRouter は execution_guard_hints を参照しない"""
        import inspect
        from trade_app.services.order_router import OrderRouter
        src = inspect.getsource(OrderRouter)
        assert "execution_guard_hints" not in src
