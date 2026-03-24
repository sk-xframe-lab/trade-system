"""
Phase M — rule registry 明文化・_evaluate_symbol() single loop 構造テスト

確認項目:
  A. _RULE_REGISTRY 構造テスト
     1.  _RULE_REGISTRY が module レベルに存在する
     2.  _RULE_REGISTRY が tuple[str, ...] 型である
     3.  _RULE_REGISTRY に 12 エントリが含まれる
     4.  _RULE_REGISTRY の全エントリが一意である
     5.  _RULE_REGISTRY に全 state code が含まれる
     6.  _RULE_REGISTRY の順序が _evaluate_symbol() の出力順と一致する
         (gap → volume → trend → independent → symbol_range → breakout)

  B. _RULE_DEP_FLAGS 構造テスト
     7.  _RULE_DEP_FLAGS が module レベルに存在する
     8.  _RULE_DEP_FLAGS が 5 エントリを持つ
     9.  is_gap_up / is_gap_down / is_high_volume / is_trend_up / is_trend_down がキーに存在
    10.  _RULE_DEP_FLAGS のキー（state_code）は全て _RULE_REGISTRY に存在する

  C. _evaluate_symbol() single loop 挙動テスト
    11. 全 12 state code が rule_diagnostics に存在する
    12. _rules リスト（ローカル変数）が single loop で回される（data.get が _evaluate_symbol() 内にない）
    13. deps フラグが後続 rule に正しく伝播される（is_gap_up: True → breakout_candidate が抑制される）
    14. deps フラグが後続 rule に正しく伝播される（is_trend_up: True → symbol_range が inactive）
    15. deps フラグが後続 rule に正しく伝播される（is_high_volume: True → breakout_candidate が発火）

  D. symbol_range high ATR 診断に reason="high_atr" が含まれる
    16. high ATR inactive: reason="high_atr" / atr_ratio が含まれる
    17. trending inactive: reason="trending"（reason="high_atr" でないことを確認）
    18. _evaluate_symbol() 経由でも high ATR inactive 診断に reason="high_atr"
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

from trade_app.services.market_state import symbol_evaluator as _mod
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult
from trade_app.services.market_state.symbol_evaluator import (
    SymbolStateEvaluator,
    _RULE_DEP_FLAGS,
    _RULE_REGISTRY,
    _rule_symbol_range,
)

_UTC = timezone.utc
_EVAL_TIME = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

ATR_RATIO_HIGH = SymbolStateEvaluator.ATR_RATIO_HIGH  # 0.02


# ─── テスト用 make ヘルパー ────────────────────────────────────────────────────

def _make(
    ticker: str,
    state_code: str,
    score: float,
    confidence: float,
    evidence: dict[str, Any],
) -> StateEvaluationResult:
    return StateEvaluationResult(
        layer="symbol",
        target_type="symbol",
        target_code=ticker,
        state_code=state_code,
        score=score,
        confidence=confidence,
        evidence=evidence,
    )


# ─── evaluate() 経由ヘルパー ─────────────────────────────────────────────────

def _diags(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """_evaluate_symbol() が返す rule_diagnostics を取得する"""
    evaluator = SymbolStateEvaluator()
    _results, rule_diagnostics = evaluator._evaluate_symbol("TEST", data, _EVAL_TIME)
    return rule_diagnostics


def _active_states(data: dict[str, Any]) -> list[str]:
    """_evaluate_symbol() が返す active state code の一覧"""
    evaluator = SymbolStateEvaluator()
    results, _diags = evaluator._evaluate_symbol("TEST", data, _EVAL_TIME)
    return [r.state_code for r in results]


# ─── テスト用データセット ──────────────────────────────────────────────────────

# ギャップアップ（breakout_candidate を抑制するはず）
_GAP_UP = {
    "current_price": 1020.0,
    "current_open": 1020.0,
    "prev_close": 1000.0,   # +2% gap_up_open
    "ma20": 980.0,
    "current_volume": 300.0,
    "avg_volume_same_time": 100.0,  # high_volume
    "best_bid": 1019.0,
    "best_ask": 1021.0,
}

# トレンドアップ（symbol_range を抑制するはず）
_TREND_UP = {
    "current_price": 1100.0,
    "vwap": 900.0,
    "ma5": 1050.0,
    "ma20": 980.0,
    "atr": 15.0,  # atr_ratio=0.0136 < 0.02（ATR は低いが trend が出ているため range にならない）
    "best_bid": 1099.0,
    "best_ask": 1101.0,
}

# 高出来高 + ギャップなし + MA20 より上（breakout_candidate が発火するはず）
_BREAKOUT = {
    "current_price": 1050.0,
    "current_open": 1005.0,
    "prev_close": 1000.0,   # +0.5% — gap 閾値 2% 未満（gap なし）
    "ma20": 1000.0,
    "current_volume": 300.0,
    "avg_volume_same_time": 100.0,  # vol_ratio=3.0 → is_high_volume=True
    "best_bid": 1049.0,
    "best_ask": 1051.0,
}

# トレンドなし・低 ATR（symbol_range が発火するはず）
_RANGE = {
    "current_price": 1000.0,
    "vwap": 1000.0,     # price == vwap → trend_up 不成立
    "ma5": 1000.0,
    "ma20": 1000.0,
    "atr": 5.0,         # atr_ratio=0.005 < 0.02 → range 発火
    "best_bid": 999.0,
    "best_ask": 1001.0,
}

# 高 ATR（symbol_range が inactive になるはず）
# vwap/ma5/ma20 を省略→ trend rule が skip → is_trend_up=False / is_trend_down=False
_HIGH_ATR = {
    "current_price": 1000.0,
    "atr": 30.0,    # atr_ratio=0.03 >= 0.02 → high_atr inactive
}


# ─── A. _RULE_REGISTRY 構造テスト ──────────────────────────────────────────────

class TestRuleRegistry:
    def test_exists_at_module_level(self):
        assert hasattr(_mod, "_RULE_REGISTRY"), "_RULE_REGISTRY が module にない"

    def test_is_tuple(self):
        assert isinstance(_RULE_REGISTRY, tuple)

    def test_has_14_entries(self):
        assert len(_RULE_REGISTRY) == 14, f"expected 14, got {len(_RULE_REGISTRY)}: {_RULE_REGISTRY}"

    def test_all_unique(self):
        assert len(_RULE_REGISTRY) == len(set(_RULE_REGISTRY)), "重複 state code がある"

    def test_contains_all_state_codes(self):
        expected = {
            "gap_up_open", "gap_down_open",
            "high_relative_volume", "low_liquidity",
            "symbol_trend_up", "symbol_trend_down",
            "wide_spread", "price_stale", "overextended",
            "symbol_volatility_high", "symbol_range", "breakout_candidate",
        }
        missing = expected - set(_RULE_REGISTRY)
        assert not missing, f"registry に不足: {missing}"

    def test_order_dep_providers_before_consumers(self):
        """依存フラグを提供する rule は、それを消費する rule より前にある"""
        registry_list = list(_RULE_REGISTRY)

        # is_gap_up/is_gap_down は breakout_candidate より前
        assert registry_list.index("gap_up_open") < registry_list.index("breakout_candidate")
        assert registry_list.index("gap_down_open") < registry_list.index("breakout_candidate")

        # is_high_volume は breakout_candidate より前
        assert registry_list.index("high_relative_volume") < registry_list.index("breakout_candidate")

        # is_trend_up/is_trend_down は symbol_range より前
        assert registry_list.index("symbol_trend_up") < registry_list.index("symbol_range")
        assert registry_list.index("symbol_trend_down") < registry_list.index("symbol_range")


# ─── B. _RULE_DEP_FLAGS 構造テスト ────────────────────────────────────────────

class TestRuleDepFlags:
    def test_exists_at_module_level(self):
        assert hasattr(_mod, "_RULE_DEP_FLAGS"), "_RULE_DEP_FLAGS が module にない"

    def test_has_5_entries(self):
        assert len(_RULE_DEP_FLAGS) == 5, f"expected 5, got {len(_RULE_DEP_FLAGS)}"

    def test_contains_expected_flags(self):
        expected_flags = {"is_gap_up", "is_gap_down", "is_high_volume", "is_trend_up", "is_trend_down"}
        actual_flags = set(_RULE_DEP_FLAGS.values())
        assert actual_flags == expected_flags

    def test_keys_are_in_registry(self):
        for state_code in _RULE_DEP_FLAGS:
            assert state_code in _RULE_REGISTRY, f"{state_code} が _RULE_REGISTRY にない"


# ─── C. _evaluate_symbol() single loop 挙動テスト ─────────────────────────────

class TestEvaluateSymbolSingleLoop:
    def test_all_state_codes_in_diagnostics(self):
        """全 state code が rule_diagnostics に存在する"""
        diags = _diags({})
        missing = set(_RULE_REGISTRY) - set(diags.keys())
        assert not missing, f"rule_diagnostics に不足: {missing}"

    def test_no_data_get_in_evaluate_symbol(self):
        """_evaluate_symbol() のソースに data.get( がない（全 rule 化の証明）"""
        src = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert "data.get(" not in src, \
            "_evaluate_symbol() に data.get() が残っている（rule 化が不完全）"

    def test_gap_up_suppresses_breakout(self):
        """is_gap_up=True のとき breakout_candidate は発火しない"""
        active = _active_states(_GAP_UP)
        assert "gap_up_open" in active
        assert "breakout_candidate" not in active

    def test_trend_up_suppresses_range(self):
        """is_trend_up=True のとき symbol_range は inactive になる"""
        diags = _diags(_TREND_UP)
        assert diags["symbol_trend_up"]["status"] == "active"
        assert diags["symbol_range"]["status"] == "inactive"
        assert diags["symbol_range"].get("reason") == "trending"

    def test_high_volume_enables_breakout(self):
        """is_high_volume=True + ギャップなし + price > MA20 → breakout_candidate が発火する"""
        active = _active_states(_BREAKOUT)
        assert "high_relative_volume" in active
        assert "breakout_candidate" in active


# ─── D. symbol_range high ATR 診断 reason="high_atr" テスト ─────────────────────

class TestSymbolRangeHighAtrReason:
    def test_high_atr_inactive_has_reason(self):
        """高 ATR inactive 診断に reason="high_atr" が含まれる"""
        _result, diag = _rule_symbol_range(
            "TEST", _HIGH_ATR,
            is_trend_up=False,
            is_trend_down=False,
            atr_ratio_high=ATR_RATIO_HIGH,
            make=_make,
        )
        assert _result is None
        assert diag["status"] == "inactive"
        assert diag.get("reason") == "high_atr"
        assert "atr_ratio" in diag

    def test_trending_inactive_reason_is_trending_not_high_atr(self):
        """trending inactive は reason="trending"（reason="high_atr" でない）"""
        _result, diag = _rule_symbol_range(
            "TEST", _HIGH_ATR,
            is_trend_up=True,
            is_trend_down=False,
            atr_ratio_high=ATR_RATIO_HIGH,
            make=_make,
        )
        assert _result is None
        assert diag["status"] == "inactive"
        assert diag.get("reason") == "trending"
        assert diag.get("reason") != "high_atr"

    def test_high_atr_reason_via_evaluate_symbol(self):
        """_evaluate_symbol() 経由でも high ATR inactive 診断に reason="high_atr" が含まれる"""
        diags = _diags(_HIGH_ATR)
        d = diags["symbol_range"]
        assert d["status"] == "inactive"
        assert d.get("reason") == "high_atr"
        assert "atr_ratio" in d
