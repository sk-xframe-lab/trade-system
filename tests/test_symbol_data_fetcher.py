"""
SymbolDataFetcher テスト

Phase 2 Step B: current_price + best_bid + best_ask を取得する実装の動作を検証する。

検証内容:
  - 正常取得 → {"current_price": price, "best_bid": bid, "best_ask": ask} が返る
  - get_market_data() の各フィールドが None → そのまま含まれる（正常系）
  - 例外 → その ticker は結果から除外される（他 ticker に影響しない）
  - BrokerAuthError → 除外される（AUTH エラーも握りつぶし）
  - 空リスト → 空 dict
  - 複数 ticker の失敗分離
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from trade_app.brokers.base import BrokerAPIError, BrokerAuthError, MarketData
from trade_app.services.market_state.symbol_data_fetcher import SymbolDataFetcher


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_broker(
    data: dict[str, MarketData | Exception]
) -> AsyncMock:
    """
    get_market_data(ticker) の挙動を定義した MockBrokerAdapter を返す。

    data dict の値:
      MarketData インスタンス → その値を返す
      Exception インスタンス → raise する
    """
    broker = AsyncMock()

    async def _get_data(ticker: str) -> MarketData:
        val = data.get(ticker)
        if isinstance(val, Exception):
            raise val
        return val

    broker.get_market_data.side_effect = _get_data
    return broker


def _md(price: float | None, bid: float | None = None, ask: float | None = None) -> MarketData:
    """MarketData を簡易作成するヘルパー"""
    return MarketData(current_price=price, best_bid=bid, best_ask=ask)


# ─── 正常系 ───────────────────────────────────────────────────────────────────

class TestFetchSuccess:
    """正常取得ケース"""

    @pytest.mark.asyncio
    async def test_single_ticker_with_bid_ask(self):
        """current_price + best_bid + best_ask + last_updated が全て返る"""
        broker = _make_broker({"7203": _md(3400.0, bid=3395.0, ask=3405.0)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert result["7203"]["current_price"] == 3400.0
        assert result["7203"]["best_bid"] == 3395.0
        assert result["7203"]["best_ask"] == 3405.0
        assert "last_updated" in result["7203"]

    @pytest.mark.asyncio
    async def test_multiple_tickers(self):
        broker = _make_broker({
            "7203": _md(3400.0, bid=3395.0, ask=3405.0),
            "6758": _md(1500.0, bid=1490.0, ask=1510.0),
        })
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203", "6758"])
        assert result["7203"]["current_price"] == 3400.0
        assert result["7203"]["best_bid"] == 3395.0
        assert result["7203"]["best_ask"] == 3405.0
        assert result["6758"]["current_price"] == 1500.0
        assert result["6758"]["best_bid"] == 1490.0
        assert result["6758"]["best_ask"] == 1510.0

    @pytest.mark.asyncio
    async def test_none_fields_are_included(self):
        """
        各フィールドが None を返す（取引時間外・データなし等）は正常系。
        None として結果に含めて snapshot の updated_at をリセットする。
        last_updated は fetch 実行時刻が設定される（None にならない）。
        """
        broker = _make_broker({"7203": _md(None, bid=None, ask=None)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert result["7203"]["current_price"] is None
        assert result["7203"]["best_bid"] is None
        assert result["7203"]["best_ask"] is None
        assert "last_updated" in result["7203"]

    @pytest.mark.asyncio
    async def test_price_only_bid_ask_none(self):
        """current_price あり、bid/ask なし（取引時間外等）"""
        broker = _make_broker({"7203": _md(3400.0, bid=None, ask=None)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert result["7203"]["current_price"] == 3400.0
        assert result["7203"]["best_bid"] is None
        assert result["7203"]["best_ask"] is None

    @pytest.mark.asyncio
    async def test_empty_tickers_returns_empty(self):
        broker = AsyncMock()
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch([])
        assert result == {}
        broker.get_market_data.assert_not_called()


# ─── 失敗分離 ─────────────────────────────────────────────────────────────────

class TestFetchFailureIsolation:
    """ticker 単位の失敗分離"""

    @pytest.mark.asyncio
    async def test_broker_api_error_excludes_ticker(self):
        """BrokerAPIError → その ticker は除外される"""
        broker = _make_broker({"7203": BrokerAPIError("network error")})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_broker_auth_error_excludes_ticker(self):
        """BrokerAuthError も除外される（系全体を止めない）"""
        broker = _make_broker({"7203": BrokerAuthError("auth failed")})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_one_failure_does_not_affect_others(self):
        """
        1 ticker が例外 → その ticker のみ除外、他 ticker は正常に返る
        """
        broker = _make_broker({
            "7203": _md(3400.0, bid=3390.0, ask=3410.0),
            "6758": BrokerAPIError("timeout"),
            "9984": _md(1800.0, bid=1790.0, ask=1810.0),
        })
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203", "6758", "9984"])
        assert "7203" in result
        assert result["7203"]["current_price"] == 3400.0
        assert "9984" in result
        assert result["9984"]["current_price"] == 1800.0
        assert "6758" not in result

    @pytest.mark.asyncio
    async def test_all_fail_returns_empty(self):
        """全 ticker が失敗 → 空 dict を返す（例外はシステムに伝播しない）"""
        broker = _make_broker({
            "7203": BrokerAPIError("error"),
            "6758": BrokerAPIError("error"),
        })
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203", "6758"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_generic_exception_excluded(self):
        """予期しない例外（RuntimeError 等）も握りつぶして除外される"""
        broker = _make_broker({"7203": RuntimeError("unexpected")})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert result == {}


# ─── 呼び出し順 ───────────────────────────────────────────────────────────────

class TestFetchCallOrder:
    """get_market_data の呼び出し順序検証"""

    @pytest.mark.asyncio
    async def test_each_ticker_called_once(self):
        """各 ticker に対して get_market_data が1回ずつ呼ばれる"""
        broker = _make_broker({
            "7203": _md(3400.0, bid=3390.0, ask=3410.0),
            "6758": _md(1500.0, bid=1490.0, ask=1510.0),
        })
        fetcher = SymbolDataFetcher(broker)
        await fetcher.fetch(["7203", "6758"])
        assert broker.get_market_data.call_count == 2
        calls = [c.args[0] for c in broker.get_market_data.call_args_list]
        assert "7203" in calls
        assert "6758" in calls

    @pytest.mark.asyncio
    async def test_result_contains_expected_fields(self):
        """fetch 結果は current_price / best_bid / best_ask / last_updated / bid_ask_updated の5フィールドを含む"""
        broker = _make_broker({"7203": _md(3400.0, bid=3390.0, ask=3410.0)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert set(result["7203"].keys()) == {
            "current_price", "best_bid", "best_ask", "last_updated", "bid_ask_updated"
        }


# ─── last_updated ─────────────────────────────────────────────────────────────

class TestFetchLastUpdated:
    """fetch() が last_updated を正しく設定すること"""

    @pytest.mark.asyncio
    async def test_last_updated_is_present(self):
        """fetch 成功時 last_updated キーが存在する"""
        broker = _make_broker({"7203": _md(3400.0, bid=3390.0, ask=3410.0)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert "last_updated" in result["7203"]

    @pytest.mark.asyncio
    async def test_last_updated_is_datetime(self):
        """last_updated は datetime オブジェクト"""
        broker = _make_broker({"7203": _md(3400.0)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert isinstance(result["7203"]["last_updated"], datetime)

    @pytest.mark.asyncio
    async def test_last_updated_is_timezone_aware(self):
        """last_updated は timezone-aware（UTC）"""
        broker = _make_broker({"7203": _md(3400.0)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        lu = result["7203"]["last_updated"]
        assert lu.tzinfo is not None

    @pytest.mark.asyncio
    async def test_last_updated_when_fields_none(self):
        """current_price=None でも last_updated は設定される"""
        broker = _make_broker({"7203": _md(None)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert isinstance(result["7203"]["last_updated"], datetime)
