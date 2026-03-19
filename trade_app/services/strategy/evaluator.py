"""
StrategyEvaluator — 純粋な判定ロジック

DB アクセスなし。StrategyDefinition + conditions + active state codes → StrategyDecisionResult。

判定ルール (Phase 1):
  required_state  : 指定 state が active_states_by_layer[layer] に存在するか
  forbidden_state : 指定 state が active なら entry 禁止
  size_modifier   : 指定 state が active なら size_ratio に反映（複数の場合は最小値）

entry_allowed:
  - strategy.is_enabled = True
  - missing_required が 0 件
  - matched_forbidden が 0 件
  - pre_blocking_reasons が空（snapshot missing/stale チェック）

設計制約:
  StrategyEvaluator は発注しない。
  BrokerAdapter / OrderRouter / PositionManager には依存しない。
"""
from __future__ import annotations

import logging
from datetime import datetime

from trade_app.models.strategy_condition import StrategyCondition
from trade_app.models.strategy_definition import StrategyDefinition
from trade_app.services.strategy.schemas import StrategyDecisionResult

logger = logging.getLogger(__name__)


class StrategyEvaluator:
    """
    1つの StrategyDefinition を評価して StrategyDecisionResult を返す。
    純粋関数的実装（副作用なし、DB アクセスなし）。
    """

    def evaluate(
        self,
        strategy: StrategyDefinition,
        conditions: list[StrategyCondition],
        active_states_by_layer: dict[str, list[str]],
        ticker: str | None,
        evaluation_time: datetime,
        pre_blocking_reasons: list[str] | None = None,
    ) -> StrategyDecisionResult:
        """
        Strategy を評価して StrategyDecisionResult を返す。

        Args:
            strategy: StrategyDefinition
            conditions: この strategy の条件リスト
            active_states_by_layer: 現在アクティブな状態 {"market": [...], "time_window": [...], ...}
            ticker: 銘柄コード（None = 銘柄横断評価）
            evaluation_time: 評価時刻
            pre_blocking_reasons: snapshot missing/stale など事前ブロック理由

        Returns:
            StrategyDecisionResult
        """
        blocking_reasons: list[str] = list(pre_blocking_reasons or [])

        # ─── strategy 有効チェック ─────────────────────────────────────
        if not strategy.is_enabled:
            blocking_reasons.append("strategy_disabled")

        # ─── 条件を種別に分類 ──────────────────────────────────────────
        required_conds = [c for c in conditions if c.condition_type == "required_state"]
        forbidden_conds = [c for c in conditions if c.condition_type == "forbidden_state"]
        size_mod_conds = [c for c in conditions if c.condition_type == "size_modifier"]

        matched_required: list[str] = []
        missing_required: list[str] = []
        matched_forbidden: list[str] = []

        # ─── required_state チェック ───────────────────────────────────
        for cond in required_conds:
            layer_states = active_states_by_layer.get(cond.layer, [])
            key = f"{cond.layer}:{cond.state_code}"
            if cond.state_code in layer_states:
                matched_required.append(key)
            else:
                missing_required.append(key)
                blocking_reasons.append(f"missing_required_state:{cond.layer}:{cond.state_code}")

        # ─── forbidden_state チェック ──────────────────────────────────
        for cond in forbidden_conds:
            layer_states = active_states_by_layer.get(cond.layer, [])
            if cond.state_code in layer_states:
                key = f"{cond.layer}:{cond.state_code}"
                matched_forbidden.append(key)
                blocking_reasons.append(f"forbidden_state:{cond.layer}:{cond.state_code}")

        # ─── size_modifier 計算 ────────────────────────────────────────
        # 複数の size_modifier が成立する場合は最小値（保守的）を採用
        applied_modifiers: list[float] = []
        for cond in size_mod_conds:
            layer_states = active_states_by_layer.get(cond.layer, [])
            if cond.state_code in layer_states and cond.size_modifier is not None:
                applied_modifiers.append(cond.size_modifier)

        applied_size_modifier = min(applied_modifiers) if applied_modifiers else 1.0
        size_ratio = strategy.max_size_ratio * applied_size_modifier

        # ─── entry_allowed 最終判定 ────────────────────────────────────
        # pre_blocking_reasons が空 AND strategy 有効 AND required 全成立 AND forbidden 0件
        has_pre_block = bool(pre_blocking_reasons)
        entry_allowed = (
            strategy.is_enabled
            and not has_pre_block
            and len(missing_required) == 0
            and len(matched_forbidden) == 0
        )

        if not entry_allowed:
            size_ratio = 0.0

        # ─── size_ratio=0 の安全チェック ──────────────────────────────
        # size_modifier=0.0 や max_size_ratio=0.0 の場合、条件が揃っていても
        # size_ratio が 0 になることがある。発注サイズ 0 で entry_allowed=True は
        # Signal Router との接続時に曖昧なので安全側へ倒す。
        if entry_allowed and size_ratio <= 0.0:
            entry_allowed = False
            blocking_reasons.append("size_ratio_zero")

        logger.debug(
            "StrategyEvaluator: strategy=%s ticker=%s entry_allowed=%s blocking=%s",
            strategy.strategy_code, ticker, entry_allowed, blocking_reasons,
        )

        return StrategyDecisionResult(
            strategy_id=strategy.id,
            strategy_code=strategy.strategy_code,
            strategy_name=strategy.strategy_name,
            ticker=ticker,
            evaluation_time=evaluation_time,
            is_active=entry_allowed,
            entry_allowed=entry_allowed,
            size_ratio=size_ratio,
            matched_required_states=matched_required,
            matched_forbidden_states=matched_forbidden,
            missing_required_states=missing_required,
            blocking_reasons=blocking_reasons,
            applied_size_modifier=applied_size_modifier,
            evidence={
                "strategy_code": strategy.strategy_code,
                "direction": strategy.direction,
                "priority": strategy.priority,
                "active_states_by_layer": {k: v for k, v in active_states_by_layer.items()},
                "ticker": ticker,
            },
        )
