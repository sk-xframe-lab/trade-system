"""
Phase G — observability 強化テスト

確認項目:
  A. rule 診断 直テスト（_evaluate_symbol() 経由）
     各 rule の status / reason / metrics を直接確認する。

     wide_spread:
      1.  active   → {status: active, spread_rate, spread}
      2.  inactive → {status: inactive, spread_rate}
      3.  skipped  → {status: skipped, reason: no_current_price}
      4.  skipped  → {status: skipped, reason: no_bid}
      5.  skipped  → {status: skipped, reason: no_ask}
      6.  skipped  → {status: skipped, reason: inverted_spread}

     price_stale:
      7.  active   → {status: active, reason: stale_price, age_sec}
      8.  active   → {status: active, reason: missing_price}
      9.  active   → {status: active, reason: missing_timestamp}
     10.  inactive → {status: inactive, age_sec}
     11.  skipped  → {status: skipped, reason: no_last_updated_key}

     overextended:
     12.  active   → {status: active, direction: overbought, rsi}
     13.  active   → {status: active, direction: oversold, rsi}
     14.  inactive → {status: inactive, rsi}
     15.  skipped  → {status: skipped, reason: no_rsi}

     symbol_volatility_high:
     16.  active   → {status: active, atr_ratio}
     17.  inactive → {status: inactive, atr_ratio}
     18.  skipped  → {status: skipped, reason: no_current_price}
     19.  skipped  → {status: skipped, reason: no_atr}

  B. 全 rule キーの存在確認
     20. symbol_data に何もフィールドがなくても 4 つのキーが揃う

  C. engine snapshot 統合テスト
     21. engine.run() 後に snapshot の state_summary_json["rule_diagnostics"] に
         4 つのキーが含まれる
     22. active rule の diagnostic が snapshot に正しく書き込まれる
     23. 全 rule が skipped のとき snapshot に rule_diagnostics キーが存在する

設計:
  - _evaluate_symbol() は (results, rule_diagnostics) を返すので直接呼び出す
  - rule_diagnostics のキーは "wide_spread" / "price_stale" / "overextended" / "symbol_volatility_high"
  - snapshot.state_summary_json["rule_diagnostics"] が診断サマリの永続化先
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.services.market_state.engine import MarketStateEngine
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.market_state.schemas import EvaluationContext
from trade_app.services.market_state.symbol_evaluator import SymbolStateEvaluator

_UTC = timezone.utc
_EVAL_TIME = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

_RULE_KEYS = {
    "gap_up_open", "gap_down_open",
    "high_relative_volume", "low_liquidity",
    "symbol_trend_up", "symbol_trend_down",
    "wide_spread", "price_stale", "overextended", "symbol_volatility_high",
    "symbol_range",
    "breakout_candidate",
    "quote_only",
    "stale_bid_ask",
}

# stale timestamp: 評価時刻の 120秒前
_LAST_UPDATED_STALE = _EVAL_TIME - timedelta(seconds=120)
# fresh timestamp: 評価時刻の 30秒前
_LAST_UPDATED_FRESH = _EVAL_TIME - timedelta(seconds=30)


def _diags(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """_evaluate_symbol() を呼び出して rule_diagnostics のみ返すヘルパー。"""
    evaluator = SymbolStateEvaluator()
    _results, diagnostics = evaluator._evaluate_symbol("7203", data, _EVAL_TIME)
    return diagnostics


# ─── A. rule 診断 直テスト ────────────────────────────────────────────────────

class TestWidespreadDiagnostic:
    """wide_spread rule の diagnostic 内容を確認する"""

    def test_active(self):
        """spread_rate >= threshold → status=active、spread_rate と spread を含む"""
        # spread=12, current_price=1000 → spread_rate=0.012 >= 0.003
        diags = _diags({"current_price": 1000.0, "best_bid": 994.0, "best_ask": 1006.0})
        d = diags["wide_spread"]
        assert d["status"] == "active"
        assert "spread_rate" in d
        assert "spread" in d
        assert d["spread_rate"] == pytest.approx(0.012, abs=1e-5)

    def test_inactive(self):
        """spread_rate < threshold → status=inactive、spread_rate を含む"""
        # spread=2, current_price=1000 → spread_rate=0.002 < 0.003
        diags = _diags({"current_price": 1000.0, "best_bid": 999.0, "best_ask": 1001.0})
        d = diags["wide_spread"]
        assert d["status"] == "inactive"
        assert "spread_rate" in d
        assert d["spread_rate"] == pytest.approx(0.002, abs=1e-5)

    def test_skipped_no_current_price(self):
        """current_price=None → status=skipped, reason=no_current_price"""
        diags = _diags({"current_price": None, "best_bid": 990.0, "best_ask": 1010.0})
        d = diags["wide_spread"]
        assert d["status"] == "skipped"
        assert d["reason"] == "no_current_price"

    def test_skipped_no_bid(self):
        """best_bid=None → status=skipped, reason=no_bid"""
        diags = _diags({"current_price": 1000.0, "best_bid": None, "best_ask": 1010.0})
        d = diags["wide_spread"]
        assert d["status"] == "skipped"
        assert d["reason"] == "no_bid"

    def test_skipped_no_ask(self):
        """best_ask=None → status=skipped, reason=no_ask"""
        diags = _diags({"current_price": 1000.0, "best_bid": 990.0, "best_ask": None})
        d = diags["wide_spread"]
        assert d["status"] == "skipped"
        assert d["reason"] == "no_ask"

    def test_skipped_inverted_spread(self):
        """best_ask < best_bid → status=skipped, reason=inverted_spread"""
        diags = _diags({"current_price": 1000.0, "best_bid": 1010.0, "best_ask": 990.0})
        d = diags["wide_spread"]
        assert d["status"] == "skipped"
        assert d["reason"] == "inverted_spread"


class TestPriceStaleDiagnostic:
    """price_stale rule の diagnostic 内容を確認する"""

    def test_active_stale_price(self):
        """age_sec >= threshold → status=active, reason=stale_price, age_sec"""
        diags = _diags({"current_price": 1000.0, "last_updated": _LAST_UPDATED_STALE})
        d = diags["price_stale"]
        assert d["status"] == "active"
        assert d["reason"] == "stale_price"
        assert "age_sec" in d
        assert d["age_sec"] == pytest.approx(120.0, abs=1.0)

    def test_active_missing_price(self):
        """current_price=None + last_updated → status=active, reason=missing_price"""
        diags = _diags({"current_price": None, "last_updated": _LAST_UPDATED_STALE})
        d = diags["price_stale"]
        assert d["status"] == "active"
        assert d["reason"] == "missing_price"

    def test_active_missing_timestamp(self):
        """current_price あり + last_updated=None → status=active, reason=missing_timestamp"""
        diags = _diags({"current_price": 1000.0, "last_updated": None})
        d = diags["price_stale"]
        assert d["status"] == "active"
        assert d["reason"] == "missing_timestamp"

    def test_inactive_fresh(self):
        """age_sec < threshold → status=inactive, age_sec"""
        diags = _diags({"current_price": 1000.0, "last_updated": _LAST_UPDATED_FRESH})
        d = diags["price_stale"]
        assert d["status"] == "inactive"
        assert "age_sec" in d
        assert d["age_sec"] == pytest.approx(30.0, abs=1.0)

    def test_skipped_no_key(self):
        """last_updated キーなし → status=skipped, reason=no_last_updated_key"""
        diags = _diags({"current_price": 1000.0})  # last_updated キーなし
        d = diags["price_stale"]
        assert d["status"] == "skipped"
        assert d["reason"] == "no_last_updated_key"


class TestOverextendedDiagnostic:
    """overextended rule の diagnostic 内容を確認する"""

    def test_active_overbought(self):
        """rsi >= 75 → status=active, direction=overbought, rsi"""
        diags = _diags({"rsi": 80.0})
        d = diags["overextended"]
        assert d["status"] == "active"
        assert d["direction"] == "overbought"
        assert d["rsi"] == 80.0

    def test_active_oversold(self):
        """rsi <= 25 → status=active, direction=oversold, rsi"""
        diags = _diags({"rsi": 20.0})
        d = diags["overextended"]
        assert d["status"] == "active"
        assert d["direction"] == "oversold"
        assert d["rsi"] == 20.0

    def test_inactive_neutral(self):
        """中立 RSI → status=inactive, rsi"""
        diags = _diags({"rsi": 50.0})
        d = diags["overextended"]
        assert d["status"] == "inactive"
        assert d["rsi"] == 50.0

    def test_skipped_no_rsi(self):
        """rsi キーなし → status=skipped, reason=no_rsi"""
        diags = _diags({})
        d = diags["overextended"]
        assert d["status"] == "skipped"
        assert d["reason"] == "no_rsi"


class TestVolatilityHighDiagnostic:
    """symbol_volatility_high rule の diagnostic 内容を確認する"""

    def test_active(self):
        """atr_ratio >= threshold → status=active, atr_ratio"""
        # atr=30, current_price=1000 → atr_ratio=0.03 >= 0.02
        diags = _diags({"current_price": 1000.0, "atr": 30.0})
        d = diags["symbol_volatility_high"]
        assert d["status"] == "active"
        assert "atr_ratio" in d
        assert d["atr_ratio"] == pytest.approx(0.03, abs=1e-6)

    def test_inactive(self):
        """atr_ratio < threshold → status=inactive, atr_ratio"""
        # atr=10, current_price=1000 → atr_ratio=0.01 < 0.02
        diags = _diags({"current_price": 1000.0, "atr": 10.0})
        d = diags["symbol_volatility_high"]
        assert d["status"] == "inactive"
        assert d["atr_ratio"] == pytest.approx(0.01, abs=1e-6)

    def test_skipped_no_current_price(self):
        """current_price=None → status=skipped, reason=no_current_price"""
        diags = _diags({"current_price": None, "atr": 30.0})
        d = diags["symbol_volatility_high"]
        assert d["status"] == "skipped"
        assert d["reason"] == "no_current_price"

    def test_skipped_no_atr(self):
        """atr キーなし → status=skipped, reason=no_atr"""
        diags = _diags({"current_price": 1000.0})
        d = diags["symbol_volatility_high"]
        assert d["status"] == "skipped"
        assert d["reason"] == "no_atr"


# ─── B. 全 rule キーの存在確認 ─────────────────────────────────────────────────

class TestAllRuleKeysPresent:
    """空データでも 4 つの rule キーが揃っていること"""

    def test_all_four_keys_present_with_empty_data(self):
        """symbol_data が空 dict でも 4 キーが返る"""
        diags = _diags({})
        assert _RULE_KEYS == set(diags.keys()), \
            f"期待: {_RULE_KEYS}, 実際: {set(diags.keys())}"

    def test_all_four_keys_present_with_partial_data(self):
        """一部フィールドのみのデータでも 4 キーが揃う"""
        diags = _diags({"current_price": 1000.0, "rsi": 50.0})
        assert _RULE_KEYS == set(diags.keys())

    def test_all_rules_active(self):
        """
        各 rule が active になるデータでステータスが正しいこと。

        注: gap_up_open が active のとき breakout_candidate は inactive になる（ギャップ抑制）。
        両者は別データセットで確認する。
        """
        # gap なし: wide_spread / price_stale / overextended / symbol_volatility_high /
        #            high_relative_volume / breakout_candidate / symbol_trend_up
        diags_no_gap = _diags({
            "current_price": 1100.0,
            "best_bid": 1089.0, "best_ask": 1111.0,
            "last_updated": _LAST_UPDATED_STALE,
            "rsi": 80.0,
            "atr": 33.0,
            "ma20": 1000.0,
            "current_volume": 300_000, "avg_volume_same_time": 100_000,
            "vwap": 900.0, "ma5": 1050.0,  # trend_up: price(1100) > vwap(900) AND ma5(1050) > ma20(1000)
        })
        assert diags_no_gap["wide_spread"]["status"] == "active"
        assert diags_no_gap["price_stale"]["status"] == "active"
        assert diags_no_gap["overextended"]["status"] == "active"
        assert diags_no_gap["symbol_volatility_high"]["status"] == "active"
        assert diags_no_gap["high_relative_volume"]["status"] == "active"   # vol_ratio=3.0
        assert diags_no_gap["symbol_trend_up"]["status"] == "active"
        assert diags_no_gap["breakout_candidate"]["status"] == "active"

        # symbol_trend_down: trend_up と排他的なので別データセット
        diags_trend_down = _diags({
            "current_price": 900.0,
            "vwap": 1000.0, "ma5": 950.0, "ma20": 1000.0,  # price(900)<vwap(1000) AND ma5(950)<ma20(1000)
        })
        assert diags_trend_down["symbol_trend_down"]["status"] == "active"

        # low_liquidity: high_relative_volume と排他的なので別データセット
        diags_low_liq = _diags({
            "current_volume": 10_000, "avg_volume_same_time": 100_000,  # vol_ratio=0.1 < 0.3
        })
        assert diags_low_liq["low_liquidity"]["status"] == "active"

        # symbol_range: トレンドなし かつ ATR 低水準（trend_up と排他的なので別データセット）
        diags_range = _diags({
            "current_price": 1000.0,
            "atr": 10.0,  # atr_ratio=0.01 < 0.02 → symbol_range 発火（トレンドデータなし）
        })
        assert diags_range["symbol_range"]["status"] == "active"

        # gap_up あり: gap_up_open active
        diags_gap_up = _diags({
            "current_price": 1163.0,
            "current_open": 1163.0, "prev_close": 1000.0,  # gap_pct=16.3% → gap_up_open
        })
        assert diags_gap_up["gap_up_open"]["status"] == "active"


# ─── C. engine snapshot 統合テスト ───────────────────────────────────────────

@pytest.mark.asyncio
class TestSnapshotDiagnostics:
    """engine.run() 後に snapshot の state_summary_json に rule_diagnostics が書き込まれる"""

    async def test_rule_diagnostics_key_in_snapshot(self, db_session: AsyncSession):
        """engine.run() 後、snapshot.state_summary_json に "rule_diagnostics" キーがある"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": {"current_price": 1000.0, "rsi": 50.0}},
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        snapshot = await repo.get_symbol_snapshot("7203")
        assert snapshot is not None
        summary = snapshot.state_summary_json
        assert "rule_diagnostics" in summary, \
            f"state_summary_json に rule_diagnostics がない: {summary}"

    async def test_all_four_rule_keys_in_snapshot(self, db_session: AsyncSession):
        """snapshot の rule_diagnostics に全 rule キーが揃う"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": {}},
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        snapshot = await repo.get_symbol_snapshot("7203")
        assert snapshot is not None
        rule_diag = snapshot.state_summary_json["rule_diagnostics"]
        assert _RULE_KEYS == set(rule_diag.keys()), \
            f"rule_diagnostics のキーが不足: {set(rule_diag.keys())}"

    async def test_active_rule_diagnostic_in_snapshot(self, db_session: AsyncSession):
        """active な rule の diagnostic が snapshot に正しく書き込まれる"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": {
                "current_price": 1000.0,
                "atr": 30.0,   # symbol_volatility_high: 0.03 >= 0.02
                "rsi": 80.0,   # overextended: overbought
            }},
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        snapshot = await repo.get_symbol_snapshot("7203")
        assert snapshot is not None
        rule_diag = snapshot.state_summary_json["rule_diagnostics"]

        assert rule_diag["symbol_volatility_high"]["status"] == "active"
        assert rule_diag["symbol_volatility_high"]["atr_ratio"] == pytest.approx(0.03, abs=1e-5)
        assert rule_diag["overextended"]["status"] == "active"
        assert rule_diag["overextended"]["direction"] == "overbought"
        assert rule_diag["overextended"]["rsi"] == 80.0

    async def test_inactive_skipped_diagnostics_in_snapshot(self, db_session: AsyncSession):
        """inactive / skipped な rule の diagnostic も snapshot に含まれる"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            # no best_bid/ask (wide_spread → skipped), no rsi (overextended → skipped)
            # atr=10 (volatility_high → inactive), no last_updated (price_stale → skipped)
            symbol_data={"7203": {"current_price": 1000.0, "atr": 10.0}},
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        snapshot = await repo.get_symbol_snapshot("7203")
        assert snapshot is not None
        rule_diag = snapshot.state_summary_json["rule_diagnostics"]

        assert rule_diag["wide_spread"]["status"] == "skipped"
        assert rule_diag["overextended"]["status"] == "skipped"
        assert rule_diag["price_stale"]["status"] == "skipped"
        assert rule_diag["symbol_volatility_high"]["status"] == "inactive"
        assert rule_diag["symbol_volatility_high"]["atr_ratio"] == pytest.approx(0.01, abs=1e-5)

    async def test_snapshot_updated_on_second_run(self, db_session: AsyncSession):
        """2回目の run() で snapshot の rule_diagnostics も更新される"""
        engine = MarketStateEngine(db_session)

        # run1: overextended active
        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": {"rsi": 80.0}},
        )
        await engine.run(ctx1)

        # run2: overextended inactive (中立 RSI)
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": {"rsi": 50.0}},
        )
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        snapshot = await repo.get_symbol_snapshot("7203")
        assert snapshot is not None
        rule_diag = snapshot.state_summary_json["rule_diagnostics"]

        # run2 の状態が反映されているはず
        assert rule_diag["overextended"]["status"] == "inactive"
        assert rule_diag["overextended"]["rsi"] == 50.0
