"""
SymbolStateEvaluator — 銘柄状態評価器

ctx.symbol_data（ticker → dict）から銘柄ごとに複数の状態を評価し、
StateEvaluationResult のリストを返す。

売買判断は行わない。状態コードと証拠を記録することのみが責務。

入力 (ctx.symbol_data keyed by ticker):
    current_price       : float  最終値（現在値）
    current_open        : float  当日始値
    prev_close          : float  前日終値
    vwap                : float  当日 VWAP
    ma5                 : float  5日移動平均
    ma20               : float  20日移動平均
    atr                 : float  ATR（日次）
    rsi                 : float  RSI（14日）
    current_volume      : float  当日累積出来高
    avg_volume_same_time: float  同時刻の平均出来高
    best_bid            : float  最良売値（bid）
    best_ask            : float  最良買値（ask）

出力 (layer="symbol", target_type="symbol", target_code=ticker):
    複数の StateEvaluationResult（銘柄に対して複数の状態が同時に有効）

状態コード一覧:
    gap_up_open           : 始値が前日比 +2% 以上のギャップアップ
    gap_down_open         : 始値が前日比 -2% 以下のギャップダウン
    symbol_trend_up       : price > VWAP かつ MA5 > MA20
    symbol_trend_down     : price < VWAP かつ MA5 < MA20
    symbol_range          : トレンドなし かつ ATR 低水準
    high_relative_volume  : 同時刻平均比200% 以上の出来高
    low_liquidity         : 同時刻平均比30% 未満の出来高
    wide_spread           : スプレッド / 中間値 >= 0.3%
    symbol_volatility_high: ATR / 現在値 >= 2%
    breakout_candidate    : price > MA20 かつ 高出来高 かつ ギャップなし
    overextended          : RSI >= 75 または RSI <= 25
"""
from __future__ import annotations

import logging
from typing import Any

from trade_app.services.market_state.evaluator_base import AbstractStateEvaluator
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult

logger = logging.getLogger(__name__)

_LAYER = "symbol"
_TARGET_TYPE = "symbol"


class SymbolStateEvaluator(AbstractStateEvaluator):
    """
    銘柄の状態を評価する Evaluator。

    ctx.symbol_data が空の場合は空リストを返す（監視銘柄なし）。
    各銘柄に対して複数の状態が同時に有効となりうる（例: gap_up_open + high_relative_volume）。
    Engine の save_evaluations はグループ単位でソフト失効するため、
    同一 ticker の複数結果は正しく保存される。
    """

    # ─── 閾値定数 ──────────────────────────────────────────────────────────────

    GAP_THRESHOLD: float = 0.02       # 2%: gap up / gap down 判定
    VOLUME_RATIO_HIGH: float = 2.0    # 200%: high_relative_volume
    VOLUME_RATIO_LOW: float = 0.3     # 30%: low_liquidity
    SPREAD_THRESHOLD: float = 0.003   # 0.3%: wide_spread
    ATR_RATIO_HIGH: float = 0.02      # 2%: symbol_volatility_high / range 境界
    RSI_OVERBOUGHT: float = 75.0      # RSI >= 75: overextended (overbought)
    RSI_OVERSOLD: float = 25.0        # RSI <= 25: overextended (oversold)

    @property
    def name(self) -> str:
        return "SymbolStateEvaluator"

    def evaluate(self, ctx: EvaluationContext) -> list[StateEvaluationResult]:
        """
        symbol_data に含まれる全銘柄を評価する。

        Returns:
            全銘柄の StateEvaluationResult リスト（空データなら []）
        """
        if not ctx.symbol_data:
            return []

        results: list[StateEvaluationResult] = []
        for ticker, data in ctx.symbol_data.items():
            try:
                symbol_results = self._evaluate_symbol(ticker, data)
                results.extend(symbol_results)
                logger.debug(
                    "SymbolStateEvaluator: ticker=%s → %d state(s): %s",
                    ticker,
                    len(symbol_results),
                    [r.state_code for r in symbol_results],
                )
            except Exception as exc:
                logger.error(
                    "SymbolStateEvaluator: ticker=%s error=%s",
                    ticker, exc, exc_info=True,
                )

        return results

    # ─── 内部実装 ─────────────────────────────────────────────────────────────

    def _make(
        self,
        ticker: str,
        state_code: str,
        score: float,
        confidence: float,
        evidence: dict[str, Any],
    ) -> StateEvaluationResult:
        return StateEvaluationResult(
            layer=_LAYER,
            target_type=_TARGET_TYPE,
            target_code=ticker,
            state_code=state_code,
            score=max(0.0, min(1.0, score)),
            confidence=max(0.0, min(1.0, confidence)),
            evidence=evidence,
        )

    def _evaluate_symbol(
        self, ticker: str, data: dict[str, Any]
    ) -> list[StateEvaluationResult]:
        """1銘柄の全状態を評価して返す。"""
        results: list[StateEvaluationResult] = []

        # データ抽出（None は「データなし」として各ルールがスキップ）
        current_price = data.get("current_price")
        current_open = data.get("current_open")
        prev_close = data.get("prev_close")
        vwap = data.get("vwap")
        ma5 = data.get("ma5")
        ma20 = data.get("ma20")
        atr = data.get("atr")
        rsi = data.get("rsi")
        current_volume = data.get("current_volume")
        avg_volume = data.get("avg_volume_same_time")
        best_bid = data.get("best_bid")
        best_ask = data.get("best_ask")

        # ── ギャップ判定 ──────────────────────────────────────────────────────
        is_gap_up = False
        is_gap_down = False
        if current_open is not None and prev_close is not None and prev_close != 0:
            gap_pct = (current_open - prev_close) / prev_close
            if gap_pct >= self.GAP_THRESHOLD:
                is_gap_up = True
                score = min(1.0, gap_pct / 0.04)  # 4% gap → score 1.0
                results.append(self._make(ticker, "gap_up_open", score, 0.9, {
                    "gap_pct": round(gap_pct * 100, 3),
                    "current_open": current_open,
                    "prev_close": prev_close,
                    "threshold_pct": self.GAP_THRESHOLD * 100,
                    "rule": "(current_open - prev_close) / prev_close >= 0.02",
                }))
            elif gap_pct <= -self.GAP_THRESHOLD:
                is_gap_down = True
                score = min(1.0, abs(gap_pct) / 0.04)
                results.append(self._make(ticker, "gap_down_open", score, 0.9, {
                    "gap_pct": round(gap_pct * 100, 3),
                    "current_open": current_open,
                    "prev_close": prev_close,
                    "threshold_pct": -self.GAP_THRESHOLD * 100,
                    "rule": "(current_open - prev_close) / prev_close <= -0.02",
                }))

        # ── 出来高判定 ────────────────────────────────────────────────────────
        is_high_volume = False
        if current_volume is not None and avg_volume is not None and avg_volume > 0:
            vol_ratio = current_volume / avg_volume
            if vol_ratio >= self.VOLUME_RATIO_HIGH:
                is_high_volume = True
                score = min(1.0, vol_ratio / 4.0)  # 4x 平均 → score 1.0
                results.append(self._make(ticker, "high_relative_volume", score, 0.85, {
                    "volume_ratio": round(vol_ratio, 3),
                    "current_volume": current_volume,
                    "avg_volume_same_time": avg_volume,
                    "threshold": self.VOLUME_RATIO_HIGH,
                    "rule": "current_volume / avg_volume_same_time >= 2.0",
                }))
            elif vol_ratio < self.VOLUME_RATIO_LOW:
                score = max(0.1, 1.0 - vol_ratio / self.VOLUME_RATIO_LOW)
                results.append(self._make(ticker, "low_liquidity", score, 0.8, {
                    "volume_ratio": round(vol_ratio, 3),
                    "current_volume": current_volume,
                    "avg_volume_same_time": avg_volume,
                    "threshold": self.VOLUME_RATIO_LOW,
                    "rule": "current_volume / avg_volume_same_time < 0.3",
                }))

        # ── トレンド判定 ──────────────────────────────────────────────────────
        is_trend_up = False
        is_trend_down = False
        if (
            current_price is not None
            and vwap is not None
            and ma5 is not None
            and ma20 is not None
            and vwap > 0
            and ma20 > 0
        ):
            price_above_vwap = current_price > vwap
            ma5_above_ma20 = ma5 > ma20
            if price_above_vwap and ma5_above_ma20:
                is_trend_up = True
                vwap_diff = (current_price - vwap) / vwap
                ma_diff = (ma5 - ma20) / ma20
                score = max(0.3, min(1.0, (vwap_diff + ma_diff) * 20))
                results.append(self._make(ticker, "symbol_trend_up", score, 0.75, {
                    "current_price": current_price,
                    "vwap": vwap,
                    "ma5": ma5,
                    "ma20": ma20,
                    "price_above_vwap": True,
                    "ma5_above_ma20": True,
                    "rule": "price > vwap AND ma5 > ma20",
                }))
            elif not price_above_vwap and not ma5_above_ma20:
                is_trend_down = True
                vwap_diff = (vwap - current_price) / vwap
                ma_diff = (ma20 - ma5) / ma20
                score = max(0.3, min(1.0, (vwap_diff + ma_diff) * 20))
                results.append(self._make(ticker, "symbol_trend_down", score, 0.75, {
                    "current_price": current_price,
                    "vwap": vwap,
                    "ma5": ma5,
                    "ma20": ma20,
                    "price_above_vwap": False,
                    "ma5_above_ma20": False,
                    "rule": "price < vwap AND ma5 < ma20",
                }))

        # ── レンジ判定（トレンドなし かつ ATR 低水準）──────────────────────
        if (
            not is_trend_up
            and not is_trend_down
            and current_price is not None
            and atr is not None
            and current_price > 0
        ):
            atr_ratio = atr / current_price
            if atr_ratio < self.ATR_RATIO_HIGH:
                score = max(0.1, 1.0 - atr_ratio / self.ATR_RATIO_HIGH)
                results.append(self._make(ticker, "symbol_range", score, 0.65, {
                    "current_price": current_price,
                    "atr": atr,
                    "atr_ratio": round(atr_ratio, 6),
                    "threshold": self.ATR_RATIO_HIGH,
                    "rule": "not trending AND atr / price < 0.02",
                }))

        # ── スプレッド判定 ────────────────────────────────────────────────────
        if (
            best_bid is not None
            and best_ask is not None
            and best_bid > 0
            and best_ask > 0
        ):
            mid_price = (best_bid + best_ask) / 2
            if mid_price > 0:
                spread_ratio = (best_ask - best_bid) / mid_price
                if spread_ratio >= self.SPREAD_THRESHOLD:
                    score = min(1.0, spread_ratio / 0.01)  # 1% spread → score 1.0
                    results.append(self._make(ticker, "wide_spread", score, 0.9, {
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "spread": round(best_ask - best_bid, 4),
                        "spread_ratio": round(spread_ratio, 6),
                        "mid_price": mid_price,
                        "threshold": self.SPREAD_THRESHOLD,
                        "rule": "(ask - bid) / mid_price >= 0.003",
                    }))

        # ── ボラティリティ判定（ATR 高水準）──────────────────────────────────
        if current_price is not None and atr is not None and current_price > 0:
            atr_ratio = atr / current_price
            if atr_ratio >= self.ATR_RATIO_HIGH:
                score = min(1.0, atr_ratio / 0.05)  # 5% ATR → score 1.0
                results.append(self._make(ticker, "symbol_volatility_high", score, 0.8, {
                    "current_price": current_price,
                    "atr": atr,
                    "atr_ratio": round(atr_ratio, 6),
                    "threshold": self.ATR_RATIO_HIGH,
                    "rule": "atr / price >= 0.02",
                }))

        # ── ブレイクアウト候補（高出来高 + MA20 上抜け + ギャップなし）────
        if (
            current_price is not None
            and ma20 is not None
            and ma20 > 0
            and is_high_volume
            and not is_gap_up
            and not is_gap_down
            and current_price > ma20
        ):
            pct_above_ma20 = (current_price - ma20) / ma20
            score = max(0.3, min(1.0, pct_above_ma20 / 0.03))  # 3% above → score 1.0
            results.append(self._make(ticker, "breakout_candidate", score, 0.7, {
                "current_price": current_price,
                "ma20": ma20,
                "price_above_ma20_pct": round(pct_above_ma20 * 100, 3),
                "is_high_volume": True,
                "rule": "price > ma20 AND high_relative_volume AND no_gap",
            }))

        # ── RSI 過熱判定 ──────────────────────────────────────────────────────
        if rsi is not None:
            if rsi >= self.RSI_OVERBOUGHT:
                score = min(1.0, (rsi - self.RSI_OVERBOUGHT) / 15.0)
                results.append(self._make(ticker, "overextended", max(0.3, score), 0.75, {
                    "rsi": rsi,
                    "direction": "overbought",
                    "threshold": self.RSI_OVERBOUGHT,
                    "rule": "rsi >= 75",
                }))
            elif rsi <= self.RSI_OVERSOLD:
                score = min(1.0, (self.RSI_OVERSOLD - rsi) / 15.0)
                results.append(self._make(ticker, "overextended", max(0.3, score), 0.75, {
                    "rsi": rsi,
                    "direction": "oversold",
                    "threshold": self.RSI_OVERSOLD,
                    "rule": "rsi <= 25",
                }))

        return results
