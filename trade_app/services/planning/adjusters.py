"""
Planning Adjusters — 流動性・スプレッド・ボラティリティによるサイズ調整

各 Adjuster は AdjustmentResult を返す。
サイズ縮小は可。サイズ増量は一切行わない（安全側原則）。

Phase 9 設計:
  - 閾値は class 定数として定義（将来 config 化可能）
  - market data が None の場合はデータ未取得として調整をスキップ
  - reject は SPREAD_TOO_WIDE / MARKET_NOT_TRADABLE / SYMBOL_NOT_TRADABLE のみ
  - それ以外（低流動性・高 ATR・高 vol）は縮小に留める
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from trade_app.services.planning.context import PlannerContext
from trade_app.services.planning.reasons import PlanningReasonCode

logger = logging.getLogger(__name__)


@dataclass
class AdjustmentResult:
    """
    1つの Adjuster が返す調整結果。

    reason_code が None の場合は「調整なし（通過）」を意味する。
    rejected=True の場合は以降の stage をスキップして reject へ。
    """
    stage: str
    input_qty: int
    output_qty: int
    ratio_applied: float          # 0.0〜1.0（1.0 = 調整なし）
    reason_code: PlanningReasonCode | None
    reason_detail: str | None
    rejected: bool = False        # True = この stage で発注不可と判定

    @property
    def was_reduced(self) -> bool:
        return self.output_qty < self.input_qty

    def as_trace_entry(self) -> dict:
        return {
            "stage": self.stage,
            "input_qty": self.input_qty,
            "output_qty": self.output_qty,
            "ratio_applied": self.ratio_applied,
            "reason_code": self.reason_code.value if self.reason_code else None,
            "reason_detail": self.reason_detail,
            "rejected": self.rejected,
        }


class MarketTradabilityChecker:
    """
    市場・銘柄の取引可能性チェック。

    is_market_tradable=False → MARKET_NOT_TRADABLE で reject
    is_symbol_tradable=False → SYMBOL_NOT_TRADABLE で reject
    """

    def check(self, qty: int, ctx: PlannerContext) -> AdjustmentResult:
        if not ctx.is_market_tradable:
            return AdjustmentResult(
                stage="market_tradability",
                input_qty=qty,
                output_qty=0,
                ratio_applied=0.0,
                reason_code=PlanningReasonCode.MARKET_NOT_TRADABLE,
                reason_detail="市場が取引不可状態です",
                rejected=True,
            )
        if not ctx.is_symbol_tradable:
            return AdjustmentResult(
                stage="market_tradability",
                input_qty=qty,
                output_qty=0,
                ratio_applied=0.0,
                reason_code=PlanningReasonCode.SYMBOL_NOT_TRADABLE,
                reason_detail=f"{ctx.ticker} は現在取引停止中です",
                rejected=True,
            )
        return AdjustmentResult(
            stage="market_tradability",
            input_qty=qty,
            output_qty=qty,
            ratio_applied=1.0,
            reason_code=None,
            reason_detail=None,
        )


class LiquidityAdjuster:
    """
    出来高（volume_ratio）に基づくサイズ縮小。

    volume_ratio < REDUCE_THRESHOLD: 縮小
    volume_ratio の下限クリップは 0.0（= 全量縮小 = 0 → 後段で PLANNED_SIZE_ZERO）

    閾値（クラス定数 / 将来 config 化可能）:
      REDUCE_LOW_THRESHOLD  = 0.3: volume_ratio < 0.3 → 50% に縮小
      REDUCE_VERY_LOW       = 0.1: volume_ratio < 0.1 → 25% に縮小
    """

    REDUCE_LOW_THRESHOLD: float = 0.3    # volume_ratio < 0.3 で縮小開始
    REDUCE_LOW_RATIO: float = 0.5         # → 50% に縮小
    REDUCE_VERY_LOW_THRESHOLD: float = 0.1
    REDUCE_VERY_LOW_RATIO: float = 0.25  # → 25% に縮小

    def adjust(self, qty: int, ctx: PlannerContext) -> AdjustmentResult:
        """
        volume_ratio に応じてサイズを縮小する。

        volume_ratio が 1.0 以上（平常）なら調整なし。
        """
        vr = ctx.volume_ratio

        if vr >= self.REDUCE_LOW_THRESHOLD:
            # 平常または軽微な低流動性 → 調整なし
            return AdjustmentResult(
                stage="liquidity_adjustment",
                input_qty=qty,
                output_qty=qty,
                ratio_applied=1.0,
                reason_code=None,
                reason_detail=None,
            )

        if vr < self.REDUCE_VERY_LOW_THRESHOLD:
            ratio = self.REDUCE_VERY_LOW_RATIO
            code = PlanningReasonCode.LIQUIDITY_REDUCTION
            detail = f"volume_ratio={vr:.2f} < {self.REDUCE_VERY_LOW_THRESHOLD} → {ratio*100:.0f}% に縮小"
        else:
            ratio = self.REDUCE_LOW_RATIO
            code = PlanningReasonCode.LIQUIDITY_REDUCTION
            detail = f"volume_ratio={vr:.2f} < {self.REDUCE_LOW_THRESHOLD} → {ratio*100:.0f}% に縮小"

        new_qty = int(math.floor(qty * ratio))
        logger.debug("LiquidityAdjuster: %s qty %d→%d", detail, qty, new_qty)

        return AdjustmentResult(
            stage="liquidity_adjustment",
            input_qty=qty,
            output_qty=new_qty,
            ratio_applied=ratio,
            reason_code=code,
            reason_detail=detail,
        )


class SpreadAdjuster:
    """
    スプレッド（spread_bps）に基づくサイズ縮小または拒否。

    REJECT_BPS 以上: reject（スプレッドが広すぎて発注コスト過大）
    REDUCE_BPS 以上: 50% に縮小

    閾値（クラス定数）:
      REJECT_BPS  = 100.0 bps: 1% 以上のスプレッド → reject
      REDUCE_BPS  =  50.0 bps: 0.5% 以上のスプレッド → 縮小
    """

    REJECT_BPS: float = 100.0
    REDUCE_BPS: float = 50.0
    REDUCE_RATIO: float = 0.5

    def adjust(self, qty: int, ctx: PlannerContext) -> AdjustmentResult:
        bps = ctx.spread_bps

        if bps <= 0.0:
            # スプレッドデータなし or スプレッドなし → 通過
            return AdjustmentResult(
                stage="spread_adjustment",
                input_qty=qty,
                output_qty=qty,
                ratio_applied=1.0,
                reason_code=None,
                reason_detail=None,
            )

        if bps >= self.REJECT_BPS:
            logger.debug("SpreadAdjuster: REJECT spread_bps=%.1f", bps)
            return AdjustmentResult(
                stage="spread_adjustment",
                input_qty=qty,
                output_qty=0,
                ratio_applied=0.0,
                reason_code=PlanningReasonCode.SPREAD_TOO_WIDE,
                reason_detail=f"spread_bps={bps:.1f} >= {self.REJECT_BPS} → reject",
                rejected=True,
            )

        if bps >= self.REDUCE_BPS:
            new_qty = int(math.floor(qty * self.REDUCE_RATIO))
            detail = f"spread_bps={bps:.1f} >= {self.REDUCE_BPS} → {self.REDUCE_RATIO*100:.0f}% に縮小"
            logger.debug("SpreadAdjuster: REDUCE %s", detail)
            return AdjustmentResult(
                stage="spread_adjustment",
                input_qty=qty,
                output_qty=new_qty,
                ratio_applied=self.REDUCE_RATIO,
                reason_code=PlanningReasonCode.SPREAD_REDUCTION,
                reason_detail=detail,
            )

        return AdjustmentResult(
            stage="spread_adjustment",
            input_qty=qty,
            output_qty=qty,
            ratio_applied=1.0,
            reason_code=None,
            reason_detail=None,
        )


class VolatilityAdjuster:
    """
    ATR・ヒストリカルボラティリティに基づくサイズ縮小。

    ATR が市場価格の一定割合を超える場合は縮小。
    ボラティリティが閾値を超える場合は縮小。

    データが None の場合は調整スキップ（Phase 9: データ未整備を安全側で許容）

    閾値（クラス定数）:
      ATR_REDUCE_RATIO   = 0.03: ATR / price > 3% → 50% 縮小
      VOL_REDUCE_RATIO   = 0.04: volatility > 4% → 50% 縮小
    """

    ATR_REDUCE_THRESHOLD: float = 0.03   # ATR / market_price > 3% で縮小
    ATR_REDUCE_RATIO: float = 0.5
    VOL_REDUCE_THRESHOLD: float = 0.04   # volatility (日次) > 4% で縮小
    VOL_REDUCE_RATIO: float = 0.5

    def adjust(self, qty: int, ctx: PlannerContext) -> AdjustmentResult:
        # ─── ATR チェック ────────────────────────────────────────────────
        if ctx.atr is not None and ctx.effective_market_price:
            atr_ratio = ctx.atr / ctx.effective_market_price
            if atr_ratio > self.ATR_REDUCE_THRESHOLD:
                new_qty = int(math.floor(qty * self.ATR_REDUCE_RATIO))
                detail = (
                    f"ATR={ctx.atr:.2f} / price={ctx.effective_market_price:.2f} "
                    f"= {atr_ratio:.3f} > {self.ATR_REDUCE_THRESHOLD} → 縮小"
                )
                logger.debug("VolatilityAdjuster ATR reduce: %s qty %d→%d", detail, qty, new_qty)
                return AdjustmentResult(
                    stage="volatility_adjustment",
                    input_qty=qty,
                    output_qty=new_qty,
                    ratio_applied=self.ATR_REDUCE_RATIO,
                    reason_code=PlanningReasonCode.ATR_REDUCTION,
                    reason_detail=detail,
                )

        # ─── ヒストリカル・ボラティリティ チェック ────────────────────────
        if ctx.volatility is not None:
            if ctx.volatility > self.VOL_REDUCE_THRESHOLD:
                new_qty = int(math.floor(qty * self.VOL_REDUCE_RATIO))
                detail = f"volatility={ctx.volatility:.4f} > {self.VOL_REDUCE_THRESHOLD} → 縮小"
                logger.debug("VolatilityAdjuster vol reduce: %s qty %d→%d", detail, qty, new_qty)
                return AdjustmentResult(
                    stage="volatility_adjustment",
                    input_qty=qty,
                    output_qty=new_qty,
                    ratio_applied=self.VOL_REDUCE_RATIO,
                    reason_code=PlanningReasonCode.VOLATILITY_REDUCTION,
                    reason_detail=detail,
                )

        # データなし or 閾値以下 → 調整なし
        return AdjustmentResult(
            stage="volatility_adjustment",
            input_qty=qty,
            output_qty=qty,
            ratio_applied=1.0,
            reason_code=None,
            reason_detail=None,
        )
