"""
GET  /api/v1/strategies/current            — 全 strategy の最新判定結果（銘柄横断）
GET  /api/v1/strategies/symbols/{ticker}   — ticker 単位の strategy 判定結果
POST /api/v1/strategies/recalculate        — 管理用: 現在の state snapshot で再評価
GET  /api/v1/strategies/latest             — current_strategy_decisions から最新（銘柄横断）
GET  /api/v1/strategies/latest/{ticker}    — current_strategy_decisions から最新（ticker 別）
GET  /api/v1/strategies/history            — strategy_evaluations から時系列履歴

設計制約:
  このルートは StrategyEngine / DecisionRepository を呼び出すのみ。
  発注・ポジション更新・BrokerAdapter 呼び出しは一切行わない。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.config import get_settings
from trade_app.models.database import get_db
from trade_app.models.strategy_definition import StrategyDefinition
from trade_app.services.strategy.decision_repository import DecisionRepository
from trade_app.services.strategy.engine import StrategyEngine
from trade_app.services.strategy.repository import StrategyRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])
settings = get_settings()


def _verify_auth(authorization: str) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="認証エラー")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="無効なトークン")


# ─── レスポンススキーマ ────────────────────────────────────────────────────────

class StrategyDecisionItem(BaseModel):
    """strategy 判定結果の API レスポンス 1 件分"""
    strategy_id: str
    strategy_code: str
    strategy_name: str
    ticker: str | None
    is_active: bool
    entry_allowed: bool
    size_ratio: float
    blocking_reasons: list[str]
    matched_required_states: list[str]
    matched_forbidden_states: list[str]
    missing_required_states: list[str]
    evaluation_time: datetime
    evidence: dict[str, Any]

    model_config = {"from_attributes": True}


class RecalculateRequest(BaseModel):
    """POST /recalculate リクエストボディ"""
    ticker: str | None = None


class RecalculateResponse(BaseModel):
    evaluated: int
    results: list[StrategyDecisionItem]


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

async def _evaluation_to_item(
    row,  # StrategyEvaluation ORM object
    strategy_map: dict[str, StrategyDefinition],
) -> StrategyDecisionItem:
    """StrategyEvaluation ORM → StrategyDecisionItem"""
    strategy = strategy_map.get(row.strategy_id)
    return StrategyDecisionItem(
        strategy_id=row.strategy_id,
        strategy_code=strategy.strategy_code if strategy else row.strategy_id,
        strategy_name=strategy.strategy_name if strategy else "",
        ticker=row.ticker,
        is_active=row.is_active,
        entry_allowed=row.entry_allowed,
        size_ratio=row.size_ratio,
        blocking_reasons=row.blocking_reasons_json or [],
        matched_required_states=row.matched_required_states_json or [],
        matched_forbidden_states=row.matched_forbidden_states_json or [],
        missing_required_states=row.missing_required_states_json or [],
        evaluation_time=row.evaluation_time,
        evidence=row.evidence_json or {},
    )


def _decision_to_item(
    row,  # CurrentStrategyDecision ORM object
    strategy_map: dict[str, StrategyDefinition],
) -> StrategyDecisionItem:
    """CurrentStrategyDecision ORM → StrategyDecisionItem"""
    strategy = strategy_map.get(row.strategy_id)
    return StrategyDecisionItem(
        strategy_id=row.strategy_id,
        strategy_code=row.strategy_code,
        strategy_name=strategy.strategy_name if strategy else "",
        ticker=row.ticker,
        is_active=row.is_active,
        entry_allowed=row.entry_allowed,
        size_ratio=row.size_ratio,
        blocking_reasons=row.blocking_reasons_json or [],
        matched_required_states=row.matched_required_states_json or [],
        matched_forbidden_states=row.matched_forbidden_states_json or [],
        missing_required_states=row.missing_required_states_json or [],
        evaluation_time=row.evaluation_time,
        evidence=row.evidence_json or {},
    )


# ─── エンドポイント ────────────────────────────────────────────────────────────

@router.get(
    "/current",
    response_model=list[StrategyDecisionItem],
    summary="全 strategy の最新判定結果（銘柄横断）— strategy_evaluations から取得",
)
async def get_current_strategies(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
) -> list[StrategyDecisionItem]:
    """
    銘柄横断評価（ticker=None）の最新 strategy 判定結果を返す。
    strategy_evaluations テーブルから strategy_id ごとに最新の 1 件を返す。
    データがない場合は空リスト。
    """
    _verify_auth(authorization)

    repo = StrategyRepository(db)
    rows = await repo.get_latest_evaluations(ticker=None)
    strategies = await repo.get_all_strategies(enabled_only=False)
    strategy_map = {s.id: s for s in strategies}

    return [await _evaluation_to_item(r, strategy_map) for r in rows]


@router.get(
    "/symbols/{ticker}",
    response_model=list[StrategyDecisionItem],
    summary="ticker 単位の strategy 判定結果 — strategy_evaluations から取得",
)
async def get_symbol_strategies(
    ticker: str,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
) -> list[StrategyDecisionItem]:
    """
    指定 ticker の最新 strategy 判定結果を返す。
    strategy_evaluations テーブルから strategy_id ごとに最新の 1 件を返す。
    データがない場合は空リスト（404 ではなく []）。
    """
    _verify_auth(authorization)

    repo = StrategyRepository(db)
    rows = await repo.get_latest_evaluations(ticker=ticker)
    strategies = await repo.get_all_strategies(enabled_only=False)
    strategy_map = {s.id: s for s in strategies}

    return [await _evaluation_to_item(r, strategy_map) for r in rows]


@router.get(
    "/latest",
    response_model=list[StrategyDecisionItem],
    summary="全 strategy の現在 decision 正本（銘柄横断）— current_strategy_decisions から取得",
)
async def get_latest_decisions(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
) -> list[StrategyDecisionItem]:
    """
    current_strategy_decisions から銘柄横断（ticker=None）の最新 decision を返す。

    strategy_evaluations（APPEND ONLY 時系列）ではなく current_strategy_decisions（正本）を参照。
    StrategyRunner が評価サイクルごとに UPSERT するため常に最新の判定結果が取得できる。
    データがない場合は空リスト。
    """
    _verify_auth(authorization)

    decision_repo = DecisionRepository(db)
    rows = await decision_repo.get_latest_decisions(ticker=None)

    strategy_repo = StrategyRepository(db)
    strategies = await strategy_repo.get_all_strategies(enabled_only=False)
    strategy_map = {s.id: s for s in strategies}

    return [_decision_to_item(r, strategy_map) for r in rows]


@router.get(
    "/latest/{ticker}",
    response_model=list[StrategyDecisionItem],
    summary="ticker 単位の現在 decision 正本 — current_strategy_decisions から取得",
)
async def get_latest_decisions_for_ticker(
    ticker: str,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
) -> list[StrategyDecisionItem]:
    """
    current_strategy_decisions から指定 ticker の最新 decision を返す。

    StrategyRunner が評価サイクルごとに UPSERT するため常に最新の判定結果が取得できる。
    データがない場合は空リスト（404 ではなく []）。
    """
    _verify_auth(authorization)

    decision_repo = DecisionRepository(db)
    rows = await decision_repo.get_latest_decisions(ticker=ticker)

    strategy_repo = StrategyRepository(db)
    strategies = await strategy_repo.get_all_strategies(enabled_only=False)
    strategy_map = {s.id: s for s in strategies}

    return [_decision_to_item(r, strategy_map) for r in rows]


@router.get(
    "/history",
    response_model=list[StrategyDecisionItem],
    summary="strategy 判定の時系列履歴 — strategy_evaluations から取得",
)
async def get_strategy_history(
    ticker: str | None = Query(default=None, description="銘柄コード。未指定=銘柄横断（ticker IS NULL）"),
    strategy_code: str | None = Query(default=None, description="strategy コード。未指定=全 strategy"),
    limit: int = Query(default=100, ge=1, le=1000, description="取得件数上限"),
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
) -> list[StrategyDecisionItem]:
    """
    strategy_evaluations から時系列履歴を返す。

    - ticker: 未指定の場合は ticker IS NULL（銘柄横断）の評価を返す
    - strategy_code: 指定の場合はその strategy のみ絞り込む
    - limit: 最大取得件数（デフォルト 100、最大 1000）
    - evaluation_time DESC 順
    """
    _verify_auth(authorization)

    decision_repo = DecisionRepository(db)
    rows = await decision_repo.get_history(
        ticker=ticker,
        strategy_code=strategy_code,
        limit=limit,
    )

    strategy_repo = StrategyRepository(db)
    strategies = await strategy_repo.get_all_strategies(enabled_only=False)
    strategy_map = {s.id: s for s in strategies}

    return [await _evaluation_to_item(r, strategy_map) for r in rows]


@router.post(
    "/recalculate",
    response_model=RecalculateResponse,
    summary="管理用: 現在の state snapshot を使って strategy を再評価",
)
async def recalculate_strategies(
    body: RecalculateRequest | None = None,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
) -> RecalculateResponse:
    """
    現在の state snapshot を使って全 strategy を再評価し、結果を保存する。

    - body.ticker を指定した場合はその銘柄を含めて評価
    - ticker 未指定の場合は market + time_window のみで評価
    - 結果は strategy_evaluations + current_strategy_decisions テーブルに保存される

    ⚠️ 発注は行わない。判定結果の返却のみ。
    """
    _verify_auth(authorization)

    ticker = body.ticker if body else None
    engine = StrategyEngine(db)
    decision_results = await engine.run(ticker=ticker)

    repo = StrategyRepository(db)
    strategies = await repo.get_all_strategies(enabled_only=False)
    strategy_map = {s.id: s for s in strategies}

    items = [
        StrategyDecisionItem(
            strategy_id=r.strategy_id,
            strategy_code=r.strategy_code,
            strategy_name=r.strategy_name,
            ticker=r.ticker,
            is_active=r.is_active,
            entry_allowed=r.entry_allowed,
            size_ratio=r.size_ratio,
            blocking_reasons=r.blocking_reasons,
            matched_required_states=r.matched_required_states,
            matched_forbidden_states=r.matched_forbidden_states,
            missing_required_states=r.missing_required_states,
            evaluation_time=r.evaluation_time,
            evidence=r.evidence,
        )
        for r in decision_results
    ]

    return RecalculateResponse(evaluated=len(items), results=items)
