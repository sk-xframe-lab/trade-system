"""
MarketStateRunner — MarketStateEngine 定期実行ジョブ

実行モデル:
  main.py の lifespan 内で asyncio.create_task() により
  バックグラウンドタスクとして起動する。

実行周期:
  MARKET_STATE_INTERVAL_SEC（デフォルト 60 秒）
  ExitWatcher(10秒)・OrderPoller(5秒)とは独立した周期で動作する。

銘柄データ:
  WATCHED_SYMBOLS（カンマ区切り）に登録された銘柄を評価対象とする。
  Phase 2 以降は SymbolDataFetcher 経由でブローカー API から価格データを取得する。
  symbol_fetcher=None の場合は Phase 1 動作（symbol_data 空）にフォールバックする。

失敗分離方針:
  - 1 evaluator の失敗は engine 層で握りつぶしループ継続（MarketStateEngine 設計）
  - _run_once 全体の失敗は _run_loop で握りつぶしループ継続
  - WATCHED_SYMBOLS が空でも例外にしない（time_window / market 評価のみ継続）
  - WATCHED_SYMBOLS が空の場合はログを最小化する（初回のみ info）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import zoneinfo

from trade_app.config import get_settings
from trade_app.models.database import AsyncSessionLocal
from trade_app.services.market_state.daily_metrics import DailyMetricsComputer, DailyMetricsRepository
from trade_app.services.market_state.engine import MarketStateEngine
from trade_app.services.market_state.schemas import EvaluationContext
from trade_app.services.market_state.symbol_data_fetcher import SymbolDataFetcher

_JST = zoneinfo.ZoneInfo("Asia/Tokyo")

logger = logging.getLogger(__name__)


class MarketStateRunner:
    """
    MarketStateEngine を定期実行するバックグラウンドタスク。

    失敗分離:
      - _run_once の例外は _run_loop でキャッチしてループ継続（次の周期で再試行）
      - engine.run() 内部では evaluator 単位で例外を握りつぶす（MarketStateEngine 設計）
      - WATCHED_SYMBOLS が空でも market / time_window 評価は継続される

    ログ方針:
      - WATCHED_SYMBOLS が空の場合は初回のみ info を出力し、以降は debug レベルに落とす
      - 毎周期の通常ログは debug レベル
      - エラーは error レベル
    """

    def __init__(self, symbol_fetcher: Optional[SymbolDataFetcher] = None) -> None:
        self._task: asyncio.Task | None = None
        self._running: bool = False
        self._warned_empty_symbols: bool = False  # 空シンボル警告を1度だけ出すフラグ
        self._symbol_fetcher = symbol_fetcher

    def start(self) -> None:
        """バックグラウンドタスクを起動する。"""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        settings = get_settings()
        logger.info(
            "MarketStateRunner: 起動 (interval=%ds, watched_symbols=%r)",
            settings.MARKET_STATE_INTERVAL_SEC,
            settings.WATCHED_SYMBOLS or "(none)",
        )

    async def stop(self) -> None:
        """バックグラウンドタスクを停止する。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MarketStateRunner: 停止")

    async def _run_loop(self) -> None:
        settings = get_settings()
        while self._running:
            try:
                await self._run_once()
            except Exception as exc:
                logger.error(
                    "MarketStateRunner: 評価サイクルエラー: %s — 次の周期で再試行",
                    exc, exc_info=True,
                )
            await asyncio.sleep(settings.MARKET_STATE_INTERVAL_SEC)

    async def _run_once(self) -> None:
        """
        1サイクルの評価を実行する。

        WATCHED_SYMBOLS が空の場合:
          - symbol_data は {} のまま
          - SymbolStateEvaluator は [] を返すのみでエラーにならない
          - time_window / market 評価は継続される
          - 初回のみ info ログを出力。以降はサイレント。
        """
        settings = get_settings()
        now = datetime.now(timezone.utc)

        # WATCHED_SYMBOLS から監視銘柄リストを生成
        watched = [
            s.strip()
            for s in settings.WATCHED_SYMBOLS.split(",")
            if s.strip()
        ]

        # 空シンボル時のログ（初回のみ）
        if not watched and not self._warned_empty_symbols:
            logger.info(
                "MarketStateRunner: WATCHED_SYMBOLS が未設定のため銘柄評価をスキップ。"
                "time_window / market 評価は継続する。"
            )
            self._warned_empty_symbols = True

        # Phase 2: SymbolDataFetcher 経由で current_price を取得
        # symbol_fetcher=None の場合は Phase 1 動作（symbol_data 空）にフォールバック
        symbol_data: dict = {}
        if watched:
            if self._symbol_fetcher is not None:
                symbol_data = await self._symbol_fetcher.fetch(watched)
                logger.debug(
                    "MarketStateRunner: symbol_data 取得完了 — %d/%d tickers",
                    len(symbol_data),
                    len(watched),
                )
            else:
                logger.debug(
                    "MarketStateRunner: watched_symbols=%s (symbol_fetcher 未設定: Phase 1 動作)",
                    watched,
                )

        # Phase AT: 日次メトリクス注入 + エンジン実行（同一 DB セッション）
        async with AsyncSessionLocal() as db:
            if symbol_data:
                today_jst = datetime.now(_JST).date()
                daily_repo = DailyMetricsRepository(db)
                for ticker in list(symbol_data.keys()):
                    rows = await daily_repo.get_recent_rows(ticker, n=21)
                    metrics = DailyMetricsComputer.compute(rows, today_jst)
                    symbol_data[ticker].update(metrics)
                    logger.debug(
                        "MarketStateRunner: daily metrics ticker=%s ma5=%s ma20=%s atr=%s rsi=%s",
                        ticker,
                        metrics.get("ma5"),
                        metrics.get("ma20"),
                        metrics.get("atr"),
                        metrics.get("rsi"),
                    )

            ctx = EvaluationContext(
                evaluation_time=now,
                symbol_data=symbol_data,
            )
            engine = MarketStateEngine(db)
            results = await engine.run(ctx)

        logger.debug(
            "MarketStateRunner: サイクル完了 — %d result(s) at %s",
            len(results), now.isoformat(),
        )
