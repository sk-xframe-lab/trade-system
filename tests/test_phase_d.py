"""
Phase D — rule ベース構造リファクタリングテスト

確認項目:
  1. _rule_wide_spread() の直接呼び出し単体テスト
     - ガード（invalid_current_price / no_bid / no_ask / inverted_spread）
     - 非発火（spread_rate < threshold）
     - 発火（spread_rate >= threshold）
     - evidence フィールドの内容

  2. orchestrator 経由の結合確認
     - rule リスト経由でも従来と同じ結果が得られる
     - wide_spread と他状態が同時に評価される

  3. 構造確認
     - _evaluate_symbol() に spread 固有判定が残っていないこと
     - _rule_wide_spread() が module レベルに存在すること

設計:
  - _rule_wide_spread() は SymbolStateEvaluator._make() と同等の make ヘルパーを渡す
  - 直接呼び出し時は make をテスト用ファクトリで代用する
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from trade_app.services.market_state import symbol_evaluator as _mod
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult
from trade_app.services.market_state.symbol_evaluator import SymbolStateEvaluator

_UTC = timezone.utc


# ─── テスト用 make ヘルパー ────────────────────────────────────────────────────

def _make(
    ticker: str,
    state_code: str,
    score: float,
    confidence: float,
    evidence: dict[str, Any],
) -> StateEvaluationResult:
    """_rule_wide_spread() の直接呼び出し用 make ファクトリ"""
    return StateEvaluationResult(
        layer="symbol",
        target_type="symbol",
        target_code=ticker,
        state_code=state_code,
        score=max(0.0, min(1.0, score)),
        confidence=max(0.0, min(1.0, confidence)),
        evidence=evidence,
    )


def _ctx(**fields) -> EvaluationContext:
    """ticker="7203" の symbol_data を持つ EvaluationContext を生成する。"""
    return EvaluationContext(
        evaluation_time=datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC),
        symbol_data={"7203": fields},
    )


THRESHOLD = SymbolStateEvaluator.SPREAD_THRESHOLD  # 0.003


def _call_rule(data: dict[str, Any]) -> StateEvaluationResult | None:
    """_rule_wide_spread() を呼び出し、result 部分のみを返すヘルパー。
    Phase G で戻り値が tuple になったため、テスト側でのアンパックを集約する。"""
    result, _diag = _mod._rule_wide_spread(
        "7203", data, spread_threshold=THRESHOLD, make=_make
    )
    return result


# ─── 1. _rule_wide_spread() 単体テスト ───────────────────────────────────────

class TestRuleWidespreadDirect:
    """_rule_wide_spread() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_returns_none_when_current_price_none(self):
        """current_price=None → None"""
        result = _call_rule({"current_price": None, "best_bid": 990.0, "best_ask": 1010.0})
        assert result is None

    def test_returns_none_when_current_price_zero(self):
        """current_price=0 → None（ゼロ除算ガード）"""
        result = _call_rule({"current_price": 0, "best_bid": 990.0, "best_ask": 1010.0})
        assert result is None

    def test_returns_none_when_current_price_negative(self):
        """current_price=-1 → None（無効値ガード）"""
        result = _call_rule({"current_price": -1.0, "best_bid": 990.0, "best_ask": 1010.0})
        assert result is None

    def test_returns_none_when_bid_none(self):
        """best_bid=None → None"""
        result = _call_rule({"current_price": 1000.0, "best_bid": None, "best_ask": 1010.0})
        assert result is None

    def test_returns_none_when_bid_zero(self):
        """best_bid=0 → None（<= 0 は無効）"""
        result = _call_rule({"current_price": 1000.0, "best_bid": 0, "best_ask": 1010.0})
        assert result is None

    def test_returns_none_when_ask_none(self):
        """best_ask=None → None"""
        result = _call_rule({"current_price": 1000.0, "best_bid": 990.0, "best_ask": None})
        assert result is None

    def test_returns_none_when_inverted_spread(self):
        """best_ask < best_bid → None（逆転スプレッド）"""
        result = _call_rule({"current_price": 1000.0, "best_bid": 1010.0, "best_ask": 990.0})
        assert result is None

    def test_inverted_spread_emits_warning(self, caplog):
        """逆転スプレッドで WARNING ログが出力される"""
        with caplog.at_level(logging.WARNING):
            _call_rule({"current_price": 1000.0, "best_bid": 1010.0, "best_ask": 990.0})
        assert any("inverted spread" in r.message for r in caplog.records)

    # ── 非発火 ──

    def test_returns_none_when_spread_below_threshold(self):
        """spread_rate < threshold → None（非発火）"""
        # spread=2, current_price=1000 → spread_rate=0.002 < 0.003
        result = _call_rule({"current_price": 1000.0, "best_bid": 999.0, "best_ask": 1001.0})
        assert result is None

    def test_returns_none_with_current_price_denominator(self):
        """
        mid_price 分母なら発火するが current_price 分母では非発火のケース。

        bid=997, ask=1003, current_price=3000:
          spread_rate (correct)  = 6 / 3000 = 0.002 < 0.003 → 非発火
          spread_rate (wrong)    = 6 / 1000 = 0.006 >= 0.003 → 誤発火
        """
        result = _call_rule({"current_price": 3000.0, "best_bid": 997.0, "best_ask": 1003.0})
        assert result is None, "current_price 分母では非発火であるべき"

    # ── 発火 ──

    def test_returns_result_when_spread_at_threshold(self):
        """spread_rate = threshold ちょうど → 発火（>=）"""
        # spread=3, current_price=1000 → spread_rate=0.003 = threshold
        result = _call_rule({"current_price": 1000.0, "best_bid": 998.5, "best_ask": 1001.5})
        assert result is not None
        assert result.state_code == "wide_spread"

    def test_returns_result_when_spread_above_threshold(self):
        """spread_rate > threshold → 発火"""
        result = _call_rule({"current_price": 3000.0, "best_bid": 2994.0, "best_ask": 3006.0})
        assert result is not None
        assert result.state_code == "wide_spread"
        assert result.target_code == "7203"
        assert result.layer == "symbol"

    # ── evidence ──

    def test_evidence_contains_required_fields(self):
        """発火時 evidence に reason / current_price / spread / spread_rate が含まれる"""
        result = _call_rule({"current_price": 3000.0, "best_bid": 2994.0, "best_ask": 3006.0})
        assert result is not None
        ev = result.evidence
        assert ev["reason"] == "wide_spread"
        assert ev["current_price"] == 3000.0
        assert ev["best_bid"] == 2994.0
        assert ev["best_ask"] == 3006.0
        assert ev["spread"] == pytest.approx(12.0, abs=0.001)

    def test_evidence_spread_rate_uses_current_price_denominator(self):
        """
        evidence[\"spread_rate\"] = (ask - bid) / current_price

        bid=994, ask=1006, current_price=2000:
          spread = 12
          spread_rate = 12 / 2000 = 0.006
        """
        result = _call_rule({"current_price": 2000.0, "best_bid": 994.0, "best_ask": 1006.0})
        assert result is not None
        expected = 12.0 / 2000.0
        assert result.evidence["spread_rate"] == pytest.approx(expected, abs=1e-5)

    def test_score_scales_with_spread_rate(self):
        """score = min(1.0, spread_rate / 0.01)"""
        # spread=10, current_price=1000 → spread_rate=0.01 → score=1.0
        result = _call_rule({"current_price": 1000.0, "best_bid": 995.0, "best_ask": 1005.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)


# ─── 2. orchestrator 経由の結合確認 ──────────────────────────────────────────

class TestOrchestratorIntegration:
    """_evaluate_symbol() 経由でも同じ結果が得られることを確認する"""

    def test_wide_spread_via_rule_list(self):
        """
        SymbolStateEvaluator.evaluate() が rule リスト経由で wide_spread を検出する。
        spread_rate = 12/3000 = 0.4% >= 0.3% → 発火
        """
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=3000.0, best_bid=2994.0, best_ask=3006.0)
        results = evaluator.evaluate(ctx)
        assert any(r.state_code == "wide_spread" for r in results)

    def test_normal_spread_not_fired_via_rule_list(self):
        """spread_rate < threshold → wide_spread が発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=1000.0, best_bid=999.0, best_ask=1001.0)
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "wide_spread" for r in results)

    def test_wide_spread_evidence_via_rule_list(self):
        """orchestrator 経由でも evidence フィールドが正しい"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=3000.0, best_bid=2950.0, best_ask=3050.0)
        results = evaluator.evaluate(ctx)
        r = next(r for r in results if r.state_code == "wide_spread")
        assert r.evidence["reason"] == "wide_spread"
        assert r.evidence["current_price"] == 3000.0
        expected_rate = (3050.0 - 2950.0) / 3000.0
        assert r.evidence["spread_rate"] == pytest.approx(expected_rate, abs=1e-5)

    def test_wide_spread_and_gap_up_coexist(self):
        """wide_spread と gap_up_open が同時に発火する"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=3000.0,
            current_open=3060.0, prev_close=3000.0,   # gap_up_open
            best_bid=2994.0, best_ask=3006.0,          # wide_spread
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "wide_spread" in codes
        assert "gap_up_open" in codes

    def test_guard_no_bid_via_rule_list(self):
        """best_bid=None → rule リスト経由でも wide_spread が発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=1000.0, best_bid=None, best_ask=1010.0)
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "wide_spread" for r in results)


# ─── 3. 構造確認 ──────────────────────────────────────────────────────────────

class TestStructure:
    """リファクタリング後の構造を確認する"""

    def test_rule_wide_spread_is_module_level_function(self):
        """_rule_wide_spread が module レベルに存在する"""
        assert hasattr(_mod, "_rule_wide_spread"), "_rule_wide_spread が module に存在しない"
        assert callable(_mod._rule_wide_spread)

    def test_evaluate_symbol_has_no_spread_inline(self):
        """
        _evaluate_symbol() のソースコードに spread 固有の変数名が残っていないこと。

        wide_spread ロジックが rule 関数に移っていれば、
        _evaluate_symbol() 内に "best_bid" / "best_ask" の直接代入がないはず。
        """
        import inspect
        mod_source = inspect.getsource(_mod)
        ev_source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        # _rule_wide_spread が module (_RULES) に登録されている
        assert "spread_threshold" in mod_source, "_rule_wide_spread の呼び出しが見当たらない"
        # _evaluate_symbol() 内で best_bid / best_ask を data.get() していないこと
        assert 'data.get("best_bid")' not in ev_source, "_evaluate_symbol() 内に best_bid の直接抽出が残っている"
        assert 'data.get("best_ask")' not in ev_source, "_evaluate_symbol() 内に best_ask の直接抽出が残っている"
