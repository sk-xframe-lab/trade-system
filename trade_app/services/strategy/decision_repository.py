"""
DecisionRepository — current_strategy_decisions テーブルの DB 操作層

責務:
  - upsert_decisions(): StrategyDecisionResult のリストを (strategy_id, ticker) 単位で UPSERT
  - get_latest_decisions(): 指定 ticker の現在 decision 一覧を取得
  - get_history(): strategy_evaluations から時系列履歴を取得

UPSERT 方針:
  ticker が None の場合、DB の UNIQUE 制約を nullable column に使えないため
  アプリケーション層で select → update-or-insert を行う（SQLite 互換）。

設計制約:
  発注しない。BrokerAdapter / OrderRouter / PositionManager には依存しない。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.current_strategy_decision import CurrentStrategyDecision
from trade_app.models.strategy_evaluation import StrategyEvaluation
from trade_app.services.strategy.schemas import StrategyDecisionResult

logger = logging.getLogger(__name__)


class DecisionRepository:
    """current_strategy_decisions の UPSERT / 取得を担当する。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ─── UPSERT ──────────────────────────────────────────────────────────

    async def upsert_decisions(
        self, results: list[StrategyDecisionResult]
    ) -> list[CurrentStrategyDecision]:
        """
        StrategyDecisionResult のリストを current_strategy_decisions に UPSERT する。

        UPSERT キー: (strategy_id, ticker)
        - ticker IS NULL の場合は ticker IS NULL の行を対象とする
        - ticker が指定されている場合は ticker == ticker の行を対象とする

        アプリケーションレベルの select-then-update-or-insert（SQLite 互換）。
        """
        now = datetime.now(timezone.utc)
        upserted: list[CurrentStrategyDecision] = []

        for result in results:
            existing = await self._find_existing(result.strategy_id, result.ticker)

            if existing is not None:
                # UPDATE
                existing.strategy_code = result.strategy_code
                existing.is_active = result.is_active
                existing.entry_allowed = result.entry_allowed
                existing.size_ratio = result.size_ratio
                existing.blocking_reasons_json = result.blocking_reasons
                existing.matched_required_states_json = result.matched_required_states
                existing.missing_required_states_json = result.missing_required_states
                existing.matched_forbidden_states_json = result.matched_forbidden_states
                existing.evidence_json = result.evidence
                existing.evaluation_time = result.evaluation_time
                existing.updated_at = now
                upserted.append(existing)
                logger.debug(
                    "DecisionRepository: updated strategy_code=%s ticker=%s entry_allowed=%s",
                    result.strategy_code, result.ticker, result.entry_allowed,
                )
            else:
                # INSERT
                row = CurrentStrategyDecision(
                    strategy_id=result.strategy_id,
                    strategy_code=result.strategy_code,
                    ticker=result.ticker,
                    is_active=result.is_active,
                    entry_allowed=result.entry_allowed,
                    size_ratio=result.size_ratio,
                    blocking_reasons_json=result.blocking_reasons,
                    matched_required_states_json=result.matched_required_states,
                    missing_required_states_json=result.missing_required_states,
                    matched_forbidden_states_json=result.matched_forbidden_states,
                    evidence_json=result.evidence,
                    evaluation_time=result.evaluation_time,
                    updated_at=now,
                )
                self._db.add(row)
                upserted.append(row)
                logger.debug(
                    "DecisionRepository: inserted strategy_code=%s ticker=%s entry_allowed=%s",
                    result.strategy_code, result.ticker, result.entry_allowed,
                )

        await self._db.flush()
        return upserted

    async def _find_existing(
        self, strategy_id: str, ticker: str | None
    ) -> CurrentStrategyDecision | None:
        """(strategy_id, ticker) で既存レコードを検索する。"""
        stmt = select(CurrentStrategyDecision).where(
            CurrentStrategyDecision.strategy_id == strategy_id
        )
        if ticker is None:
            stmt = stmt.where(CurrentStrategyDecision.ticker.is_(None))
        else:
            stmt = stmt.where(CurrentStrategyDecision.ticker == ticker)

        stmt = stmt.order_by(
            CurrentStrategyDecision.updated_at.desc(),
            CurrentStrategyDecision.id.desc(),
        ).limit(2)

        result = await self._db.execute(stmt)
        rows = list(result.scalars().all())
        if len(rows) >= 2:
            logger.warning(
                "DecisionRepository._find_existing: 少なくとも2件の重複行を検出 "
                "strategy_id=%s ticker=%s — updated_at・id 降順で最新行を使用",
                strategy_id, ticker,
            )
        return rows[0] if rows else None

    # ─── 取得 ─────────────────────────────────────────────────────────────

    async def get_latest_decisions(
        self, ticker: str | None = None
    ) -> list[CurrentStrategyDecision]:
        """
        current_strategy_decisions から最新 decision を取得する。

        Args:
            ticker: None の場合は ticker IS NULL（銘柄横断）の行を返す。
                    指定の場合はその ticker の行を返す。

        Returns:
            strategy_code ごとに 1 件の CurrentStrategyDecision
            updated_at DESC 順（最新優先）
        """
        stmt = (
            select(CurrentStrategyDecision)
            .order_by(CurrentStrategyDecision.updated_at.desc())
        )
        if ticker is None:
            stmt = stmt.where(CurrentStrategyDecision.ticker.is_(None))
        else:
            stmt = stmt.where(CurrentStrategyDecision.ticker == ticker)

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_history(
        self,
        ticker: str | None = None,
        strategy_code: str | None = None,
        limit: int = 100,
    ) -> list[StrategyEvaluation]:
        """
        strategy_evaluations から時系列履歴を取得する。

        Args:
            ticker: None の場合は ticker IS NULL の評価を返す。指定の場合はその ticker。
            strategy_code: 指定の場合はその strategy のみ絞り込む。
            limit: 取得件数上限（デフォルト 100）

        Returns:
            evaluation_time DESC 順の StrategyEvaluation リスト
        """
        from trade_app.models.strategy_definition import StrategyDefinition

        stmt = (
            select(StrategyEvaluation)
            .order_by(StrategyEvaluation.evaluation_time.desc())
            .limit(limit)
        )

        if ticker is None:
            stmt = stmt.where(StrategyEvaluation.ticker.is_(None))
        else:
            stmt = stmt.where(StrategyEvaluation.ticker == ticker)

        if strategy_code is not None:
            # strategy_code でサブクエリ絞り込み
            sub = select(StrategyDefinition.id).where(
                StrategyDefinition.strategy_code == strategy_code
            )
            stmt = stmt.where(StrategyEvaluation.strategy_id.in_(sub))

        result = await self._db.execute(stmt)
        return list(result.scalars().all())
