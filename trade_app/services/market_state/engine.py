"""
MarketStateEngine — 市場状態評価エンジン

評価器（Evaluator）を順に実行し、結果を DB に永続化する。
売買判断は行わない。状態コードと証拠を記録することのみが責務。

使用方法:
    engine = MarketStateEngine(db)
    ctx = EvaluationContext(evaluation_time=datetime.now(timezone.utc))
    await engine.run(ctx)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.services.market_state.evaluator_base import AbstractStateEvaluator
from trade_app.services.market_state.market_evaluator import MarketStateEvaluator
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult
from trade_app.services.market_state.symbol_evaluator import SymbolStateEvaluator
from trade_app.services.market_state.time_window_evaluator import TimeWindowStateEvaluator

logger = logging.getLogger(__name__)


class MarketStateEngine:
    """
    登録された Evaluator を実行し、結果を永続化する。

    Phase 1 デフォルト Evaluator:
      - TimeWindowStateEvaluator: 時間帯状態
      - MarketStateEvaluator:     市場トレンド状態
      - SymbolStateEvaluator:     銘柄状態（ctx.symbol_data が空の場合はスキップ）
    """

    def __init__(
        self,
        db: AsyncSession,
        evaluators: list[AbstractStateEvaluator] | None = None,
    ) -> None:
        self._db = db
        self._repo = MarketStateRepository(db)
        self._evaluators: list[AbstractStateEvaluator] = evaluators or [
            TimeWindowStateEvaluator(),
            MarketStateEvaluator(),
            SymbolStateEvaluator(),
        ]

    # ─── メイン実行 ────────────────────────────────────────────────────────────

    async def run(self, ctx: EvaluationContext) -> list[StateEvaluationResult]:
        """
        全 Evaluator を実行し、結果を DB に保存する。

        Args:
            ctx: 評価コンテキスト（evaluation_time と任意の market_data を含む）

        Returns:
            保存した StateEvaluationResult のリスト
        """
        evaluation_time = ctx.evaluation_time
        if evaluation_time.tzinfo is None:
            evaluation_time = evaluation_time.replace(tzinfo=timezone.utc)

        all_results: list[StateEvaluationResult] = []

        # ─── 各 Evaluator を実行 ───────────────────────────────────────────
        for evaluator in self._evaluators:
            try:
                results = evaluator.evaluate(ctx)
                all_results.extend(results)
                logger.debug(
                    "MarketStateEngine: evaluator=%s produced %d result(s)",
                    evaluator.name, len(results),
                )
            except Exception as exc:
                logger.error(
                    "MarketStateEngine: evaluator=%s raised %s — skipping",
                    evaluator.name, exc, exc_info=True,
                )

        if not all_results:
            logger.warning("MarketStateEngine: no evaluation results produced")
            return []

        # ─── 評価結果を DB に保存 ──────────────────────────────────────────
        await self._repo.save_evaluations(all_results, evaluation_time)

        # ─── スナップショットを更新 ────────────────────────────────────────
        await self._update_snapshots(all_results, evaluation_time)

        await self._db.commit()

        logger.info(
            "MarketStateEngine: run complete — %d result(s) saved at %s",
            len(all_results), evaluation_time.isoformat(),
        )
        return all_results

    # ─── スナップショット更新 ──────────────────────────────────────────────────

    async def _update_snapshots(
        self,
        results: list[StateEvaluationResult],
        evaluation_time: datetime,
    ) -> None:
        """
        結果を layer/target ごとにグループ化してスナップショットを UPSERT する。
        """
        # layer + target_type + target_code をキーにグループ化
        groups: dict[tuple[str, str, str | None], list[StateEvaluationResult]] = {}
        for r in results:
            key = (r.layer, r.target_type, r.target_code)
            groups.setdefault(key, []).append(r)

        for (layer, target_type, target_code), group in groups.items():
            active_states = [r.state_code for r in group]

            # 最初の結果をプライマリとしてサマリーを構築
            primary = group[0]
            summary = {
                "primary_state": primary.state_code,
                "score": primary.score,
                "confidence": primary.confidence,
                "evaluated_at": evaluation_time.isoformat(),
                "evaluator_count": len(group),
            }

            await self._repo.upsert_snapshot(
                layer=layer,
                target_type=target_type,
                target_code=target_code,
                active_state_codes=active_states,
                summary=summary,
            )
