"""
GET /api/v1/market-state/current          — 現在の市場状態スナップショット
GET /api/v1/market-state/history          — 評価履歴
GET /api/v1/market-state/symbols/{ticker} — 銘柄の現在状態
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
from trade_app.services.market_state.repository import MarketStateRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/market-state", tags=["market-state"])
settings = get_settings()


def _verify_auth(authorization: str) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="認証エラー")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="無効なトークン")


# ─── レスポンススキーマ ────────────────────────────────────────────────────────

class CurrentStateItem(BaseModel):
    layer: str
    target_type: str
    target_code: str | None
    active_states: list[str]
    summary: dict[str, Any]
    updated_at: datetime

    model_config = {"from_attributes": True}


class EvaluationHistoryItem(BaseModel):
    id: str
    layer: str
    target_type: str
    target_code: str | None
    evaluation_time: datetime
    state_code: str
    score: float
    confidence: float
    is_active: bool
    evidence: dict[str, Any]

    model_config = {"from_attributes": True}


class SymbolStateResponse(BaseModel):
    """銘柄の現在状態レスポンス"""
    ticker: str
    # 現在アクティブな状態コードのリスト
    active_states: list[str]
    # スナップショットの集約スコア（プライマリ状態の score）
    score: float | None
    # スナップショットの集約信頼度（プライマリ状態の confidence）
    confidence: float | None
    # 各アクティブ評価の evidence リスト（状態ごとの判定根拠）
    evidence_list: list[dict[str, Any]]
    # スナップショット更新時刻
    updated_at: datetime | None


# ─── エンドポイント ────────────────────────────────────────────────────────────

@router.get(
    "/current",
    response_model=list[CurrentStateItem],
    summary="現在の市場状態スナップショット",
)
async def get_current_market_state(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
    layer: str | None = Query(None, description="フィルタ: market / time_window / symbol"),
) -> list[CurrentStateItem]:
    """
    直近の市場状態スナップショットを返す。

    - layer パラメータで絞り込み可能。省略時は全 layer を返す。
    - CurrentStateSnapshot が存在しない場合は空リストを返す。
    """
    _verify_auth(authorization)

    repo = MarketStateRepository(db)
    layers = [layer] if layer else None
    snapshots = await repo.get_current_states(layers=layers)

    return [
        CurrentStateItem(
            layer=s.layer,
            target_type=s.target_type,
            target_code=s.target_code,
            active_states=s.active_states_json or [],
            summary=s.state_summary_json or {},
            updated_at=s.updated_at,
        )
        for s in snapshots
    ]


@router.get(
    "/history",
    response_model=list[EvaluationHistoryItem],
    summary="市場状態評価履歴",
)
async def get_market_state_history(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
    layer: str | None = Query(None, description="フィルタ: market / time_window / symbol"),
    target_type: str | None = Query(None, description="フィルタ: market / time_window など"),
    target_code: str | None = Query(None, description="フィルタ: 銘柄コード等"),
    limit: int = Query(50, ge=1, le=500, description="最大取得件数"),
) -> list[EvaluationHistoryItem]:
    """
    state_evaluations テーブルから評価履歴を返す（新しい順）。

    - 全パラメータはオプション。組み合わせてフィルタリング可能。
    - limit のデフォルトは 50、最大 500。
    """
    _verify_auth(authorization)

    repo = MarketStateRepository(db)
    rows = await repo.get_evaluation_history(
        layer=layer,
        target_type=target_type,
        target_code=target_code,
        limit=limit,
    )

    return [
        EvaluationHistoryItem(
            id=r.id,
            layer=r.layer,
            target_type=r.target_type,
            target_code=r.target_code,
            evaluation_time=r.evaluation_time,
            state_code=r.state_code,
            score=r.score,
            confidence=r.confidence,
            is_active=r.is_active,
            evidence=r.evidence_json or {},
        )
        for r in rows
    ]


@router.get(
    "/symbols/{ticker}",
    response_model=SymbolStateResponse,
    summary="銘柄の現在状態",
)
async def get_symbol_state(
    ticker: str,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
) -> SymbolStateResponse:
    """
    指定銘柄の現在状態スナップショットとアクティブな評価ログを返す。

    - active_states: 現在有効な状態コードのリスト（例: ["gap_up_open", "high_relative_volume"]）
    - score / confidence: プライマリ状態のスコアと信頼度
    - evidence_list: 各状態の判定根拠
    - データがない場合は 404 を返す
    """
    _verify_auth(authorization)

    repo = MarketStateRepository(db)
    snapshot = await repo.get_symbol_snapshot(ticker)
    evaluations = await repo.get_symbol_active_evaluations(ticker)

    if snapshot is None and not evaluations:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"銘柄 {ticker} の状態データが見つかりません",
        )

    active_states: list[str] = snapshot.active_states_json if snapshot else []
    summary: dict[str, Any] = snapshot.state_summary_json if snapshot else {}

    return SymbolStateResponse(
        ticker=ticker,
        active_states=active_states,
        score=summary.get("score"),
        confidence=summary.get("confidence"),
        evidence_list=[e.evidence_json for e in evaluations if e.evidence_json],
        updated_at=snapshot.updated_at if snapshot else None,
    )
