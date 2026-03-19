"""
StrategyRepository — Strategy Engine の DB 操作層

責務:
  - strategy 定義・条件の取得
  - strategy 判定ログ（strategy_evaluations）の保存・取得

設計制約:
  OrderRouter / PositionManager / BrokerAdapter には一切依存しない。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.strategy_condition import StrategyCondition
from trade_app.models.strategy_definition import StrategyDefinition
from trade_app.models.strategy_evaluation import StrategyEvaluation
from trade_app.services.strategy.schemas import StrategyDecisionResult

logger = logging.getLogger(__name__)


class StrategyRepository:
    """Strategy Engine の DB 操作を担当する。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ─── 定義取得 ──────────────────────────────────────────────────────────

    async def get_all_strategies(
        self, enabled_only: bool = True
    ) -> list[StrategyDefinition]:
        """
        strategy 定義を全件取得する（priority DESC 順）。

        Args:
            enabled_only: True の場合は is_enabled=True のみ返す
        """
        stmt = select(StrategyDefinition).order_by(
            StrategyDefinition.priority.desc(),
            StrategyDefinition.strategy_code,
        )
        if enabled_only:
            stmt = stmt.where(StrategyDefinition.is_enabled.is_(True))
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_conditions_for_strategy(
        self, strategy_id: str
    ) -> list[StrategyCondition]:
        """指定 strategy の条件リストを取得する。"""
        stmt = select(StrategyCondition).where(
            StrategyCondition.strategy_id == strategy_id
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    # ─── 判定ログ保存 ─────────────────────────────────────────────────────

    async def save_evaluation(
        self, result: StrategyDecisionResult
    ) -> StrategyEvaluation:
        """StrategyDecisionResult を strategy_evaluations テーブルに INSERT する。"""
        row = StrategyEvaluation(
            strategy_id=result.strategy_id,
            ticker=result.ticker,
            evaluation_time=result.evaluation_time,
            is_active=result.is_active,
            entry_allowed=result.entry_allowed,
            size_ratio=result.size_ratio,
            matched_required_states_json=result.matched_required_states,
            matched_forbidden_states_json=result.matched_forbidden_states,
            missing_required_states_json=result.missing_required_states,
            blocking_reasons_json=result.blocking_reasons,
            evidence_json=result.evidence,
        )
        self._db.add(row)
        await self._db.flush()
        logger.debug(
            "StrategyRepository: saved evaluation strategy_id=%s ticker=%s entry_allowed=%s",
            result.strategy_id, result.ticker, result.entry_allowed,
        )
        return row

    # ─── 判定ログ取得 ─────────────────────────────────────────────────────

    async def get_latest_evaluations(
        self, ticker: str | None = None
    ) -> list[StrategyEvaluation]:
        """
        strategy ごとの最新 evaluation を返す。

        Args:
            ticker: None の場合は ticker IS NULL（銘柄横断）評価のみ返す。
                    指定の場合はその ticker の評価のみ返す。

        Returns:
            strategy_id ごとに最新の StrategyEvaluation（重複なし）
        """
        stmt = (
            select(StrategyEvaluation)
            .order_by(StrategyEvaluation.evaluation_time.desc())
            .limit(500)
        )
        if ticker is not None:
            stmt = stmt.where(StrategyEvaluation.ticker == ticker)
        else:
            stmt = stmt.where(StrategyEvaluation.ticker.is_(None))

        result = await self._db.execute(stmt)
        rows = list(result.scalars().all())

        # strategy_id ごとに最新の 1 件のみ返す（Python 側で dedup）
        seen: set[str] = set()
        unique: list[StrategyEvaluation] = []
        for row in rows:
            if row.strategy_id not in seen:
                seen.add(row.strategy_id)
                unique.append(row)
        return unique
