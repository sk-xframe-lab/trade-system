"""
Planning Layer 理由コード定義

PlanningReasonCode: 縮小・拒否の個別理由（signal_plan_reasons に記録）
PlanningStatus: signal_plans の最終ステータス
"""
from __future__ import annotations

import enum


class PlanningReasonCode(str, enum.Enum):
    """
    Planning stage での縮小・拒否理由コード。
    signal_plan_reasons.reason_code として記録される。
    """
    # ─── strategy decision 検証 ────────────────────────────────────────────
    DECISION_MISSING = "decision_missing"           # strategy decision が存在しない
    DECISION_STALE = "decision_stale"               # strategy decision が古すぎる

    # ─── 市場・銘柄状態 ────────────────────────────────────────────────────
    MARKET_NOT_TRADABLE = "market_not_tradable"     # 市場が取引不可状態
    SYMBOL_NOT_TRADABLE = "symbol_not_tradable"     # 銘柄が取引不可状態（売買停止等）

    # ─── 流動性・スプレッド ───────────────────────────────────────────────
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"  # 出来高が低すぎる
    SPREAD_TOO_WIDE = "spread_too_wide"             # スプレッドが広すぎる

    # ─── ボラティリティ・ATR ──────────────────────────────────────────────
    VOLATILITY_TOO_HIGH = "volatility_too_high"     # ヒストリカル・ボラが高すぎる
    ATR_TOO_HIGH = "atr_too_high"                   # ATR が高すぎる（急騰急落）

    # ─── サイズ・ロット ────────────────────────────────────────────────────
    LOT_SIZE_BELOW_MIN = "lot_size_below_min"       # lot 丸め後が最小単元未満
    PLANNED_SIZE_ZERO = "planned_size_zero"         # 計画発注数量がゼロ

    # ─── 資金 ─────────────────────────────────────────────────────────────
    BUYING_POWER_UNAVAILABLE = "buying_power_unavailable"  # 買付余力が不足

    # ─── Phase V: execution hard guard ────────────────────────────────────────
    EXECUTION_GUARD_PRICE_STALE = "execution_guard_price_stale"  # price_stale hard guard

    # ─── サイズ調整（縮小理由） ───────────────────────────────────────────
    SIZE_RATIO_APPLIED = "size_ratio_applied"       # strategy size_ratio による縮小
    LIQUIDITY_REDUCTION = "liquidity_reduction"     # 流動性による縮小
    SPREAD_REDUCTION = "spread_reduction"           # スプレッドによる縮小
    VOLATILITY_REDUCTION = "volatility_reduction"   # ボラティリティによる縮小
    ATR_REDUCTION = "atr_reduction"                 # ATR による縮小


class PlanningStatus(str, enum.Enum):
    """
    signal_plans.planning_status の値。
    planning 全体の最終判定結果を表す。
    """
    ACCEPTED = "accepted"   # 変更なし、または許容範囲内の縮小で受け入れ
    REDUCED = "reduced"     # 縮小あり、ただし最小サイズ以上で発注可
    REJECTED = "rejected"   # 発注不可（サイズ不足・tradable でない等）


# ─── Phase W: shadow hard guard trace 用定数 ─────────────────────────────────
# これは reject reason 用ではなく shadow trace 用として使う。
# PlanningReasonCode enum に含めない（signal_plan_reasons には記録されない）。
EXECUTION_GUARD_STALE_BID_ASK_SHADOW = "execution_guard_stale_bid_ask_shadow"
