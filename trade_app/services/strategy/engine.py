"""
StrategyEngine — Strategy 判定エンジン

責務:
  1. current_state_snapshots から現在の state を取得
  2. snapshot missing / stale の安全側チェック
  3. strategy ごとに StrategyEvaluator を呼び出す
  4. 結果を strategy_evaluations テーブルに保存
  5. StrategyDecisionResult のリストを返す

⚠️ 設計制約（必須・将来にわたって維持すること）:
  - 発注しない
  - BrokerAdapter を呼ばない
  - Position を直接更新しない
  - RiskManager をバイパスしない
  - Signal を直接消費して注文を作成しない
  - 返すものは StrategyDecisionResult のみ

state snapshot 安全側設計:
  - market snapshot 未存在       → entry_allowed=False, "state_snapshot_missing:market"
  - time_window snapshot 未存在  → entry_allowed=False, "state_snapshot_missing:time_window"
  - symbol snapshot 未存在       → entry_allowed=False, "state_snapshot_missing:symbol"
    （ticker 評価時のみ）
  - snapshot が stale（STRATEGY_MAX_STATE_AGE_SEC 超過）
                                 → entry_allowed=False, "state_snapshot_stale:{layer}"
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.config import get_settings
from trade_app.models.current_state_snapshot import CurrentStateSnapshot
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.strategy.decision_repository import DecisionRepository
from trade_app.services.strategy.evaluator import StrategyEvaluator
from trade_app.services.strategy.repository import StrategyRepository
from trade_app.services.strategy.schemas import StrategyDecisionResult

logger = logging.getLogger(__name__)


class StrategyEngine:
    """
    Market State Snapshot を参照して全 Strategy の判定を実行する。

    依存関係:
      - MarketStateRepository (state snapshot 読み取り専用)
      - StrategyRepository    (strategy 定義・評価ログ)
      - StrategyEvaluator     (純粋な判定ロジック)

    NG 依存（含めてはならない）:
      - OrderRouter, PositionManager, BrokerAdapter, RiskManager
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._strategy_repo = StrategyRepository(db)
        self._state_repo = MarketStateRepository(db)
        self._decision_repo = DecisionRepository(db)
        self._evaluator = StrategyEvaluator()

    # ─── メイン実行 ────────────────────────────────────────────────────────

    async def run(
        self,
        ticker: str | None = None,
        evaluation_time: datetime | None = None,
    ) -> list[StrategyDecisionResult]:
        """
        全 strategy を現在の state snapshot を使って評価する。

        Args:
            ticker: 銘柄コード。None の場合は market + time_window のみで評価。
                    指定の場合は market + time_window + symbol (ticker) で評価。
            evaluation_time: 評価基準時刻。None の場合は現在時刻 (UTC) を使用。
                    ⚠️ stale 判定はこの時刻を基準に行う（API 呼び出し時刻ではなく engine 実行時刻）。
                    snapshot.updated_at がこの時刻より STRATEGY_MAX_STATE_AGE_SEC 秒以上古い場合 stale 扱い。

        Returns:
            StrategyDecisionResult のリスト（strategy ごとに 1 件）

        ticker=None 時の symbol 条件挙動:
            active_states_by_layer に "symbol" キーが存在しないため、
            strategy に layer="symbol" の required_state 条件がある場合は
            常に missing_required_state として記録され entry_allowed=False になる。
            これは設計上の仕様（銘柄横断評価では symbol 状態を参照しない）。
            ticker 別評価は run(ticker="7203") で行うこと。
        """
        now = evaluation_time or datetime.now(timezone.utc)
        settings = get_settings()
        max_age_sec = settings.STRATEGY_MAX_STATE_AGE_SEC

        # ─── state snapshots 取得 ──────────────────────────────────────
        snapshots = await self._state_repo.get_current_states()
        snapshot_map: dict[tuple[str, str | None], CurrentStateSnapshot] = {
            (s.layer, s.target_code): s for s in snapshots
        }

        # ─── safety check: missing / stale ────────────────────────────
        safety_reasons: list[str] = []
        active_states_by_layer: dict[str, list[str]] = {}

        def _check_layer(layer: str, target_code: str | None) -> None:
            snap = snapshot_map.get((layer, target_code))
            if snap is None:
                safety_reasons.append(f"state_snapshot_missing:{layer}")
                return
            snap_updated = snap.updated_at
            if snap_updated.tzinfo is None:
                snap_updated = snap_updated.replace(tzinfo=timezone.utc)
            age_sec = (now - snap_updated).total_seconds()
            if age_sec > max_age_sec:
                safety_reasons.append(f"state_snapshot_stale:{layer}")
                return
            active_states_by_layer[layer] = list(snap.active_states_json or [])

        _check_layer("market", None)
        _check_layer("time_window", None)
        if ticker:
            _check_layer("symbol", ticker)

        # ─── strategies を評価 ─────────────────────────────────────────
        strategies = await self._strategy_repo.get_all_strategies(enabled_only=False)
        results: list[StrategyDecisionResult] = []

        for strategy in strategies:
            conditions = await self._strategy_repo.get_conditions_for_strategy(
                strategy.id
            )
            result = self._evaluator.evaluate(
                strategy=strategy,
                conditions=conditions,
                active_states_by_layer=active_states_by_layer,
                ticker=ticker,
                evaluation_time=now,
                pre_blocking_reasons=list(safety_reasons),
            )

            try:
                await self._strategy_repo.save_evaluation(result)
            except Exception as exc:
                logger.error(
                    "StrategyEngine: save_evaluation failed strategy=%s: %s",
                    strategy.strategy_code, exc, exc_info=True,
                )

            results.append(result)

        # ─── current_strategy_decisions に UPSERT ─────────────────────────
        try:
            await self._decision_repo.upsert_decisions(results)
        except Exception as exc:
            logger.error(
                "StrategyEngine: upsert_decisions failed for ticker=%s: %s",
                ticker, exc, exc_info=True,
            )

        await self._db.commit()

        logger.info(
            "StrategyEngine: run complete — %d strategy(ies) evaluated for ticker=%s at %s",
            len(results), ticker, now.isoformat(),
        )
        return results
