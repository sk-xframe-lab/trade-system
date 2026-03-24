"""
Phase S — execution_guard_hints テスト

確認項目:
  A. _build_execution_guard_hints() 単体テスト
     1.  price_stale active → blocking_reasons に入る
     2.  stale_bid_ask active → blocking_reasons に入る
     3.  quote_only active → blocking_reasons に入る
     4.  wide_spread active → warning_reasons に入る
     5.  それ以外の state のみ → blocking/warning とも空
     6.  blocking と warning が同時にある場合、両方に正しく入る
     7.  全 blocking 3件 + warning 1件 → has_quote_risk=True
     8.  対象 state なし → has_quote_risk=False
     9.  並び順が安定していること（ソート済み）
    10.  blocking_reasons / warning_reasons は list 型
    11.  has_quote_risk は bool 型

  B. state_summary_json への統合テスト
    12. active state ありの snapshot に execution_guard_hints が含まれる
    13. active state なしの snapshot にも execution_guard_hints が含まれる
    14. price_stale active の snapshot → blocking_reasons に "price_stale"
    15. wide_spread active の snapshot → warning_reasons に "wide_spread"
    16. 既存 rule_diagnostics が壊れていない

  C. 定数構造テスト
    17. _GUARD_BLOCKING_STATES が module レベルに存在する
    18. _GUARD_WARNING_STATES が module レベルに存在する
    19. _GUARD_BLOCKING_STATES に 3 エントリ
    20. _GUARD_WARNING_STATES に 1 エントリ
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trade_app.services.market_state import engine as _engine_mod
from trade_app.services.market_state.engine import (
    MarketStateEngine,
    _build_execution_guard_hints,
)
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult

_UTC = timezone.utc
_EVAL_TIME = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _make_result(
    state_code: str,
    ticker: str = "7203",
    score: float = 1.0,
    evidence: dict[str, Any] | None = None,
    is_new: bool = True,
) -> StateEvaluationResult:
    return StateEvaluationResult(
        layer="symbol",
        target_type="symbol",
        target_code=ticker,
        state_code=state_code,
        score=score,
        confidence=1.0,
        evidence=evidence or {},
        is_new_activation=is_new,
    )


async def _run_engine_with_states(state_codes: list[str], ticker: str = "7203") -> dict:
    """
    指定した state_code が active な状態で engine を動かし、
    upsert_snapshot に渡された summary を返す。
    """
    db_mock = AsyncMock()
    db_mock.commit = AsyncMock()

    # snapshot なし（初回扱い）
    repo_mock = AsyncMock()
    repo_mock.get_symbol_snapshot.return_value = None
    repo_mock.save_evaluations_transitioned = AsyncMock()
    repo_mock.upsert_snapshot = AsyncMock()

    captured_summary: dict = {}

    async def _capture_upsert(**kwargs):
        if kwargs.get("layer") == "symbol":
            captured_summary.update(kwargs.get("summary", {}))

    repo_mock.upsert_snapshot.side_effect = _capture_upsert

    results = [_make_result(sc, ticker=ticker) for sc in state_codes]

    dummy_evaluator = MagicMock()
    dummy_evaluator.name = "dummy"
    dummy_evaluator.evaluate.return_value = results

    engine = MarketStateEngine(db=db_mock, evaluators=[dummy_evaluator])
    engine._repo = repo_mock

    ctx = EvaluationContext(
        evaluation_time=_EVAL_TIME,
        symbol_data={ticker: {"current_price": 1000.0}},
    )

    await engine.run(ctx)
    return captured_summary


# ─── A. _build_execution_guard_hints() 単体テスト ─────────────────────────────

class TestBuildExecutionGuardHints:
    def test_price_stale_goes_to_blocking(self):
        h = _build_execution_guard_hints(["price_stale"])
        assert "price_stale" in h["blocking_reasons"]

    def test_stale_bid_ask_goes_to_blocking(self):
        h = _build_execution_guard_hints(["stale_bid_ask"])
        assert "stale_bid_ask" in h["blocking_reasons"]

    def test_quote_only_goes_to_blocking(self):
        h = _build_execution_guard_hints(["quote_only"])
        assert "quote_only" in h["blocking_reasons"]

    def test_wide_spread_goes_to_warning(self):
        h = _build_execution_guard_hints(["wide_spread"])
        assert "wide_spread" in h["warning_reasons"]
        assert "wide_spread" not in h["blocking_reasons"]

    def test_other_states_only_empty(self):
        h = _build_execution_guard_hints(["gap_up_open", "symbol_range", "breakout_candidate"])
        assert h["blocking_reasons"] == []
        assert h["warning_reasons"] == []

    def test_blocking_and_warning_both_present(self):
        h = _build_execution_guard_hints(["price_stale", "wide_spread"])
        assert "price_stale" in h["blocking_reasons"]
        assert "wide_spread" in h["warning_reasons"]

    def test_all_blocking_and_warning(self):
        h = _build_execution_guard_hints(["price_stale", "stale_bid_ask", "quote_only", "wide_spread"])
        assert set(h["blocking_reasons"]) == {"price_stale", "stale_bid_ask", "quote_only"}
        assert h["warning_reasons"] == ["wide_spread"]
        assert h["has_quote_risk"] is True

    def test_no_target_states_has_quote_risk_false(self):
        h = _build_execution_guard_hints([])
        assert h["has_quote_risk"] is False

    def test_no_target_states_empty_lists(self):
        h = _build_execution_guard_hints(["gap_up_open"])
        assert h["has_quote_risk"] is False

    def test_stable_ordering_blocking(self):
        """blocking_reasons は毎回同じ順序（ソート済み）"""
        codes = ["stale_bid_ask", "price_stale", "quote_only"]
        h1 = _build_execution_guard_hints(codes)
        h2 = _build_execution_guard_hints(list(reversed(codes)))
        assert h1["blocking_reasons"] == h2["blocking_reasons"]
        assert h1["blocking_reasons"] == sorted(h1["blocking_reasons"])

    def test_blocking_reasons_is_list(self):
        h = _build_execution_guard_hints(["price_stale"])
        assert isinstance(h["blocking_reasons"], list)

    def test_warning_reasons_is_list(self):
        h = _build_execution_guard_hints(["wide_spread"])
        assert isinstance(h["warning_reasons"], list)

    def test_has_quote_risk_is_bool(self):
        h = _build_execution_guard_hints(["wide_spread"])
        assert isinstance(h["has_quote_risk"], bool)

    def test_has_quote_risk_true_when_blocking_only(self):
        h = _build_execution_guard_hints(["price_stale"])
        assert h["has_quote_risk"] is True

    def test_has_quote_risk_true_when_warning_only(self):
        h = _build_execution_guard_hints(["wide_spread"])
        assert h["has_quote_risk"] is True


# ─── B. state_summary_json への統合テスト ─────────────────────────────────────

class TestSummaryIntegration:
    @pytest.mark.asyncio
    async def test_execution_guard_hints_present_with_active_states(self):
        summary = await _run_engine_with_states(["wide_spread"])
        assert "execution_guard_hints" in summary

    @pytest.mark.asyncio
    async def test_execution_guard_hints_present_with_no_target_states(self):
        """blocking / warning 対象でない state のみでも execution_guard_hints は存在する"""
        summary = await _run_engine_with_states(["gap_up_open"])
        assert "execution_guard_hints" in summary

    @pytest.mark.asyncio
    async def test_price_stale_in_blocking(self):
        summary = await _run_engine_with_states(["price_stale"])
        hints = summary["execution_guard_hints"]
        assert "price_stale" in hints["blocking_reasons"]

    @pytest.mark.asyncio
    async def test_wide_spread_in_warning(self):
        summary = await _run_engine_with_states(["wide_spread"])
        hints = summary["execution_guard_hints"]
        assert "wide_spread" in hints["warning_reasons"]

    @pytest.mark.asyncio
    async def test_rule_diagnostics_not_broken(self):
        """既存の rule_diagnostics が execution_guard_hints 追加後も残っている"""
        summary = await _run_engine_with_states(["wide_spread"])
        # rule_diagnostics は空 dict でも存在すること（evaluator が返した diagnostics が注入される）
        assert "rule_diagnostics" in summary

    @pytest.mark.asyncio
    async def test_has_quote_risk_false_no_target_states(self):
        summary = await _run_engine_with_states(["breakout_candidate"])
        hints = summary["execution_guard_hints"]
        assert hints["has_quote_risk"] is False

    @pytest.mark.asyncio
    async def test_empty_active_states_has_guard_hints(self):
        """active state が0件のときも execution_guard_hints が存在する"""
        summary = await _run_engine_with_states([])
        assert "execution_guard_hints" in summary
        hints = summary["execution_guard_hints"]
        assert hints["has_quote_risk"] is False
        assert hints["blocking_reasons"] == []
        assert hints["warning_reasons"] == []


# ─── C. 定数構造テスト ────────────────────────────────────────────────────────

class TestGuardConstants:
    def test_guard_blocking_states_exists(self):
        assert hasattr(_engine_mod, "_GUARD_BLOCKING_STATES")

    def test_guard_warning_states_exists(self):
        assert hasattr(_engine_mod, "_GUARD_WARNING_STATES")

    def test_guard_blocking_has_3_entries(self):
        from trade_app.services.market_state.engine import _GUARD_BLOCKING_STATES
        assert len(_GUARD_BLOCKING_STATES) == 3

    def test_guard_blocking_contains_expected(self):
        from trade_app.services.market_state.engine import _GUARD_BLOCKING_STATES
        assert _GUARD_BLOCKING_STATES == {"price_stale", "stale_bid_ask", "quote_only"}

    def test_guard_warning_has_1_entry(self):
        from trade_app.services.market_state.engine import _GUARD_WARNING_STATES
        assert len(_GUARD_WARNING_STATES) == 1

    def test_guard_warning_contains_wide_spread(self):
        from trade_app.services.market_state.engine import _GUARD_WARNING_STATES
        assert "wide_spread" in _GUARD_WARNING_STATES
