"""
Phase AR-1 テスト — pVWAP mapper 実装

目的:
  Tachibana demo API が返す pVWAP (numeric key=213) を
  MarketData.vwap → symbol_data["vwap"] → evaluator まで
  正しく流れることを検証する。

検証項目:
  1. _NUMERIC_KEY_MAP に "213" → "pVWAP" が存在する
  2. map_symbol_market_data_from_entry が pVWAP を抽出して 4-tuple で返す
  3. map_symbol_market_data が 4-tuple で返す（pVWAP なし → None）
  4. MarketData に vwap フィールドが存在する（デフォルト None）
  5. SymbolDataFetcher が MarketData.vwap を symbol_data に含める
  6. symbol_trend_up が no_vwap で skipped しなくなる（vwap 注入後）
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from trade_app.brokers.base import BrokerAPIError, MarketData
from trade_app.brokers.tachibana.client import _NUMERIC_KEY_MAP
from trade_app.brokers.tachibana import mapper
from trade_app.brokers.tachibana.mapper import (
    map_symbol_market_data_from_entry,
    map_symbol_market_data,
)
from trade_app.services.market_state.symbol_data_fetcher import SymbolDataFetcher
from trade_app.services.market_state.symbol_evaluator import SymbolStateEvaluator


# ─── 1. _NUMERIC_KEY_MAP ──────────────────────────────────────────────────────

class TestNumericKeyMap:
    def test_213_maps_to_pVWAP(self):
        assert "213" in _NUMERIC_KEY_MAP
        assert _NUMERIC_KEY_MAP["213"] == "pVWAP"

    def test_existing_keys_unchanged(self):
        assert _NUMERIC_KEY_MAP["115"] == "pDPP"
        assert _NUMERIC_KEY_MAP["184"] == "pQBP"
        assert _NUMERIC_KEY_MAP["182"] == "pQAP"


# ─── 2. map_symbol_market_data_from_entry ─────────────────────────────────────

class TestMapSymbolMarketDataFromEntry:
    def test_returns_4_tuple(self):
        entry = {"pDPP": "3400", "pQBP": "3390", "pQAP": "3410", "pVWAP": "3375"}
        result = map_symbol_market_data_from_entry(entry)
        assert len(result) == 4

    def test_extracts_vwap(self):
        entry = {"pDPP": "3400", "pQBP": "3390", "pQAP": "3410", "pVWAP": "3375"}
        cp, bid, ask, vwap = map_symbol_market_data_from_entry(entry)
        assert vwap == 3375.0

    def test_vwap_missing_returns_none(self):
        entry = {"pDPP": "3400", "pQBP": "3390", "pQAP": "3410"}
        _, _, _, vwap = map_symbol_market_data_from_entry(entry)
        assert vwap is None

    def test_vwap_empty_string_returns_none(self):
        entry = {"pDPP": "3400", "pQBP": "3390", "pQAP": "3410", "pVWAP": ""}
        _, _, _, vwap = map_symbol_market_data_from_entry(entry)
        assert vwap is None

    def test_vwap_zero_returns_none(self):
        entry = {"pDPP": "3400", "pQBP": "3390", "pQAP": "3410", "pVWAP": "0"}
        _, _, _, vwap = map_symbol_market_data_from_entry(entry)
        assert vwap is None

    def test_vwap_negative_returns_none(self):
        entry = {"pVWAP": "-100"}
        _, _, _, vwap = map_symbol_market_data_from_entry(entry)
        assert vwap is None

    def test_existing_fields_unchanged(self):
        entry = {"pDPP": "3400", "pQBP": "3390", "pQAP": "3410", "pVWAP": "3375"}
        cp, bid, ask, vwap = map_symbol_market_data_from_entry(entry)
        assert cp == 3400.0
        assert bid == 3390.0
        assert ask == 3410.0


# ─── 3. map_symbol_market_data ────────────────────────────────────────────────

class TestMapSymbolMarketData:
    def _raw(self, pDPP="3400", pQBP="3390", pQAP="3410", pVWAP="3375"):
        entry = {"pDPP": pDPP, "pQBP": pQBP, "pQAP": pQAP, "pVWAP": pVWAP}
        return {"aCLMMfdsMarketPrice": [entry]}

    def test_returns_4_tuple(self):
        result = map_symbol_market_data(self._raw())
        assert len(result) == 4

    def test_extracts_vwap(self):
        _, _, _, vwap = map_symbol_market_data(self._raw(pVWAP="3375"))
        assert vwap == 3375.0

    def test_vwap_missing_returns_none(self):
        raw = {"aCLMMfdsMarketPrice": [{"pDPP": "3400", "pQBP": "3390", "pQAP": "3410"}]}
        _, _, _, vwap = map_symbol_market_data(raw)
        assert vwap is None

    def test_empty_array_returns_4_nones(self):
        result = map_symbol_market_data({"aCLMMfdsMarketPrice": []})
        assert result == (None, None, None, None)

    def test_missing_array_returns_4_nones(self):
        result = map_symbol_market_data({})
        assert result == (None, None, None, None)


# ─── 4. MarketData.vwap ───────────────────────────────────────────────────────

class TestMarketDataVwap:
    def test_vwap_field_exists(self):
        md = MarketData(current_price=3400.0, best_bid=3390.0, best_ask=3410.0)
        assert hasattr(md, "vwap")

    def test_vwap_default_none(self):
        md = MarketData(current_price=3400.0, best_bid=3390.0, best_ask=3410.0)
        assert md.vwap is None

    def test_vwap_set(self):
        md = MarketData(current_price=3400.0, best_bid=3390.0, best_ask=3410.0, vwap=3375.0)
        assert md.vwap == 3375.0


# ─── 5. SymbolDataFetcher ─────────────────────────────────────────────────────

def _make_broker(data: dict[str, MarketData]) -> AsyncMock:
    broker = AsyncMock()
    async def _get(ticker):
        if ticker in data:
            return data[ticker]
        raise BrokerAPIError("not found")
    broker.get_market_data.side_effect = _get
    return broker


class TestSymbolDataFetcherVwap:
    @pytest.mark.asyncio
    async def test_vwap_included_in_result(self):
        md = MarketData(current_price=3400.0, best_bid=3390.0, best_ask=3410.0, vwap=3375.0)
        broker = _make_broker({"7203": md})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert "vwap" in result["7203"]
        assert result["7203"]["vwap"] == 3375.0

    @pytest.mark.asyncio
    async def test_vwap_none_when_not_available(self):
        md = MarketData(current_price=3400.0, best_bid=3390.0, best_ask=3410.0, vwap=None)
        broker = _make_broker({"7203": md})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert result["7203"]["vwap"] is None

    @pytest.mark.asyncio
    async def test_vwap_in_all_expected_fields(self):
        md = MarketData(current_price=3400.0, best_bid=3390.0, best_ask=3410.0, vwap=3375.0)
        broker = _make_broker({"7203": md})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        expected = {"current_price", "best_bid", "best_ask", "vwap", "last_updated", "bid_ask_updated"}
        assert set(result["7203"].keys()) == expected


# ─── 6. symbol_trend_up: no_vwap 解消確認 ────────────────────────────────────

class TestSymbolTrendUpVwapResolved:
    """vwap が symbol_data に入ることで no_vwap skipped が解消されることを確認する。"""

    def _make_evaluator(self):
        ev = SymbolStateEvaluator.__new__(SymbolStateEvaluator)
        ev.GAP_THRESHOLD = 0.02
        ev.VOLUME_RATIO_HIGH = 2.0
        ev.VOLUME_RATIO_LOW = 0.5
        ev.ATR_RATIO_HIGH = 0.02
        ev.RSI_OVERBOUGHT = 75.0
        ev.RSI_OVERSOLD = 25.0
        ev._make = lambda ticker, state_code, score, evidence, eval_time: type(
            "R", (), {
                "target_code": ticker,
                "state_code": state_code,
                "score": score,
                "evidence": evidence,
                "is_new_activation": True,
            }
        )()
        return ev

    def test_no_vwap_causes_skipped(self):
        """vwap が None のときは skipped(no_vwap) になる"""
        from trade_app.services.market_state.symbol_evaluator import _rule_symbol_trend_up
        data = {"current_price": 3400.0, "ma5": 3410.0, "ma20": 3390.0}  # vwap なし
        ev = self._make_evaluator()
        result, diag = _rule_symbol_trend_up("7203", data, make=ev._make)
        assert result is None
        assert diag["status"] == "skipped"
        assert diag["reason"] == "no_vwap"

    def test_with_vwap_evaluates(self):
        """vwap が入れば skipped にならず評価される"""
        from trade_app.services.market_state.symbol_evaluator import _rule_symbol_trend_up
        data = {
            "current_price": 3400.0,
            "vwap": 3380.0,   # price > vwap
            "ma5": 3410.0,    # ma5 > ma20
            "ma20": 3390.0,
        }
        ev = self._make_evaluator()
        result, diag = _rule_symbol_trend_up("7203", data, make=ev._make)
        # skipped ではないことを確認（active または inactive）
        assert diag["status"] != "skipped"

    def test_with_vwap_trend_up_active(self):
        """price > vwap AND ma5 > ma20 → active"""
        from trade_app.services.market_state.symbol_evaluator import _rule_symbol_trend_up
        data = {
            "current_price": 3400.0,
            "vwap": 3380.0,
            "ma5": 3410.0,
            "ma20": 3390.0,
        }
        ev = self._make_evaluator()
        result, diag = _rule_symbol_trend_up("7203", data, make=ev._make)
        assert result is not None
        assert result.state_code == "symbol_trend_up"
        assert diag["status"] == "active"

    def test_with_vwap_trend_up_inactive(self):
        """price < vwap → inactive（no_vwap ではない）"""
        from trade_app.services.market_state.symbol_evaluator import _rule_symbol_trend_up
        data = {
            "current_price": 3350.0,   # price < vwap
            "vwap": 3380.0,
            "ma5": 3410.0,
            "ma20": 3390.0,
        }
        ev = self._make_evaluator()
        result, diag = _rule_symbol_trend_up("7203", data, make=ev._make)
        assert result is None
        assert diag["status"] == "inactive"
