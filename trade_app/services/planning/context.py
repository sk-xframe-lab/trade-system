"""
PlannerContext — Planning Layer への入力コンテキスト

PlannerContext:
  SignalPlanningService が各 stage で参照するすべての入力を一箇所に集約する。
  直接 DB / Broker を各 stage から呼ばせず、依存を context builder に集中させる。

PlannerContextBuilder:
  DB クエリ（最新 SignalStrategyDecision 取得）と
  外部から注入されたパラメータをまとめて PlannerContext を生成する。

設計:
  - Phase 9 では market data（spread_bps, volume_ratio, ATR, volatility）は optional
  - 未取得の場合は安全側デフォルト値を使用（縮小・拒否なし）
  - Phase 10 以降で実際の market data フィードに接続する
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.signal import TradeSignal
from trade_app.models.signal_strategy_decision import SignalStrategyDecision

logger = logging.getLogger(__name__)


@dataclass
class PlannerContext:
    """
    SignalPlanningService が参照するすべての入力をまとめた入力コンテキスト。

    一度生成したらイミュータブルとして扱うこと（各 stage が書き込まない）。
    """
    # ─── signal 情報 ──────────────────────────────────────────────────────
    signal: TradeSignal
    # ─── strategy decision 情報 ───────────────────────────────────────────
    # strategy gate 結果の size_ratio（0.0〜1.0）
    size_ratio: float
    # signal_strategy_decisions.id（nullable: exit bypass 時）
    signal_strategy_decision_id: str | None
    # decision の評価時刻（stale チェックに使用）
    decision_evaluation_time: datetime | None

    # ─── 市場状態 ─────────────────────────────────────────────────────────
    is_market_tradable: bool = True    # False → MARKET_NOT_TRADABLE で reject
    is_symbol_tradable: bool = True    # False → SYMBOL_NOT_TRADABLE で reject

    # ─── 銘柄メタデータ ───────────────────────────────────────────────────
    symbol_lot_size: int = 100         # 単元株数（日本株: 通常 100 株）

    # ─── market data（Phase 9: optional。None = データなし = 安全デフォルト）──
    market_price: float | None = None          # 現在値（None = signal.limit_price で代替）
    spread_bps: float = 0.0                    # bid-ask スプレッド（basis points）
    volume_ratio: float = 1.0                  # 相対出来高（1.0 = 平常、< 1 = 低流動性）
    atr: float | None = None                   # ATR（Average True Range）
    volatility: float | None = None            # ヒストリカル・ボラティリティ（日次）

    @property
    def effective_market_price(self) -> float | None:
        """
        有効な市場価格を返す。
        market_price が None の場合は signal.limit_price を代替として使用する。
        """
        if self.market_price is not None:
            return self.market_price
        return self.signal.limit_price

    @property
    def ticker(self) -> str:
        return self.signal.ticker

    @property
    def base_quantity(self) -> int:
        return self.signal.quantity


class PlannerContextBuilder:
    """
    PlannerContext を DB クエリ + 外部パラメータから構築するビルダー。

    責務:
      - DB から最新 SignalStrategyDecision を取得
      - 外部から注入されたパラメータをまとめる

    直接 BrokerAdapter は呼ばない。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def build(
        self,
        signal: TradeSignal,
        *,
        # strategy decision（DB から取得、または直接指定）
        size_ratio: float = 1.0,
        # market state（Phase 9: 外部から注入。デフォルトは安全側）
        is_market_tradable: bool = True,
        is_symbol_tradable: bool = True,
        # 銘柄メタデータ（デフォルト: 日本株 100 株単元）
        symbol_lot_size: int = 100,
        # market data（Phase 9: optional）
        market_price: float | None = None,
        spread_bps: float = 0.0,
        volume_ratio: float = 1.0,
        atr: float | None = None,
        volatility: float | None = None,
    ) -> PlannerContext:
        """
        PlannerContext を生成する。

        DB から最新の SignalStrategyDecision を取得して context に組み込む。
        見つからない場合は signal_strategy_decision_id=None とする（planning で検証する）。

        Args:
            signal: チェック対象の TradeSignal
            size_ratio: strategy gate 結果の size_ratio（デフォルト 1.0）
            is_market_tradable: 市場が取引可能か
            is_symbol_tradable: 銘柄が取引可能か
            symbol_lot_size: 単元株数
            market_price: 現在の市場価格（None → signal.limit_price で代替）
            spread_bps: bid-ask スプレッド（basis points）
            volume_ratio: 相対出来高（1.0 = 平常）
            atr: ATR（None = 未取得）
            volatility: ヒストリカル・ボラティリティ（None = 未取得）
        """
        # ─── 最新 SignalStrategyDecision を DB から取得 ──────────────────
        ssd_id: str | None = None
        decision_eval_time: datetime | None = None

        result = await self._db.execute(
            select(SignalStrategyDecision)
            .where(SignalStrategyDecision.signal_id == signal.id)
            .order_by(SignalStrategyDecision.decision_time.desc())
            .limit(1)
        )
        ssd = result.scalar_one_or_none()
        if ssd is not None:
            ssd_id = ssd.id
            decision_eval_time = ssd.decision_time
            logger.debug(
                "PlannerContextBuilder: found SignalStrategyDecision %s for signal %s",
                ssd_id, signal.id,
            )
        else:
            logger.debug(
                "PlannerContextBuilder: no SignalStrategyDecision found for signal %s",
                signal.id,
            )

        return PlannerContext(
            signal=signal,
            size_ratio=size_ratio,
            signal_strategy_decision_id=ssd_id,
            decision_evaluation_time=decision_eval_time,
            is_market_tradable=is_market_tradable,
            is_symbol_tradable=is_symbol_tradable,
            symbol_lot_size=symbol_lot_size,
            market_price=market_price,
            spread_bps=spread_bps,
            volume_ratio=volume_ratio,
            atr=atr,
            volatility=volatility,
        )
