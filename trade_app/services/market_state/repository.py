"""
MarketStateRepository — DB 操作層

評価器が返した StateEvaluationResult を state_evaluations テーブルに保存し、
current_state_snapshots テーブルを UPSERT する。

save_evaluations は (layer, target_type, target_code) ごとにグループ化してから
ソフト失効を実行する。これにより同一銘柄に複数の状態（gap_up + high_volume 等）が
存在する場合でも、古いレコードを正しく 1 回だけ失効させてから全件 INSERT できる。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.current_state_snapshot import CurrentStateSnapshot
from trade_app.models.state_evaluation import StateEvaluation
from trade_app.services.market_state.schemas import StateEvaluationResult

logger = logging.getLogger(__name__)


class MarketStateRepository:
    """Market State Engine の DB 操作を担当する。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ─── 評価結果保存 ──────────────────────────────────────────────────────────

    async def save_evaluations(
        self, results: list[StateEvaluationResult], evaluation_time: datetime
    ) -> list[StateEvaluation]:
        """
        StateEvaluationResult のリストを state_evaluations に INSERT する。

        同じ (layer, target_type, target_code) の既存アクティブ行を is_active=False に
        更新してから新しいレコードを挿入する（ソフト失効）。

        グループ化して処理することで、同一 target に複数状態（例: gap_up_open と
        high_relative_volume）がある場合も古いレコードを 1 回だけ失効させる。

        Returns:
            INSERT した StateEvaluation オブジェクトのリスト
        """
        if not results:
            return []

        # (layer, target_type, target_code) ごとにグループ化
        groups: dict[tuple[str, str, str | None], list[StateEvaluationResult]] = {}
        for r in results:
            key = (r.layer, r.target_type, r.target_code)
            groups.setdefault(key, []).append(r)

        saved: list[StateEvaluation] = []

        for (layer, target_type, target_code), group in groups.items():
            # 既存の is_active=True レコードをグループ単位で 1 回だけ失効させる
            await self._db.execute(
                update(StateEvaluation)
                .where(
                    StateEvaluation.layer == layer,
                    StateEvaluation.target_type == target_type,
                    StateEvaluation.target_code == target_code,
                    StateEvaluation.is_active.is_(True),
                )
                .values(is_active=False)
            )

            # グループ内の全結果を INSERT
            for result in group:
                row = StateEvaluation(
                    layer=result.layer,
                    target_type=result.target_type,
                    target_code=result.target_code,
                    evaluation_time=evaluation_time,
                    state_code=result.state_code,
                    score=result.score,
                    confidence=result.confidence,
                    is_active=True,
                    evidence_json=result.evidence,
                    expires_at=result.expires_at,
                )
                self._db.add(row)
                saved.append(row)

        await self._db.flush()
        logger.debug("MarketStateRepository: saved %d evaluation(s)", len(saved))
        return saved

    # ─── スナップショット UPSERT ───────────────────────────────────────────────

    async def upsert_snapshot(
        self,
        layer: str,
        target_type: str,
        target_code: str | None,
        active_state_codes: list[str],
        summary: dict[str, Any],
    ) -> CurrentStateSnapshot:
        """
        current_state_snapshots を UPSERT する。
        既存行があれば更新、なければ INSERT。

        Args:
            layer: "market" | "symbol" | "time_window"
            target_type: "market" | "time_window" | "symbol" など
            target_code: 銘柄コード等。グローバルな場合は None
            active_state_codes: アクティブな state_code リスト
            summary: state_summary_json に格納するサマリー辞書
        """
        stmt = select(CurrentStateSnapshot).where(
            CurrentStateSnapshot.layer == layer,
            CurrentStateSnapshot.target_type == target_type,
            CurrentStateSnapshot.target_code == target_code,
        )
        result = await self._db.execute(stmt)
        snapshot = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)

        if snapshot is None:
            snapshot = CurrentStateSnapshot(
                layer=layer,
                target_type=target_type,
                target_code=target_code,
                active_states_json=active_state_codes,
                state_summary_json=summary,
                updated_at=now,
            )
            self._db.add(snapshot)
        else:
            snapshot.active_states_json = active_state_codes
            snapshot.state_summary_json = summary
            snapshot.updated_at = now

        await self._db.flush()
        logger.debug(
            "MarketStateRepository: upserted snapshot layer=%s target=%s states=%s",
            layer, target_code, active_state_codes,
        )
        return snapshot

    # ─── クエリ ────────────────────────────────────────────────────────────────

    async def get_current_states(
        self, layers: list[str] | None = None
    ) -> list[CurrentStateSnapshot]:
        """
        現在の状態スナップショットを返す。

        Args:
            layers: フィルタする layer のリスト。None の場合は全件返す。
        """
        stmt = select(CurrentStateSnapshot).order_by(
            CurrentStateSnapshot.layer, CurrentStateSnapshot.target_type
        )
        if layers:
            stmt = stmt.where(CurrentStateSnapshot.layer.in_(layers))

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_evaluation_history(
        self,
        layer: str | None = None,
        target_type: str | None = None,
        target_code: str | None = None,
        limit: int = 50,
    ) -> list[StateEvaluation]:
        """
        state_evaluations テーブルから評価履歴を返す（新しい順）。

        Args:
            layer: フィルタ条件
            target_type: フィルタ条件
            target_code: フィルタ条件
            limit: 最大取得件数（デフォルト 50）
        """
        stmt = (
            select(StateEvaluation)
            .order_by(StateEvaluation.evaluation_time.desc())
            .limit(limit)
        )
        if layer is not None:
            stmt = stmt.where(StateEvaluation.layer == layer)
        if target_type is not None:
            stmt = stmt.where(StateEvaluation.target_type == target_type)
        if target_code is not None:
            stmt = stmt.where(StateEvaluation.target_code == target_code)

        result = await self._db.execute(stmt)
        return list(result.scalars().all())

    async def get_symbol_snapshot(
        self, ticker: str
    ) -> CurrentStateSnapshot | None:
        """
        指定 ticker の現在状態スナップショットを返す。

        Args:
            ticker: 銘柄コード（例: "7203"）
        Returns:
            CurrentStateSnapshot または None（データなし）
        """
        stmt = select(CurrentStateSnapshot).where(
            CurrentStateSnapshot.layer == "symbol",
            CurrentStateSnapshot.target_type == "symbol",
            CurrentStateSnapshot.target_code == ticker,
        )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_symbol_active_evaluations(
        self, ticker: str
    ) -> list[StateEvaluation]:
        """
        指定 ticker の現在アクティブな評価ログを返す（is_active=True のみ）。

        Args:
            ticker: 銘柄コード
        Returns:
            StateEvaluation のリスト（evaluation_time DESC 順）
        """
        stmt = (
            select(StateEvaluation)
            .where(
                StateEvaluation.layer == "symbol",
                StateEvaluation.target_type == "symbol",
                StateEvaluation.target_code == ticker,
                StateEvaluation.is_active.is_(True),
            )
            .order_by(StateEvaluation.evaluation_time.desc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars().all())
