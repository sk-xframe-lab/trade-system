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
    wide_spread           : スプレッド / 現在値 >= 0.3%
    symbol_volatility_high: ATR / 現在値 >= 2%
    breakout_candidate    : price > MA20 かつ 高出来高 かつ ギャップなし
    overextended          : RSI >= 75 または RSI <= 25
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from trade_app.services.market_state.evaluator_base import AbstractStateEvaluator
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult

logger = logging.getLogger(__name__)

_LAYER = "symbol"
_TARGET_TYPE = "symbol"

# ─── Rule 型エイリアス ────────────────────────────────────────────────────────
# _rule_* 関数が共通で受け取る make ヘルパーの型
_MakeFn = Callable[[str, str, float, float, dict[str, Any]], StateEvaluationResult]

# rule が active になった場合に deps dict に書き込むフラグ名。
# 後続の依存型 rule がキーワード引数として deps.get(flag, False) で参照する。
_RULE_DEP_FLAGS: dict[str, str] = {
    "gap_up_open":          "is_gap_up",
    "gap_down_open":        "is_gap_down",
    "high_relative_volume": "is_high_volume",
    "symbol_trend_up":      "is_trend_up",
    "symbol_trend_down":    "is_trend_down",
}

# _RULE_REGISTRY / _RULES は全 _rule_*() 関数定義の後（ファイル末尾付近）に配置する。


# ─── Rule 関数（他ルールに依存しない独立 rule） ───────────────────────────────

def _rule_wide_spread(
    ticker: str,
    data: dict[str, Any],
    *,
    spread_threshold: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    wide_spread rule: スプレッド / 現在値 >= spread_threshold

    判定式:
        spread = best_ask - best_bid
        spread_rate = spread / current_price
        発火: spread_rate >= spread_threshold

    ガード（すべて (None, diag) を返す）:
        - current_price が None または <= 0 → skipped / no_current_price
        - best_bid が None または <= 0     → skipped / no_bid
        - best_ask が None または <= 0     → skipped / no_ask
        - best_ask < best_bid             → skipped / inverted_spread (warning ログ)

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_price = data.get("current_price")
    best_bid = data.get("best_bid")
    best_ask = data.get("best_ask")

    # ガード 1: current_price が無効
    if current_price is None or current_price <= 0:
        return None, {"status": "skipped", "reason": "no_current_price"}

    # ガード 2: bid / ask の有無チェック（None および <= 0 を無効値として扱う）
    bid_valid = best_bid is not None and best_bid > 0
    ask_valid = best_ask is not None and best_ask > 0
    if not bid_valid:
        return None, {"status": "skipped", "reason": "no_bid"}
    if not ask_valid:
        return None, {"status": "skipped", "reason": "no_ask"}

    # ガード 3: 逆転スプレッド（データ異常。サイレントにスキップしない）
    if best_ask < best_bid:
        logger.warning(
            "SymbolStateEvaluator: ticker=%s inverted spread "
            "(bid=%.4f > ask=%.4f) — wide_spread 評価スキップ",
            ticker, best_bid, best_ask,
        )
        return None, {"status": "skipped", "reason": "inverted_spread"}

    # spread_rate = (ask - bid) / current_price
    # ※ mid_price ではなく current_price を分母にすることで
    #   現在値ベースのコスト影響度として評価する
    spread = best_ask - best_bid
    spread_rate = spread / current_price
    if spread_rate < spread_threshold:
        return None, {"status": "inactive", "spread_rate": round(spread_rate, 6)}

    score = min(1.0, spread_rate / 0.01)  # 1% spread → score 1.0
    return (
        make(ticker, "wide_spread", score, 0.9, {
            "reason": "wide_spread",
            "best_bid": best_bid,
            "best_ask": best_ask,
            "current_price": current_price,
            "spread": round(spread, 4),
            "spread_rate": round(spread_rate, 6),
            "threshold": spread_threshold,
            "rule": "(ask - bid) / current_price >= 0.003",
        }),
        {"status": "active", "spread_rate": round(spread_rate, 6), "spread": round(spread, 4)},
    )


def _rule_overextended(
    ticker: str,
    data: dict[str, Any],
    *,
    rsi_overbought: float,
    rsi_oversold: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    overextended rule: RSI が過熱水準に達している状態を検出する。

    発火条件:
        overbought: rsi >= rsi_overbought  → direction="overbought"
        oversold:   rsi <= rsi_oversold    → direction="oversold"

    ガード（(None, diag) を返す）:
        - rsi が None → skipped / no_rsi

    score 計算:
        overbought: min(1.0, (rsi - rsi_overbought) / 15.0)、最小 0.3
        oversold:   min(1.0, (rsi_oversold - rsi) / 15.0)、最小 0.3

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    rsi = data.get("rsi")
    if rsi is None:
        return None, {"status": "skipped", "reason": "no_rsi"}

    if rsi >= rsi_overbought:
        score = min(1.0, (rsi - rsi_overbought) / 15.0)
        return (
            make(ticker, "overextended", max(0.3, score), 0.75, {
                "rsi": rsi,
                "direction": "overbought",
                "threshold": rsi_overbought,
                "rule": "rsi >= 75",
            }),
            {"status": "active", "direction": "overbought", "rsi": rsi},
        )

    if rsi <= rsi_oversold:
        score = min(1.0, (rsi_oversold - rsi) / 15.0)
        return (
            make(ticker, "overextended", max(0.3, score), 0.75, {
                "rsi": rsi,
                "direction": "oversold",
                "threshold": rsi_oversold,
                "rule": "rsi <= 25",
            }),
            {"status": "active", "direction": "oversold", "rsi": rsi},
        )

    return None, {"status": "inactive", "rsi": rsi}


def _rule_price_stale(
    ticker: str,
    data: dict[str, Any],
    *,
    evaluation_time: datetime,
    threshold_sec: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    price_stale rule: 価格情報が evaluation_time に対して新鮮でない状態を検出する。

    Gate: "last_updated" キーが data に存在しない場合は評価しない。
    fetcher が last_updated を付与した data のみを対象とすることで、
    last_updated を含まない既存テストデータへの影響をゼロにする。

    発火条件:
        1. current_price is None
           → reason = "missing_price"
        2. current_price はあるが last_updated is None
           → reason = "missing_timestamp"
        3. (evaluation_time - last_updated).total_seconds() >= threshold_sec
           → reason = "stale_price"（evidence に age_sec を追加）

    非発火:
        - "last_updated" キーが data にない → skipped / no_last_updated_key
        - current_price があり、last_updated があり、age_sec < threshold_sec → inactive

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火）
    """
    # Gate: last_updated キーが存在しない場合は評価しない
    if "last_updated" not in data:
        return None, {"status": "skipped", "reason": "no_last_updated_key"}

    current_price = data.get("current_price")
    last_updated: datetime | None = data["last_updated"]
    ev_iso = evaluation_time.isoformat()

    # 条件 1: current_price が未取得
    if current_price is None:
        return (
            make(ticker, "price_stale", 1.0, 0.9, {
                "reason": "missing_price",
                "current_price": None,
                "last_updated": last_updated.isoformat() if last_updated is not None else None,
                "evaluation_time": ev_iso,
                "threshold_sec": threshold_sec,
            }),
            {"status": "active", "reason": "missing_price"},
        )

    # 条件 2: last_updated が未設定
    if last_updated is None:
        return (
            make(ticker, "price_stale", 1.0, 0.9, {
                "reason": "missing_timestamp",
                "current_price": current_price,
                "last_updated": None,
                "evaluation_time": ev_iso,
                "threshold_sec": threshold_sec,
            }),
            {"status": "active", "reason": "missing_timestamp"},
        )

    # 条件 3: 古い価格
    age_sec = (evaluation_time - last_updated).total_seconds()
    if age_sec >= threshold_sec:
        return (
            make(ticker, "price_stale", 1.0, 0.9, {
                "reason": "stale_price",
                "current_price": current_price,
                "last_updated": last_updated.isoformat(),
                "evaluation_time": ev_iso,
                "age_sec": round(age_sec, 1),
                "threshold_sec": threshold_sec,
            }),
            {"status": "active", "reason": "stale_price", "age_sec": round(age_sec, 1)},
        )

    return None, {"status": "inactive", "age_sec": round(age_sec, 1)}


def _rule_gap_up_open(
    ticker: str,
    data: dict[str, Any],
    *,
    gap_threshold: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    gap_up_open rule: 始値が前日終値に対して gap_threshold 以上上昇した状態を検出する。

    判定式:
        gap_pct = (current_open - prev_close) / prev_close
        発火: gap_pct >= gap_threshold

    ガード（(None, diag) を返す）:
        - current_open が None → skipped / no_current_open
        - prev_close が None  → skipped / no_prev_close
        - prev_close == 0     → skipped / zero_prev_close

    score 計算:
        min(1.0, gap_pct / 0.04)  # 4% gap → score 1.0

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_open = data.get("current_open")
    prev_close = data.get("prev_close")

    if current_open is None:
        return None, {"status": "skipped", "reason": "no_current_open"}
    if prev_close is None:
        return None, {"status": "skipped", "reason": "no_prev_close"}
    if prev_close == 0:
        return None, {"status": "skipped", "reason": "zero_prev_close"}

    gap_pct = (current_open - prev_close) / prev_close
    if gap_pct < gap_threshold:
        return None, {"status": "inactive", "gap_pct": round(gap_pct, 6)}

    score = min(1.0, gap_pct / 0.04)  # 4% gap → score 1.0
    return (
        make(ticker, "gap_up_open", score, 0.9, {
            "gap_pct": round(gap_pct * 100, 3),
            "current_open": current_open,
            "prev_close": prev_close,
            "threshold_pct": gap_threshold * 100,
            "rule": "(current_open - prev_close) / prev_close >= 0.02",
        }),
        {"status": "active", "gap_pct": round(gap_pct, 6)},
    )


def _rule_gap_down_open(
    ticker: str,
    data: dict[str, Any],
    *,
    gap_threshold: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    gap_down_open rule: 始値が前日終値に対して gap_threshold 以上下落した状態を検出する。

    判定式:
        gap_pct = (current_open - prev_close) / prev_close
        発火: gap_pct <= -gap_threshold

    ガード（(None, diag) を返す）:
        - current_open が None → skipped / no_current_open
        - prev_close が None  → skipped / no_prev_close
        - prev_close == 0     → skipped / zero_prev_close

    score 計算:
        min(1.0, abs(gap_pct) / 0.04)  # 4% gap down → score 1.0

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_open = data.get("current_open")
    prev_close = data.get("prev_close")

    if current_open is None:
        return None, {"status": "skipped", "reason": "no_current_open"}
    if prev_close is None:
        return None, {"status": "skipped", "reason": "no_prev_close"}
    if prev_close == 0:
        return None, {"status": "skipped", "reason": "zero_prev_close"}

    gap_pct = (current_open - prev_close) / prev_close
    if gap_pct > -gap_threshold:
        return None, {"status": "inactive", "gap_pct": round(gap_pct, 6)}

    score = min(1.0, abs(gap_pct) / 0.04)  # 4% gap down → score 1.0
    return (
        make(ticker, "gap_down_open", score, 0.9, {
            "gap_pct": round(gap_pct * 100, 3),
            "current_open": current_open,
            "prev_close": prev_close,
            "threshold_pct": -gap_threshold * 100,
            "rule": "(current_open - prev_close) / prev_close <= -0.02",
        }),
        {"status": "active", "gap_pct": round(gap_pct, 6)},
    )


def _rule_symbol_trend_up(
    ticker: str,
    data: dict[str, Any],
    *,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    symbol_trend_up rule: price > VWAP かつ MA5 > MA20 のアップトレンドを検出する。

    発火条件:
        current_price > vwap  AND  ma5 > ma20

    ガード（(None, diag) を返す）:
        - current_price が None → skipped / no_current_price
        - vwap が None         → skipped / no_vwap
        - ma5 が None          → skipped / no_ma5
        - ma20 が None         → skipped / no_ma20
        - vwap <= 0            → skipped / zero_vwap
        - ma20 <= 0            → skipped / zero_ma20

    score 計算:
        vwap_diff = (current_price - vwap) / vwap
        ma_diff   = (ma5 - ma20) / ma20
        max(0.3, min(1.0, (vwap_diff + ma_diff) * 20))

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_price = data.get("current_price")
    vwap = data.get("vwap")
    ma5 = data.get("ma5")
    ma20 = data.get("ma20")

    if current_price is None:
        return None, {"status": "skipped", "reason": "no_current_price"}
    if vwap is None:
        return None, {"status": "skipped", "reason": "no_vwap"}
    if ma5 is None:
        return None, {"status": "skipped", "reason": "no_ma5"}
    if ma20 is None:
        return None, {"status": "skipped", "reason": "no_ma20"}
    if vwap <= 0:
        return None, {"status": "skipped", "reason": "zero_vwap"}
    if ma20 <= 0:
        return None, {"status": "skipped", "reason": "zero_ma20"}

    price_above_vwap = current_price > vwap
    ma5_above_ma20 = ma5 > ma20

    if not price_above_vwap or not ma5_above_ma20:
        return None, {
            "status": "inactive",
            "price_above_vwap": price_above_vwap,
            "ma5_above_ma20": ma5_above_ma20,
        }

    vwap_diff = (current_price - vwap) / vwap
    ma_diff = (ma5 - ma20) / ma20
    score = max(0.3, min(1.0, (vwap_diff + ma_diff) * 20))
    return (
        make(ticker, "symbol_trend_up", score, 0.75, {
            "current_price": current_price,
            "vwap": vwap,
            "ma5": ma5,
            "ma20": ma20,
            "price_above_vwap": True,
            "ma5_above_ma20": True,
            "rule": "price > vwap AND ma5 > ma20",
        }),
        {"status": "active", "vwap_diff": round(vwap_diff, 6), "ma_diff": round(ma_diff, 6)},
    )


def _rule_symbol_trend_down(
    ticker: str,
    data: dict[str, Any],
    *,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    symbol_trend_down rule: price < VWAP かつ MA5 < MA20 のダウントレンドを検出する。

    発火条件:
        current_price < vwap  AND  ma5 < ma20
        （price == vwap または ma5 == ma20 の場合は発火しない）

    ガード（(None, diag) を返す）:
        - current_price が None → skipped / no_current_price
        - vwap が None         → skipped / no_vwap
        - ma5 が None          → skipped / no_ma5
        - ma20 が None         → skipped / no_ma20
        - vwap <= 0            → skipped / zero_vwap
        - ma20 <= 0            → skipped / zero_ma20

    score 計算:
        vwap_diff = (vwap - current_price) / vwap
        ma_diff   = (ma20 - ma5) / ma20
        max(0.3, min(1.0, (vwap_diff + ma_diff) * 20))

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_price = data.get("current_price")
    vwap = data.get("vwap")
    ma5 = data.get("ma5")
    ma20 = data.get("ma20")

    if current_price is None:
        return None, {"status": "skipped", "reason": "no_current_price"}
    if vwap is None:
        return None, {"status": "skipped", "reason": "no_vwap"}
    if ma5 is None:
        return None, {"status": "skipped", "reason": "no_ma5"}
    if ma20 is None:
        return None, {"status": "skipped", "reason": "no_ma20"}
    if vwap <= 0:
        return None, {"status": "skipped", "reason": "zero_vwap"}
    if ma20 <= 0:
        return None, {"status": "skipped", "reason": "zero_ma20"}

    price_above_vwap = current_price > vwap
    ma5_above_ma20 = ma5 > ma20

    if price_above_vwap or ma5_above_ma20:
        return None, {
            "status": "inactive",
            "price_above_vwap": price_above_vwap,
            "ma5_above_ma20": ma5_above_ma20,
        }

    vwap_diff = (vwap - current_price) / vwap
    ma_diff = (ma20 - ma5) / ma20
    score = max(0.3, min(1.0, (vwap_diff + ma_diff) * 20))
    return (
        make(ticker, "symbol_trend_down", score, 0.75, {
            "current_price": current_price,
            "vwap": vwap,
            "ma5": ma5,
            "ma20": ma20,
            "price_above_vwap": False,
            "ma5_above_ma20": False,
            "rule": "price < vwap AND ma5 < ma20",
        }),
        {"status": "active", "vwap_diff": round(vwap_diff, 6), "ma_diff": round(ma_diff, 6)},
    )


def _rule_breakout_candidate(
    ticker: str,
    data: dict[str, Any],
    *,
    is_high_volume: bool,
    is_gap_up: bool,
    is_gap_down: bool,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    breakout_candidate rule: MA20 上抜け + 高出来高 + ギャップなし

    発火条件:
        current_price > ma20
        AND is_high_volume (出来高が平均の VOLUME_RATIO_HIGH 倍以上)
        AND NOT is_gap_up
        AND NOT is_gap_down

    ガード（(None, diag) を返す）:
        - current_price が None または <= 0 → skipped / no_current_price
        - ma20 が None または <= 0          → skipped / no_ma20

    依存引数（_evaluate_symbol() から渡される）:
        is_high_volume : 当日出来高が同時刻平均の VOLUME_RATIO_HIGH (2.0x) 以上
        is_gap_up      : 始値が前日比 GAP_THRESHOLD (2%) 以上
        is_gap_down    : 始値が前日比 -GAP_THRESHOLD (2%) 以下

    score 計算:
        max(0.3, min(1.0, pct_above_ma20 / 0.03))  # 3% above → score 1.0

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_price = data.get("current_price")
    ma20 = data.get("ma20")

    if current_price is None or current_price <= 0:
        return None, {"status": "skipped", "reason": "no_current_price"}
    if ma20 is None or ma20 <= 0:
        return None, {"status": "skipped", "reason": "no_ma20"}

    price_above_ma20 = current_price > ma20

    if not is_high_volume or is_gap_up or is_gap_down or not price_above_ma20:
        return None, {
            "status": "inactive",
            "is_high_volume": is_high_volume,
            "is_gap": is_gap_up or is_gap_down,
            "price_above_ma20": price_above_ma20,
        }

    pct_above_ma20 = (current_price - ma20) / ma20
    score = max(0.3, min(1.0, pct_above_ma20 / 0.03))  # 3% above → score 1.0
    return (
        make(ticker, "breakout_candidate", score, 0.7, {
            "current_price": current_price,
            "ma20": ma20,
            "price_above_ma20_pct": round(pct_above_ma20 * 100, 3),
            "is_high_volume": True,
            "rule": "price > ma20 AND high_relative_volume AND no_gap",
        }),
        {"status": "active", "pct_above_ma20": round(pct_above_ma20, 6)},
    )


def _rule_high_relative_volume(
    ticker: str,
    data: dict[str, Any],
    *,
    volume_ratio_high: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    high_relative_volume rule: 当日出来高が同時刻平均の volume_ratio_high 倍以上の状態を検出する。

    判定式:
        vol_ratio = current_volume / avg_volume_same_time
        発火: vol_ratio >= volume_ratio_high

    ガード（(None, diag) を返す）:
        - current_volume が None       → skipped / no_current_volume
        - avg_volume_same_time が None → skipped / no_avg_volume
        - avg_volume_same_time <= 0   → skipped / zero_avg_volume

    score 計算:
        min(1.0, vol_ratio / 4.0)  # 4x 平均 → score 1.0

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_volume = data.get("current_volume")
    avg_volume = data.get("avg_volume_same_time")

    if current_volume is None:
        return None, {"status": "skipped", "reason": "no_current_volume"}
    if avg_volume is None:
        return None, {"status": "skipped", "reason": "no_avg_volume"}
    if avg_volume <= 0:
        return None, {"status": "skipped", "reason": "zero_avg_volume"}

    vol_ratio = current_volume / avg_volume
    if vol_ratio < volume_ratio_high:
        return None, {"status": "inactive", "vol_ratio": round(vol_ratio, 3)}

    score = min(1.0, vol_ratio / 4.0)  # 4x 平均 → score 1.0
    return (
        make(ticker, "high_relative_volume", score, 0.85, {
            "volume_ratio": round(vol_ratio, 3),
            "current_volume": current_volume,
            "avg_volume_same_time": avg_volume,
            "threshold": volume_ratio_high,
            "rule": "current_volume / avg_volume_same_time >= 2.0",
        }),
        {"status": "active", "vol_ratio": round(vol_ratio, 3)},
    )


def _rule_low_liquidity(
    ticker: str,
    data: dict[str, Any],
    *,
    volume_ratio_low: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    low_liquidity rule: 当日出来高が同時刻平均の volume_ratio_low 倍未満の状態を検出する。

    判定式:
        vol_ratio = current_volume / avg_volume_same_time
        発火: vol_ratio < volume_ratio_low

    ガード（(None, diag) を返す）:
        - current_volume が None       → skipped / no_current_volume
        - avg_volume_same_time が None → skipped / no_avg_volume
        - avg_volume_same_time <= 0   → skipped / zero_avg_volume

    score 計算:
        max(0.1, 1.0 - vol_ratio / volume_ratio_low)

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_volume = data.get("current_volume")
    avg_volume = data.get("avg_volume_same_time")

    if current_volume is None:
        return None, {"status": "skipped", "reason": "no_current_volume"}
    if avg_volume is None:
        return None, {"status": "skipped", "reason": "no_avg_volume"}
    if avg_volume <= 0:
        return None, {"status": "skipped", "reason": "zero_avg_volume"}

    vol_ratio = current_volume / avg_volume
    if vol_ratio >= volume_ratio_low:
        return None, {"status": "inactive", "vol_ratio": round(vol_ratio, 3)}

    score = max(0.1, 1.0 - vol_ratio / volume_ratio_low)
    return (
        make(ticker, "low_liquidity", score, 0.8, {
            "volume_ratio": round(vol_ratio, 3),
            "current_volume": current_volume,
            "avg_volume_same_time": avg_volume,
            "threshold": volume_ratio_low,
            "rule": "current_volume / avg_volume_same_time < 0.3",
        }),
        {"status": "active", "vol_ratio": round(vol_ratio, 3)},
    )


def _rule_symbol_range(
    ticker: str,
    data: dict[str, Any],
    *,
    is_trend_up: bool,
    is_trend_down: bool,
    atr_ratio_high: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    symbol_range rule: トレンドなし かつ ATR が低水準の状態を検出する。

    発火条件:
        NOT is_trend_up
        AND NOT is_trend_down
        AND atr / current_price < atr_ratio_high

    ガード（(None, diag) を返す）:
        - current_price が None または <= 0 → skipped / no_current_price
        - atr が None                       → skipped / no_atr

    非発火:
        - is_trend_up または is_trend_down   → inactive / reason="trending"
        - atr_ratio >= atr_ratio_high        → inactive / atr_ratio を含む

    score 計算:
        max(0.1, 1.0 - atr_ratio / atr_ratio_high)
        ※ ATR が低いほど score が高い（レンジ相場として確度が上がる）

    依存引数（_evaluate_symbol() から渡される）:
        is_trend_up   : symbol_trend_up rule が active かどうか
        is_trend_down : symbol_trend_down rule が active かどうか

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_price = data.get("current_price")
    atr = data.get("atr")

    if current_price is None or current_price <= 0:
        return None, {"status": "skipped", "reason": "no_current_price"}
    if atr is None:
        return None, {"status": "skipped", "reason": "no_atr"}

    if is_trend_up or is_trend_down:
        return None, {
            "status": "inactive",
            "reason": "trending",
            "is_trend_up": is_trend_up,
            "is_trend_down": is_trend_down,
        }

    atr_ratio = atr / current_price
    if atr_ratio >= atr_ratio_high:
        return None, {"status": "inactive", "reason": "high_atr", "atr_ratio": round(atr_ratio, 6)}

    score = max(0.1, 1.0 - atr_ratio / atr_ratio_high)
    return (
        make(ticker, "symbol_range", score, 0.65, {
            "current_price": current_price,
            "atr": atr,
            "atr_ratio": round(atr_ratio, 6),
            "threshold": atr_ratio_high,
            "rule": "not trending AND atr / price < 0.02",
        }),
        {"status": "active", "atr_ratio": round(atr_ratio, 6)},
    )


def _rule_symbol_volatility_high(
    ticker: str,
    data: dict[str, Any],
    *,
    atr_ratio_high: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    symbol_volatility_high rule: ATR / 現在値 が高水準の状態を検出する。

    判定式:
        atr_ratio = atr / current_price
        発火: atr_ratio >= atr_ratio_high

    ガード（(None, diag) を返す）:
        - current_price が None または <= 0 → skipped / no_current_price
        - atr が None                       → skipped / no_atr

    score 計算:
        min(1.0, atr_ratio / 0.05)
        ※ ATR = 5% → score 1.0 を基準とする

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ガード発動時）
    """
    current_price = data.get("current_price")
    atr = data.get("atr")

    if current_price is None or current_price <= 0:
        return None, {"status": "skipped", "reason": "no_current_price"}
    if atr is None:
        return None, {"status": "skipped", "reason": "no_atr"}

    atr_ratio = atr / current_price
    if atr_ratio < atr_ratio_high:
        return None, {"status": "inactive", "atr_ratio": round(atr_ratio, 6)}

    score = min(1.0, atr_ratio / 0.05)  # 5% ATR → score 1.0
    return (
        make(ticker, "symbol_volatility_high", score, 0.8, {
            "current_price": current_price,
            "atr": atr,
            "atr_ratio": round(atr_ratio, 6),
            "threshold": atr_ratio_high,
            "rule": "atr / price >= 0.02",
        }),
        {"status": "active", "atr_ratio": round(atr_ratio, 6)},
    )


def _rule_quote_only(
    ticker: str,
    data: dict[str, Any],
    *,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    quote_only rule: 気配はあるが約定価格がない状態を検出する。

    active 条件:
        1. current_price is None（約定価格なし）
        2. best_bid is not None または best_ask is not None（気配あり）

    inactive 条件:
        - current_price が存在する → inactive / has_last_price
        - best_bid と best_ask が両方とも None → inactive / no_quotes

    score: 1.0 固定

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火時）
    """
    current_price = data.get("current_price")
    best_bid = data.get("best_bid")
    best_ask = data.get("best_ask")

    if current_price is not None:
        return None, {"status": "inactive", "reason": "has_last_price"}

    has_bid = best_bid is not None
    has_ask = best_ask is not None

    if not has_bid and not has_ask:
        return None, {"status": "inactive", "reason": "no_quotes"}

    return (
        make(ticker, "quote_only", 1.0, 1.0, {
            "reason": "quote_only",
            "current_price": current_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "has_bid": has_bid,
            "has_ask": has_ask,
        }),
        {"status": "active", "reason": "quote_only", "has_bid": has_bid, "has_ask": has_ask},
    )


def _rule_stale_bid_ask(
    ticker: str,
    data: dict[str, Any],
    *,
    evaluation_time: datetime,
    threshold_sec: float,
    make: _MakeFn,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """
    stale_bid_ask rule: bid/ask 気配の鮮度異常を検出する。

    Gate: "bid_ask_updated" キーが data に存在しない場合は評価しない。
    fetcher が bid_ask_updated を付与した data のみを対象とすることで、
    bid_ask_updated を含まない既存テストデータへの影響をゼロにする。

    active 条件:
        1. best_bid または best_ask が存在する（気配あり）
        2. bid_ask_updated is None  → reason = "missing_bid_ask_timestamp"
           または
           age_sec >= threshold_sec → reason = "stale_bid_ask"

    inactive 条件:
        - best_bid と best_ask が両方とも None → inactive / no_quotes
        - bid_ask_updated があり age_sec < threshold_sec → inactive / fresh_bid_ask

    score: 1.0 固定

    Returns:
        (StateEvaluationResult, diag)（発火時）または (None, diag)（非発火・ゲート時）
    """
    # Gate: bid_ask_updated キーが存在しない場合は評価しない
    if "bid_ask_updated" not in data:
        return None, {"status": "skipped", "reason": "no_bid_ask_updated_key"}

    best_bid = data.get("best_bid")
    best_ask = data.get("best_ask")
    bid_ask_updated: datetime | None = data["bid_ask_updated"]

    has_bid = best_bid is not None
    has_ask = best_ask is not None

    if not has_bid and not has_ask:
        return None, {"status": "inactive", "reason": "no_quotes"}

    # 気配あり: timestamp 未設定
    if bid_ask_updated is None:
        return (
            make(ticker, "stale_bid_ask", 1.0, 0.9, {
                "reason": "missing_bid_ask_timestamp",
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_ask_updated": None,
                "threshold_sec": threshold_sec,
                "has_bid": has_bid,
                "has_ask": has_ask,
            }),
            {"status": "active", "reason": "missing_bid_ask_timestamp"},
        )

    # 気配あり: timestamp あり → 鮮度チェック
    age_sec = (evaluation_time - bid_ask_updated).total_seconds()
    if age_sec >= threshold_sec:
        return (
            make(ticker, "stale_bid_ask", 1.0, 0.9, {
                "reason": "stale_bid_ask",
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_ask_updated": bid_ask_updated.isoformat(),
                "age_sec": round(age_sec, 1),
                "threshold_sec": threshold_sec,
                "has_bid": has_bid,
                "has_ask": has_ask,
            }),
            {"status": "active", "reason": "stale_bid_ask", "age_sec": round(age_sec, 1)},
        )

    return None, {"status": "inactive", "reason": "fresh_bid_ask", "age_sec": round(age_sec, 1)}


# ─── Rule 実行リスト ──────────────────────────────────────────────────────────
# 各エントリ: (state_code, caller)
# caller シグネチャ: (t, d, ev, dp, et) → (StateEvaluationResult | None, diag)
#   t  = ticker, d = data, ev = SymbolStateEvaluator instance,
#   dp = deps dict, et = evaluation_time
#
# ★ 新しい rule を追加するときは _rule_*() を定義してここに1行追加する。
#   依存フラグを提供する rule は _RULE_DEP_FLAGS にも追加する。
#   依存型 rule（他 rule 結果を使う rule）は依存元より後に置くこと。
_RULES: list[tuple[str, Any]] = [
    ("gap_up_open",          lambda t, d, ev, dp, et: _rule_gap_up_open(t, d, gap_threshold=ev.GAP_THRESHOLD, make=ev._make)),
    ("gap_down_open",        lambda t, d, ev, dp, et: _rule_gap_down_open(t, d, gap_threshold=ev.GAP_THRESHOLD, make=ev._make)),
    ("high_relative_volume", lambda t, d, ev, dp, et: _rule_high_relative_volume(t, d, volume_ratio_high=ev.VOLUME_RATIO_HIGH, make=ev._make)),
    ("low_liquidity",        lambda t, d, ev, dp, et: _rule_low_liquidity(t, d, volume_ratio_low=ev.VOLUME_RATIO_LOW, make=ev._make)),
    ("symbol_trend_up",      lambda t, d, ev, dp, et: _rule_symbol_trend_up(t, d, make=ev._make)),
    ("symbol_trend_down",    lambda t, d, ev, dp, et: _rule_symbol_trend_down(t, d, make=ev._make)),
    ("wide_spread",          lambda t, d, ev, dp, et: _rule_wide_spread(t, d, spread_threshold=ev.SPREAD_THRESHOLD, make=ev._make)),
    ("price_stale",          lambda t, d, ev, dp, et: _rule_price_stale(t, d, evaluation_time=et, threshold_sec=ev.PRICE_STALE_THRESHOLD_SEC, make=ev._make)),
    ("overextended",         lambda t, d, ev, dp, et: _rule_overextended(t, d, rsi_overbought=ev.RSI_OVERBOUGHT, rsi_oversold=ev.RSI_OVERSOLD, make=ev._make)),
    ("symbol_volatility_high", lambda t, d, ev, dp, et: _rule_symbol_volatility_high(t, d, atr_ratio_high=ev.ATR_RATIO_HIGH, make=ev._make)),
    ("symbol_range",         lambda t, d, ev, dp, et: _rule_symbol_range(t, d, is_trend_up=dp.get("is_trend_up", False), is_trend_down=dp.get("is_trend_down", False), atr_ratio_high=ev.ATR_RATIO_HIGH, make=ev._make)),
    ("breakout_candidate",   lambda t, d, ev, dp, et: _rule_breakout_candidate(t, d, is_high_volume=dp.get("is_high_volume", False), is_gap_up=dp.get("is_gap_up", False), is_gap_down=dp.get("is_gap_down", False), make=ev._make)),
    ("quote_only",           lambda t, d, ev, dp, et: _rule_quote_only(t, d, make=ev._make)),
    ("stale_bid_ask",        lambda t, d, ev, dp, et: _rule_stale_bid_ask(t, d, evaluation_time=et, threshold_sec=ev.BID_ASK_STALE_THRESHOLD_SEC, make=ev._make)),
]

# _RULE_REGISTRY は _RULES から自動導出する（手動で同期不要）
_RULE_REGISTRY: tuple[str, ...] = tuple(code for code, _ in _RULES)


class SymbolStateEvaluator(AbstractStateEvaluator):
    """
    銘柄の状態を評価する Evaluator。

    ctx.symbol_data が空の場合は空リストを返す（監視銘柄なし）。
    各銘柄に対して複数の状態が同時に有効となりうる（例: gap_up_open + high_relative_volume）。
    Engine の save_evaluations はグループ単位でソフト失効するため、
    同一 ticker の複数結果は正しく保存される。

    Rule 構造:
        module レベルの _RULES が実行定義の唯一の場所。
        _evaluate_symbol() は _RULES を iterate するだけの orchestrator。
        新しい rule を追加するには _rule_*() を定義して _RULES に1行追加する。
        他ルールの計算結果に依存する rule は _RULE_DEP_FLAGS にフラグを追加し、
        deps.get(flag, False) でキーワード引数として受け取る。
    """

    # ─── 閾値定数 ──────────────────────────────────────────────────────────────

    GAP_THRESHOLD: float = 0.02              # 2%: gap up / gap down 判定
    VOLUME_RATIO_HIGH: float = 2.0           # 200%: high_relative_volume
    VOLUME_RATIO_LOW: float = 0.3            # 30%: low_liquidity
    SPREAD_THRESHOLD: float = 0.003          # 0.3%: wide_spread
    ATR_RATIO_HIGH: float = 0.02             # 2%: symbol_volatility_high / range 境界
    RSI_OVERBOUGHT: float = 75.0             # RSI >= 75: overextended (overbought)
    RSI_OVERSOLD: float = 25.0               # RSI <= 25: overextended (oversold)
    PRICE_STALE_THRESHOLD_SEC: float = 60.0      # 60秒: price_stale 判定閾値
    BID_ASK_STALE_THRESHOLD_SEC: float = 60.0   # 60秒: stale_bid_ask 判定閾値

    @property
    def name(self) -> str:
        return "SymbolStateEvaluator"

    def evaluate(self, ctx: EvaluationContext) -> list[StateEvaluationResult]:
        """
        symbol_data に含まれる全銘柄を評価する。

        Phase G: 評価後に ctx.rule_diagnostics_by_ticker[ticker] へ rule 診断を書き込む。
        engine が _update_symbol_snapshots() で state_summary_json["rule_diagnostics"] に含める。

        Returns:
            全銘柄の StateEvaluationResult リスト（空データなら []）
        """
        if not ctx.symbol_data:
            return []

        results: list[StateEvaluationResult] = []
        for ticker, data in ctx.symbol_data.items():
            try:
                symbol_results, rule_diagnostics = self._evaluate_symbol(
                    ticker, data, ctx.evaluation_time
                )
                # Phase C+1: 前サイクルから継続中の状態には is_new_activation=False を設定
                prev_active = ctx.prev_active_states_by_ticker.get(ticker, set())
                for r in symbol_results:
                    if r.state_code in prev_active:
                        r.is_new_activation = False
                results.extend(symbol_results)
                # Phase G: rule 診断を ctx に書き込む（engine がスナップショットに含める）
                ctx.rule_diagnostics_by_ticker[ticker] = rule_diagnostics
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
        self, ticker: str, data: dict[str, Any], evaluation_time: datetime
    ) -> tuple[list[StateEvaluationResult], dict[str, dict[str, Any]]]:
        """1銘柄の全状態を評価して返す。

        Returns:
            (results, rule_diagnostics):
                results          : この銘柄の StateEvaluationResult リスト
                rule_diagnostics : 各 rule の診断サマリ {state_code → {status, ...}}
        """
        results: list[StateEvaluationResult] = []
        rule_diagnostics: dict[str, dict[str, Any]] = {}
        deps: dict[str, bool] = {}  # 前ルールの active/inactive フラグ（後続依存型 rule に渡す）

        # データ抽出は全て各 _rule_*() 関数内で行う（このメソッド内に直接データアクセスは置かない）
        # rule 定義は module レベルの _RULES を参照。新しい rule は _rule_*() 定義 + _RULES に1行追加する。
        for _state_code, _caller in _RULES:
            _result, _diag = _caller(ticker, data, self, deps, evaluation_time)
            if _result is not None:
                results.append(_result)
            rule_diagnostics[_state_code] = _diag
            if _state_code in _RULE_DEP_FLAGS:
                deps[_RULE_DEP_FLAGS[_state_code]] = _result is not None

        return results, rule_diagnostics
