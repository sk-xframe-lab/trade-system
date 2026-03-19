"""
StrategyRunner — StrategyEngine 定期実行ジョブ

実行モデル:
  main.py の lifespan 内で asyncio.create_task() により
  バックグラウンドタスクとして起動する。

実行周期:
  STRATEGY_RUNNER_INTERVAL_SEC（デフォルト 60 秒）

銘柄評価:
  WATCHED_SYMBOLS（カンマ区切り）に登録された銘柄を ticker 単位で評価する。
  空文字の場合は global（ticker=None）評価のみ。

失敗分離方針:
  - global 評価の失敗は per-ticker 評価を止めない
  - 1 ticker の失敗は他 ticker 評価を止めない
  - 各評価は独立した DB セッションを使用する
  - _run_once 全体の失敗は _run_loop で握りつぶしループ継続
  - WATCHED_SYMBOLS が空でも例外にしない（global 評価のみ継続）

テスト容易性:
  session_factory を注入可能にして AsyncSessionLocal に依存しない設計。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from trade_app.config import get_settings

logger = logging.getLogger(__name__)


class StrategyRunner:
    """
    StrategyEngine を定期実行するバックグラウンドタスク。

    失敗分離:
      - global 評価 (ticker=None) と各 ticker 評価は独立した try/except ブロック
      - 各評価は独立した DB セッションを使用（セッション汚染を防ぐ）
      - _run_once の例外は _run_loop でキャッチしてループ継続

    注入可能な session_factory（テスト用）:
      StrategyRunner(session_factory=my_factory) で差し替え可能。
      None の場合は AsyncSessionLocal を使用する。
    """

    def __init__(self, session_factory: Callable | None = None) -> None:
        self._session_factory = session_factory
        self._task: asyncio.Task | None = None
        self._running: bool = False
        self._warned_empty_symbols: bool = False

    def _get_session_factory(self):
        """AsyncSessionLocal をデフォルトとして返す（遅延 import でテスト容易性確保）"""
        if self._session_factory is not None:
            return self._session_factory
        from trade_app.models.database import AsyncSessionLocal
        return AsyncSessionLocal

    def start(self) -> None:
        """バックグラウンドタスクを起動する。"""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        settings = get_settings()
        logger.info(
            "StrategyRunner: 起動 (interval=%ds, watched_symbols=%r)",
            settings.STRATEGY_RUNNER_INTERVAL_SEC,
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
        logger.info("StrategyRunner: 停止")

    async def _run_loop(self) -> None:
        settings = get_settings()
        while self._running:
            try:
                await self._run_once()
            except Exception as exc:
                logger.error(
                    "StrategyRunner: 評価サイクルエラー: %s — 次の周期で再試行",
                    exc, exc_info=True,
                )
            await asyncio.sleep(settings.STRATEGY_RUNNER_INTERVAL_SEC)

    async def _run_once(self) -> None:
        """
        1サイクルの評価を実行する。

        global（ticker=None）と各 ticker を独立した DB セッションで評価する。
        いずれかの失敗は他の評価に影響しない（失敗分離）。
        """
        from trade_app.services.strategy.engine import StrategyEngine

        settings = get_settings()

        watched = [
            s.strip()
            for s in settings.WATCHED_SYMBOLS.split(",")
            if s.strip()
        ]

        if not watched and not self._warned_empty_symbols:
            logger.info(
                "StrategyRunner: WATCHED_SYMBOLS が未設定のため銘柄評価をスキップ。"
                "global（ticker=None）評価のみ継続する。"
            )
            self._warned_empty_symbols = True

        session_factory = self._get_session_factory()
        total_results = 0

        # ─── global 評価（ticker=None） ────────────────────────────────────
        try:
            async with session_factory() as db:
                results = await StrategyEngine(db).run(ticker=None)
                total_results += len(results)
        except Exception as exc:
            logger.error(
                "StrategyRunner: global 評価エラー: %s",
                exc, exc_info=True,
            )

        # ─── ticker 別評価 ─────────────────────────────────────────────────
        for ticker in watched:
            try:
                async with session_factory() as db:
                    results = await StrategyEngine(db).run(ticker=ticker)
                    total_results += len(results)
            except Exception as exc:
                logger.error(
                    "StrategyRunner: ticker=%s 評価エラー: %s",
                    ticker, exc, exc_info=True,
                )

        logger.debug(
            "StrategyRunner: サイクル完了 — %d result(s), watched=%s",
            total_results, watched or "(global only)",
        )
