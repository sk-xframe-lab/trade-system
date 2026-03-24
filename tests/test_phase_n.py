"""
Phase N — _RULES single source 化・二重管理解消の構造テスト

確認項目:
  A. _RULES 構造テスト
     1.  _RULES が module レベルに存在する
     2.  _RULES が list 型である
     3.  _RULES に 13 エントリが含まれる（Phase P で quote_only 追加済み）
     4.  各エントリが (str, callable) のペアである
     5.  _RULES の state_code 一覧が _RULE_REGISTRY と一致する

  B. _RULE_REGISTRY の自動導出テスト
     6.  _RULE_REGISTRY は _RULES から導出されている（手動定義ではない）
     7.  `tuple(code for code, _ in _RULES) == _RULE_REGISTRY`
     8.  _RULES と _RULE_REGISTRY の順序が一致する

  C. _evaluate_symbol() の単純化テスト
     9.  _evaluate_symbol() ソースにローカル _rules 変数がない
    10.  _evaluate_symbol() ソースが _RULES を参照している
    11.  _evaluate_symbol() ソースに data.get( がない（全 rule 化の証明）
    12.  全 state code が rule_diagnostics に存在する

  D. 挙動不変テスト（deps フラグ伝播）
    13. gap_up_open → breakout_candidate を抑制する
    14. is_trend_up → symbol_range を inactive にする
    15. is_high_volume + no_gap + price > MA20 → breakout_candidate が発火する
    16. price_stale が evaluation_time を使って判定する

  E. observability 不変テスト
    17. wide_spread active: status=active, spread_ratio が含まれる
    18. symbol_range high_atr inactive: reason="high_atr"
    19. symbol_range trending inactive: reason="trending"
    20. 全 rule が status キーを返す
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any

from trade_app.services.market_state import symbol_evaluator as _mod
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult
from trade_app.services.market_state.symbol_evaluator import (
    SymbolStateEvaluator,
    _RULE_DEP_FLAGS,
    _RULE_REGISTRY,
    _RULES,
)

_UTC = timezone.utc
_EVAL_TIME = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

ATR_RATIO_HIGH = SymbolStateEvaluator.ATR_RATIO_HIGH  # 0.02


# ─── テスト用ヘルパー ─────────────────────────────────────────────────────────

def _diags(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evaluator = SymbolStateEvaluator()
    _results, rule_diagnostics = evaluator._evaluate_symbol("TEST", data, _EVAL_TIME)
    return rule_diagnostics


def _active_states(data: dict[str, Any]) -> list[str]:
    evaluator = SymbolStateEvaluator()
    results, _diags = evaluator._evaluate_symbol("TEST", data, _EVAL_TIME)
    return [r.state_code for r in results]


# ─── テスト用データセット ──────────────────────────────────────────────────────

_GAP_UP_WITH_VOL = {
    "current_price": 1020.0,
    "current_open": 1020.0,
    "prev_close": 1000.0,       # +2% → gap_up_open 発火
    "ma20": 980.0,
    "current_volume": 300.0,
    "avg_volume_same_time": 100.0,
    "best_bid": 1019.0,
    "best_ask": 1021.0,
}

_TREND_UP = {
    "current_price": 1100.0,
    "vwap": 900.0,
    "ma5": 1050.0,
    "ma20": 980.0,
    "atr": 15.0,
    "best_bid": 1099.0,
    "best_ask": 1101.0,
}

_BREAKOUT = {
    "current_price": 1050.0,
    "current_open": 1005.0,
    "prev_close": 1000.0,       # +0.5% → gap なし
    "ma20": 1000.0,
    "current_volume": 300.0,
    "avg_volume_same_time": 100.0,
    "best_bid": 1049.0,
    "best_ask": 1051.0,
}

_WIDE_SPREAD = {
    "current_price": 1000.0,
    "best_bid":  994.0,
    "best_ask": 1006.0,         # spread=12 / 1000 = 1.2% >= 0.3%
}

_RANGE = {
    "current_price": 1000.0,
    "vwap": 1000.0,
    "ma5": 1000.0,
    "ma20": 1000.0,
    "atr": 5.0,                 # atr_ratio=0.005 < 0.02
    "best_bid": 999.0,
    "best_ask": 1001.0,
}

# 高 ATR（vwap/ma5/ma20 省略 → trend rules が skip → is_trend_up/down=False）
_HIGH_ATR_ONLY = {
    "current_price": 1000.0,
    "atr": 30.0,
}


# ─── A. _RULES 構造テスト ──────────────────────────────────────────────────────

class TestRulesList:
    def test_exists_at_module_level(self):
        assert hasattr(_mod, "_RULES"), "_RULES が module にない"

    def test_is_list(self):
        assert isinstance(_RULES, list)

    def test_has_14_entries(self):
        assert len(_RULES) == 14, f"expected 14, got {len(_RULES)}"

    def test_each_entry_is_str_callable_pair(self):
        for i, entry in enumerate(_RULES):
            assert isinstance(entry, tuple) and len(entry) == 2, \
                f"_RULES[{i}] は (str, callable) のペアでない"
            state_code, caller = entry
            assert isinstance(state_code, str), f"_RULES[{i}][0] が str でない"
            assert callable(caller), f"_RULES[{i}][1] が callable でない"

    def test_state_codes_match_rule_registry(self):
        codes_from_rules = [code for code, _ in _RULES]
        assert codes_from_rules == list(_RULE_REGISTRY)


# ─── B. _RULE_REGISTRY 自動導出テスト ────────────────────────────────────────

class TestRuleRegistryDerived:
    def test_registry_equals_derived(self):
        """_RULE_REGISTRY == tuple(code for code, _ in _RULES)"""
        derived = tuple(code for code, _ in _RULES)
        assert _RULE_REGISTRY == derived, \
            f"_RULE_REGISTRY が _RULES から正しく導出されていない:\n{_RULE_REGISTRY}\n{derived}"

    def test_order_matches(self):
        """_RULES の順序と _RULE_REGISTRY の順序が一致する"""
        for i, (code, _) in enumerate(_RULES):
            assert code == _RULE_REGISTRY[i], \
                f"位置 {i}: _RULES={code} / _RULE_REGISTRY={_RULE_REGISTRY[i]}"

    def test_dep_providers_before_consumers(self):
        """依存フラグ提供 rule は消費 rule より前にある（_RULES の順序）"""
        codes = [c for c, _ in _RULES]
        for provider in _RULE_DEP_FLAGS:
            assert provider in codes, f"{provider} が _RULES にない"
        assert codes.index("gap_up_open") < codes.index("breakout_candidate")
        assert codes.index("gap_down_open") < codes.index("breakout_candidate")
        assert codes.index("high_relative_volume") < codes.index("breakout_candidate")
        assert codes.index("symbol_trend_up") < codes.index("symbol_range")
        assert codes.index("symbol_trend_down") < codes.index("symbol_range")


# ─── C. _evaluate_symbol() 単純化テスト ──────────────────────────────────────

class TestEvaluateSymbolStructure:
    def test_no_local_rules_variable(self):
        """_evaluate_symbol() ソースにローカル _rules 変数がない（module レベル _RULES を使う）"""
        src = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        # ローカル _rules = [...] のようなアサインがないことを確認
        assert "_rules = [" not in src, \
            "_evaluate_symbol() にローカル _rules リストが残っている"

    def test_references_module_level_rules(self):
        """_evaluate_symbol() ソースが _RULES を参照している"""
        src = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert "_RULES" in src, "_evaluate_symbol() が _RULES を参照していない"

    def test_no_data_get_in_evaluate_symbol(self):
        """_evaluate_symbol() 内に data.get( がない"""
        src = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert "data.get(" not in src

    def test_all_state_codes_in_diagnostics(self):
        diags = _diags({})
        missing = set(_RULE_REGISTRY) - set(diags.keys())
        assert not missing, f"rule_diagnostics に不足: {missing}"


# ─── D. 挙動不変テスト ───────────────────────────────────────────────────────

class TestBehaviorUnchanged:
    def test_gap_up_suppresses_breakout(self):
        """gap_up_open active → breakout_candidate は発火しない"""
        active = _active_states(_GAP_UP_WITH_VOL)
        assert "gap_up_open" in active
        assert "breakout_candidate" not in active

    def test_trend_up_suppresses_symbol_range(self):
        """is_trend_up=True → symbol_range が inactive"""
        diags = _diags(_TREND_UP)
        assert diags["symbol_trend_up"]["status"] == "active"
        assert diags["symbol_range"]["status"] == "inactive"
        assert diags["symbol_range"].get("reason") == "trending"

    def test_high_volume_enables_breakout(self):
        """is_high_volume=True + ギャップなし + price > MA20 → breakout_candidate 発火"""
        active = _active_states(_BREAKOUT)
        assert "high_relative_volume" in active
        assert "breakout_candidate" in active

    def test_price_stale_uses_evaluation_time(self):
        """price_stale は evaluation_time と last_updated の差で判定する"""
        stale_time = _EVAL_TIME - timedelta(seconds=120)  # 2分前 → stale
        data = {"current_price": 1000.0, "last_updated": stale_time}
        active = _active_states(data)
        assert "price_stale" in active

        fresh_time = _EVAL_TIME - timedelta(seconds=10)   # 10秒前 → fresh
        data_fresh = {"current_price": 1000.0, "last_updated": fresh_time}
        active_fresh = _active_states(data_fresh)
        assert "price_stale" not in active_fresh


# ─── E. observability 不変テスト ─────────────────────────────────────────────

class TestObservabilityUnchanged:
    def test_wide_spread_active_diag(self):
        diags = _diags(_WIDE_SPREAD)
        d = diags["wide_spread"]
        assert d["status"] == "active"
        assert "spread_rate" in d

    def test_symbol_range_high_atr_reason(self):
        diags = _diags(_HIGH_ATR_ONLY)
        d = diags["symbol_range"]
        assert d["status"] == "inactive"
        assert d.get("reason") == "high_atr"

    def test_symbol_range_trending_reason(self):
        diags = _diags(_TREND_UP)
        d = diags["symbol_range"]
        assert d["status"] == "inactive"
        assert d.get("reason") == "trending"

    def test_all_rules_have_status_key(self):
        """全 rule の診断 dict に status キーが存在する"""
        for data in [{}, _WIDE_SPREAD, _BREAKOUT]:
            diags = _diags(data)
            for state_code in _RULE_REGISTRY:
                assert "status" in diags[state_code], \
                    f"{state_code} の診断に status キーがない (data={data})"
