"""
SymbolDataFetcher — 銘柄ごとの市場データを BrokerAdapter から収集する

責務:
  MarketStateRunner から依頼された ticker リストに対し、
  BrokerAdapter.get_market_data() を呼び出して symbol_data dict を構築する。

Phase 2 Step B から current_price + best_bid + best_ask を取得する。
  - best_bid / best_ask が取得できれば wide_spread ルールが発火可能になる
  - sTargetColumn=pDPP,pQBP,pQAP を1リクエストで送信（実測確認済み）

設計制約:
  - ticker 単位で例外を握りつぶし、失敗 ticker は結果に含めない
    → SymbolStateEvaluator が全ルールをスキップするより、
      snapshot 自体を更新しない方が安全（stale 検出に委ねる）
  - BrokerAuthError も握りつぶすが WARNING レベルでログ記録する
  - get_market_data() の各フィールドが None の正常系（取引時間外等）は
    {"current_price": None, "best_bid": None, "best_ask": None} として含める
    → SymbolStateEvaluator は None を受けてそのルールをスキップするが、
      snapshot の updated_at はリセットされる（stale タイマー維持）
  - 並列呼び出しはしない（証券 API のレートリミット保護のため順次呼び出し）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trade_app.brokers.base import BrokerAdapter

logger = logging.getLogger(__name__)


class SymbolDataFetcher:
    """
    BrokerAdapter.get_market_data() を ticker 単位で呼び出し、
    SymbolStateEvaluator が受け取る symbol_data dict を構築する。

    Args:
        broker: BrokerAdapter インスタンス（main.py の lifespan で生成して注入すること）

    使用例:
        fetcher = SymbolDataFetcher(broker)
        symbol_data = await fetcher.fetch(["7203", "6758"])
        # → {"7203": {"current_price": 3400.0, "best_bid": 3390.0, "best_ask": 3410.0},
        #    "6758": {"current_price": 1500.0, "best_bid": None, "best_ask": None}}
        #   （失敗した ticker は含まれない）
    """

    def __init__(self, broker: "BrokerAdapter") -> None:
        self._broker = broker

    async def fetch(self, tickers: list[str]) -> dict[str, dict]:
        """
        ticker リストに対して get_market_data() を順次呼び出し、
        symbol_data dict を返す。

        Returns:
            ticker → {"current_price": float|None, "best_bid": float|None, "best_ask": float|None}
            の dict。API 例外が発生した ticker はキーごと除外される。

        Note:
            - 各フィールドが None の場合（取引時間外・データなし）もキーを含める（正常系）
            - 例外が発生した ticker は結果から除外される（snapshot 更新をスキップする）
        """
        if not tickers:
            return {}

        result: dict[str, dict] = {}

        for ticker in tickers:
            try:
                data = await self._broker.get_market_data(ticker)
                now = datetime.now(timezone.utc)
                result[ticker] = {
                    "current_price":  data.current_price,
                    "best_bid":       data.best_bid,
                    "best_ask":       data.best_ask,
                    "last_updated":   now,
                    # Phase Q: bid/ask 気配の観測時刻。
                    # market の公式 quote timestamp が取れない場合はシステム観測時刻を使う。
                    "bid_ask_updated": now,
                }
                logger.debug(
                    "SymbolDataFetcher: ticker=%s current_price=%s best_bid=%s best_ask=%s",
                    ticker,
                    data.current_price,
                    data.best_bid,
                    data.best_ask,
                )
            except Exception as exc:
                logger.warning(
                    "SymbolDataFetcher: ticker=%s 市場データ取得失敗 — この ticker はスキップ: %s",
                    ticker,
                    exc,
                )

        return result
