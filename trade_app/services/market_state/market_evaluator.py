"""
MarketStateEvaluator — 市場トレンド状態評価器（Phase 1 最小実装）

Phase 1 では簡易ルールベースの実装。
将来的に指数 OHLCV データ（TOPIX/日経225）を EvaluationContext.market_data に
渡すことで、より精緻なトレンド判定に置き換えられる設計にする。

状態コード:
  trend_up   : 上昇トレンド
  trend_down : 下降トレンド
  range      : レンジ相場

Phase 1 ルール:
  - market_data["index_change_pct"] が提供されている場合:
      > +0.5%  → trend_up
      < -0.5%  → trend_down
      その他   → range
  - データなしの場合: range（データ不足を証拠に記録）
"""
from __future__ import annotations

import logging

from trade_app.models.enums import StateLayer
from trade_app.services.market_state.evaluator_base import AbstractStateEvaluator
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult

logger = logging.getLogger(__name__)

# トレンド判定閾値（%）
_TREND_UP_THRESHOLD = 0.5
_TREND_DOWN_THRESHOLD = -0.5


class MarketStateEvaluator(AbstractStateEvaluator):
    """
    市場全体のトレンド状態を評価する。

    Phase 1: EvaluationContext.market_data["index_change_pct"] を使用。
             データ未提供時は "range" を返す。
    将来: OHLCV・移動平均・ADX などを使った高精度判定に置き換え可能。
    """

    @property
    def name(self) -> str:
        return "MarketStateEvaluator"

    def evaluate(self, ctx: EvaluationContext) -> list[StateEvaluationResult]:
        """
        市場トレンド状態を評価する。

        EvaluationContext.market_data に以下を含められる:
          - index_change_pct: float  当日の指数変化率（%）
          - index_name: str          使用した指数名（例: "TOPIX", "N225"）

        Returns:
            market レイヤーの StateEvaluationResult 1件
        """
        market_data = ctx.market_data
        change_pct: float | None = market_data.get("index_change_pct")
        index_name: str = market_data.get("index_name", "unknown")

        evidence: dict = {
            "evaluation_time_utc": ctx.evaluation_time.isoformat(),
            "index_name": index_name,
            "index_change_pct": change_pct,
            "thresholds": {
                "trend_up_above": _TREND_UP_THRESHOLD,
                "trend_down_below": _TREND_DOWN_THRESHOLD,
            },
        }

        if change_pct is None:
            state_code = "range"
            evidence["reason"] = "index_change_pct not provided — defaulting to range"
            confidence = 0.3  # データなしは信頼度低
        elif change_pct > _TREND_UP_THRESHOLD:
            state_code = "trend_up"
            evidence["reason"] = f"index up {change_pct:+.2f}% > threshold {_TREND_UP_THRESHOLD}%"
            confidence = min(1.0, abs(change_pct) / 2.0)  # 変化幅に応じて信頼度調整
        elif change_pct < _TREND_DOWN_THRESHOLD:
            state_code = "trend_down"
            evidence["reason"] = f"index down {change_pct:+.2f}% < threshold {_TREND_DOWN_THRESHOLD}%"
            confidence = min(1.0, abs(change_pct) / 2.0)
        else:
            state_code = "range"
            evidence["reason"] = (
                f"index change {change_pct:+.2f}% within "
                f"[{_TREND_DOWN_THRESHOLD}%, {_TREND_UP_THRESHOLD}%]"
            )
            confidence = 0.7

        logger.debug(
            "MarketStateEvaluator: state=%s change_pct=%s confidence=%.2f",
            state_code, change_pct, confidence,
        )

        return [
            StateEvaluationResult(
                layer=StateLayer.MARKET.value,
                target_type="market",
                target_code=None,
                state_code=state_code,
                score=1.0,
                confidence=confidence,
                evidence=evidence,
            )
        ]
