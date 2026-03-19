"""
TimeWindowStateEvaluator — 時間帯状態評価器

日本株現物取引の時間帯を評価し、現在の取引セッション状態を返す。

時間帯定義（JST）:
  pre_open               : 08:00-09:00  板形成・気配値確認
  opening_auction_risk   : 09:00-09:15  寄り付き直後の高ボラティリティ
  morning_trend_zone     : 09:15-11:30  午前のトレンドゾーン
  midday_low_liquidity   : 11:30-12:30  昼時間帯（流動性低下）
                           ※ TSE は 2024-11-05 に昼休みを廃止。
                             流動性低下傾向は残るため引き続きゾーンとして管理。
  afternoon_repricing_zone: 12:30-12:45 午後再開直後の値付け直し
  closing_cleanup_zone   : 14:50-15:30  大引けに向けたポジション整理
  after_hours            : 15:30以降 or 08:00前  時間外
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from trade_app.models.enums import StateLayer
from trade_app.services.market_state.evaluator_base import AbstractStateEvaluator
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult

logger = logging.getLogger(__name__)

_JST = ZoneInfo("Asia/Tokyo")

# ─── 時間帯境界（JST）────────────────────────────────────────────────────────
_PRE_OPEN_START = time(8, 0)
_TRADING_START = time(9, 0)
_OPENING_RISK_END = time(9, 15)
_MORNING_TREND_END = time(11, 30)
_MIDDAY_END = time(12, 30)
_AFTERNOON_SETTLE_END = time(12, 45)
_CLOSING_START = time(14, 50)
_TRADING_END = time(15, 30)


def _classify_time_window(t: time) -> tuple[str, dict]:
    """
    JST の time オブジェクトから時間帯状態コードと証拠を返す。

    Returns:
        (state_code, evidence_dict)
    """
    evidence_base = {
        "jst_time": t.strftime("%H:%M:%S"),
        "boundaries": {
            "pre_open": f"{_PRE_OPEN_START}-{_TRADING_START}",
            "opening_auction_risk": f"{_TRADING_START}-{_OPENING_RISK_END}",
            "morning_trend_zone": f"{_OPENING_RISK_END}-{_MORNING_TREND_END}",
            "midday_low_liquidity": f"{_MORNING_TREND_END}-{_MIDDAY_END}",
            "afternoon_repricing_zone": f"{_MIDDAY_END}-{_AFTERNOON_SETTLE_END}",
            "closing_cleanup_zone": f"{_CLOSING_START}-{_TRADING_END}",
        },
    }

    if _PRE_OPEN_START <= t < _TRADING_START:
        return "pre_open", {**evidence_base, "description": "板形成・気配値確認"}

    if _TRADING_START <= t < _OPENING_RISK_END:
        return "opening_auction_risk", {
            **evidence_base,
            "description": "寄り付き直後の高ボラティリティゾーン",
        }

    if _OPENING_RISK_END <= t < _MORNING_TREND_END:
        return "morning_trend_zone", {
            **evidence_base,
            "description": "午前のトレンドゾーン（流動性・方向性ともに良好）",
        }

    if _MORNING_TREND_END <= t < _MIDDAY_END:
        return "midday_low_liquidity", {
            **evidence_base,
            "description": "昼時間帯（流動性低下傾向）",
            "note": "TSE は 2024-11-05 に昼休み廃止。流動性は改善傾向だが注意",
        }

    if _MIDDAY_END <= t < _AFTERNOON_SETTLE_END:
        return "afternoon_repricing_zone", {
            **evidence_base,
            "description": "午後再開直後の値付け直しゾーン",
        }

    if _AFTERNOON_SETTLE_END <= t < _CLOSING_START:
        return "morning_trend_zone", {
            **evidence_base,
            "description": "午後トレンドゾーン（引けまでの主要取引時間）",
            "sub_zone": "afternoon_main",
        }

    if _CLOSING_START <= t <= _TRADING_END:
        return "closing_cleanup_zone", {
            **evidence_base,
            "description": "大引けに向けたポジション整理ゾーン",
        }

    # after_hours: < 08:00 or > 15:30
    return "after_hours", {
        **evidence_base,
        "description": "時間外（取引なし）",
    }


class TimeWindowStateEvaluator(AbstractStateEvaluator):
    """
    現在時刻から時間帯状態を評価する。
    BrokerAdapter や DB には依存しない純粋な時間評価器。
    """

    @property
    def name(self) -> str:
        return "TimeWindowEvaluator"

    def evaluate(self, ctx: EvaluationContext) -> list[StateEvaluationResult]:
        """
        EvaluationContext の evaluation_time から時間帯を判定する。

        Returns:
            time_window レイヤーの StateEvaluationResult 1件
        """
        # evaluation_time を JST に変換
        eval_time = ctx.evaluation_time
        if eval_time.tzinfo is None:
            eval_time = eval_time.replace(tzinfo=timezone.utc)
        jst_time = eval_time.astimezone(_JST)
        t = jst_time.time()

        state_code, evidence = _classify_time_window(t)
        evidence["evaluation_time_utc"] = eval_time.isoformat()
        evidence["evaluation_time_jst"] = jst_time.isoformat()

        logger.debug(
            "TimeWindowEvaluator: state=%s jst=%s",
            state_code, jst_time.strftime("%H:%M:%S"),
        )

        return [
            StateEvaluationResult(
                layer=StateLayer.TIME_WINDOW.value,
                target_type="time_window",
                target_code=None,
                state_code=state_code,
                score=1.0,
                confidence=1.0,
                evidence=evidence,
            )
        ]
