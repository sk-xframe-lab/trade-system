"""
MarketStateEngine — 市場状態評価エンジン

評価器（Evaluator）を順に実行し、結果を DB に永続化する。
売買判断は行わない。状態コードと証拠を記録することのみが責務。

使用方法:
    engine = MarketStateEngine(db)
    ctx = EvaluationContext(evaluation_time=datetime.now(timezone.utc))
    await engine.run(ctx)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.services.market_state.evaluator_base import AbstractStateEvaluator
from trade_app.services.market_state.market_evaluator import MarketStateEvaluator
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult
from trade_app.services.market_state.symbol_evaluator import SymbolStateEvaluator
from trade_app.services.market_state.time_window_evaluator import TimeWindowStateEvaluator

logger = logging.getLogger(__name__)

# ─── Phase R: priority ベース通知定義 ─────────────────────────────────────────
#
# Priority 1: 新規 activated で無条件通知
# Priority 2: 新規 activated かつ state 別条件を満たした場合のみ通知
# 未定義: 通知しない
#
# Phase O で追加した NOTIFIABLE_STATE_CODES は Phase R で非使用化。
# 後方互換のため定数は残すが、extract_notification_candidates() は参照しない。
NOTIFIABLE_STATE_CODES: frozenset[str] = frozenset({
    "wide_spread",
    "price_stale",
    "breakout_candidate",
})

STATE_NOTIFICATION_PRIORITY: dict[str, int] = {
    "wide_spread":          1,
    "price_stale":          1,
    "stale_bid_ask":        1,
    "breakout_candidate":   2,
    "quote_only":           2,
}


def _check_priority2_condition(r: StateEvaluationResult) -> bool:
    """
    Priority 2 state の通知条件を評価する。

    breakout_candidate: score >= 0.8
    quote_only:         best_bid is not None AND best_ask is not None
    """
    ev = r.evidence
    if r.state_code == "breakout_candidate":
        return r.score >= 0.8
    if r.state_code == "quote_only":
        return ev.get("best_bid") is not None and ev.get("best_ask") is not None
    return False


def extract_notification_candidates(
    symbol_results: list[StateEvaluationResult],
    evaluation_time: datetime,
) -> list[dict]:
    """
    activated かつ STATE_NOTIFICATION_PRIORITY に定義された state を通知 payload のリストとして返す。

    抽出条件:
      1. is_new_activation == True
      2. STATE_NOTIFICATION_PRIORITY に state_code が存在する
      3. priority 1 → 無条件採用
         priority 2 → state 別条件を満たした場合のみ採用

    それ以外（continued / deactivated / 未定義 / 条件不一致）は無視する。
    """
    candidates = []
    for r in symbol_results:
        if not r.is_new_activation:
            continue

        priority = STATE_NOTIFICATION_PRIORITY.get(r.state_code)
        if priority is None:
            continue

        if priority == 2 and not _check_priority2_condition(r):
            continue

        ev = r.evidence
        payload: dict = {
            "ticker":           r.target_code,
            "state_code":       r.state_code,
            "evaluation_time":  evaluation_time,
            "priority":         priority,
            "reason":           ev.get("reason"),
            "score":            r.score,
        }

        if r.state_code == "wide_spread":
            payload.update({
                "spread":           ev.get("spread"),
                "spread_rate":      ev.get("spread_rate"),
                "current_price":    ev.get("current_price"),
            })

        elif r.state_code == "price_stale":
            payload.update({
                "last_updated":     ev.get("last_updated"),
                "age_sec":          ev.get("age_sec"),
                "threshold_sec":    ev.get("threshold_sec"),
            })

        elif r.state_code == "stale_bid_ask":
            payload.update({
                "bid_ask_updated":  ev.get("bid_ask_updated"),
                "age_sec":          ev.get("age_sec"),
                "threshold_sec":    ev.get("threshold_sec"),
                "best_bid":         ev.get("best_bid"),
                "best_ask":         ev.get("best_ask"),
            })

        elif r.state_code == "breakout_candidate":
            pass  # score / reason は共通フィールドで提供済み

        elif r.state_code == "quote_only":
            payload.update({
                "best_bid":         ev.get("best_bid"),
                "best_ask":         ev.get("best_ask"),
                "current_price":    ev.get("current_price"),
            })

        candidates.append(payload)

    return candidates


# ─── Phase S: execution guard hints ──────────────────────────────────────────

_GUARD_BLOCKING_STATES: frozenset[str] = frozenset({"price_stale", "stale_bid_ask", "quote_only"})
_GUARD_WARNING_STATES: frozenset[str] = frozenset({"wide_spread"})


def _build_execution_guard_hints(active_state_codes: list[str]) -> dict:
    """
    active state 集合から execution_guard_hints を生成する。

    blocking_reasons: active state のうち _GUARD_BLOCKING_STATES に含まれるもの（ソート済み）
    warning_reasons:  active state のうち _GUARD_WARNING_STATES  に含まれるもの（ソート済み）
    has_quote_risk:   blocking または warning が1件でもあれば True
    """
    active = set(active_state_codes)
    blocking = sorted(active & _GUARD_BLOCKING_STATES)
    warning  = sorted(active & _GUARD_WARNING_STATES)
    return {
        "has_quote_risk":   bool(blocking or warning),
        "blocking_reasons": blocking,
        "warning_reasons":  warning,
    }


def dispatch_notifications(candidates: list[dict]) -> None:
    """
    通知候補を各通知先に送る。

    既存通知経路がある場合はそこに流す。
    失敗は必ず握りつぶす（run 全体を失敗させない）。
    """
    for c in candidates:
        try:
            logger.info("[NOTIFY] %s", c)
        except Exception:
            pass


class MarketStateEngine:
    """
    登録された Evaluator を実行し、結果を永続化する。

    Phase 1 デフォルト Evaluator:
      - TimeWindowStateEvaluator: 時間帯状態
      - MarketStateEvaluator:     市場トレンド状態
      - SymbolStateEvaluator:     銘柄状態（ctx.symbol_data が空の場合はスキップ）
    """

    def __init__(
        self,
        db: AsyncSession,
        evaluators: list[AbstractStateEvaluator] | None = None,
    ) -> None:
        self._db = db
        self._repo = MarketStateRepository(db)
        self._evaluators: list[AbstractStateEvaluator] = evaluators or [
            TimeWindowStateEvaluator(),
            MarketStateEvaluator(),
            SymbolStateEvaluator(),
        ]

    # ─── メイン実行 ────────────────────────────────────────────────────────────

    async def run(self, ctx: EvaluationContext) -> list[StateEvaluationResult]:
        """
        全 Evaluator を実行し、結果を DB に保存する。

        Phase C+1: symbol 状態は遷移ベース保存（activated のみ INSERT / deactivated のみ soft-expire）。
        non-symbol 状態は従来通り全件 soft-expire + INSERT。

        Args:
            ctx: 評価コンテキスト（evaluation_time と任意の market_data を含む）

        Returns:
            全 Evaluator が返した StateEvaluationResult のリスト
        """
        evaluation_time = ctx.evaluation_time
        if evaluation_time.tzinfo is None:
            evaluation_time = evaluation_time.replace(tzinfo=timezone.utc)

        # Phase C+1: symbol の前サイクル active 状態を ctx に注入（evaluator が使用）
        await self._load_prev_active_states(ctx)

        all_results: list[StateEvaluationResult] = []

        # ─── 各 Evaluator を実行 ───────────────────────────────────────────
        for evaluator in self._evaluators:
            try:
                results = evaluator.evaluate(ctx)
                all_results.extend(results)
                logger.debug(
                    "MarketStateEngine: evaluator=%s produced %d result(s)",
                    evaluator.name, len(results),
                )
            except Exception as exc:
                logger.error(
                    "MarketStateEngine: evaluator=%s raised %s — skipping",
                    evaluator.name, exc, exc_info=True,
                )

        # symbol / non-symbol に分割
        non_symbol = [r for r in all_results if r.layer != "symbol"]
        symbol_results = [r for r in all_results if r.layer == "symbol"]

        if not non_symbol and not symbol_results and not ctx.symbol_data:
            logger.warning("MarketStateEngine: no evaluation results produced")
            return []

        # ─── non-symbol: 従来通り全件 soft-expire + INSERT ─────────────────
        if non_symbol:
            await self._repo.save_evaluations(non_symbol, evaluation_time)

        # ─── Phase O: activated state の通知 ─────────────────────────────────
        try:
            candidates = extract_notification_candidates(symbol_results, evaluation_time)
            dispatch_notifications(candidates)
        except Exception as exc:
            logger.error(
                "MarketStateEngine: 通知処理エラー: %s — 無視して継続",
                exc, exc_info=True,
            )

        # ─── symbol: 遷移ベース保存（activated INSERT / deactivated soft-expire）
        await self._save_symbol_transitions(symbol_results, ctx, evaluation_time)

        # ─── スナップショットを更新 ────────────────────────────────────────
        if non_symbol:
            await self._update_snapshots(non_symbol, evaluation_time)
        await self._update_symbol_snapshots(symbol_results, ctx, evaluation_time)

        await self._db.commit()

        logger.info(
            "MarketStateEngine: run complete — %d result(s) at %s",
            len(all_results), evaluation_time.isoformat(),
        )
        return all_results

    # ─── Phase C+1: 前サイクル状態ロード ──────────────────────────────────────

    async def _load_prev_active_states(self, ctx: EvaluationContext) -> None:
        """
        ctx.symbol_data の全 ticker について snapshot から前サイクルの active 状態を取得し、
        ctx.prev_active_states_by_ticker に設定する。

        snapshot が存在しない ticker は空 set（= 初回扱い、全状態が新規活性化）。
        """
        for ticker in ctx.symbol_data:
            snapshot = await self._repo.get_symbol_snapshot(ticker)
            if snapshot is not None and snapshot.active_states_json:
                ctx.prev_active_states_by_ticker[ticker] = set(snapshot.active_states_json)
            else:
                ctx.prev_active_states_by_ticker[ticker] = set()

    # ─── Phase C+1: symbol 遷移保存 ───────────────────────────────────────────

    async def _save_symbol_transitions(
        self,
        symbol_results: list[StateEvaluationResult],
        ctx: EvaluationContext,
        evaluation_time: datetime,
    ) -> None:
        """
        symbol 状態の遷移ベース保存。

        - inactive→active (activated): INSERT
        - active→active (continuation): 何もしない
        - active→inactive (deactivated): soft-expire のみ
        - inactive→inactive: 何もしない
        """
        activated = [r for r in symbol_results if r.is_new_activation]

        # ticker ごとに deactivated state_code を収集
        deactivated_by_target: dict[tuple[str, str, str | None], set[str]] = {}
        for ticker in ctx.symbol_data:
            prev_active = ctx.prev_active_states_by_ticker.get(ticker, set())
            if not prev_active:
                continue
            current_codes = {r.state_code for r in symbol_results if r.target_code == ticker}
            deactivated = prev_active - current_codes
            if deactivated:
                deactivated_by_target[("symbol", "symbol", ticker)] = deactivated

        await self._repo.save_evaluations_transitioned(
            activated, deactivated_by_target, evaluation_time
        )

    # ─── Phase C+1: symbol スナップショット更新 ───────────────────────────────

    async def _update_symbol_snapshots(
        self,
        symbol_results: list[StateEvaluationResult],
        ctx: EvaluationContext,
        evaluation_time: datetime,
    ) -> None:
        """
        ctx.symbol_data の全 ticker のスナップショットを UPSERT する。

        active 状態がない ticker も updated_at を更新する（stale 検出のため）。
        """
        # ticker → active state_codes マップを構築
        states_by_ticker: dict[str, list[StateEvaluationResult]] = {}
        for r in symbol_results:
            if r.target_code is not None:
                states_by_ticker.setdefault(r.target_code, []).append(r)

        for ticker in ctx.symbol_data:
            group = states_by_ticker.get(ticker, [])
            active_states = [r.state_code for r in group]
            rule_diag = ctx.rule_diagnostics_by_ticker.get(ticker, {})

            guard_hints = _build_execution_guard_hints(active_states)

            if group:
                primary = group[0]
                summary = {
                    "primary_state": primary.state_code,
                    "score": primary.score,
                    "confidence": primary.confidence,
                    "evaluated_at": evaluation_time.isoformat(),
                    "state_count": len(group),
                    "rule_diagnostics": rule_diag,
                    "execution_guard_hints": guard_hints,
                }
            else:
                summary = {
                    "primary_state": None,
                    "evaluated_at": evaluation_time.isoformat(),
                    "state_count": 0,
                    "rule_diagnostics": rule_diag,
                    "execution_guard_hints": guard_hints,
                }

            await self._repo.upsert_snapshot(
                layer="symbol",
                target_type="symbol",
                target_code=ticker,
                active_state_codes=active_states,
                summary=summary,
            )

    # ─── スナップショット更新（non-symbol 用） ─────────────────────────────────

    async def _update_snapshots(
        self,
        results: list[StateEvaluationResult],
        evaluation_time: datetime,
    ) -> None:
        """
        結果を layer/target ごとにグループ化してスナップショットを UPSERT する。
        """
        # layer + target_type + target_code をキーにグループ化
        groups: dict[tuple[str, str, str | None], list[StateEvaluationResult]] = {}
        for r in results:
            key = (r.layer, r.target_type, r.target_code)
            groups.setdefault(key, []).append(r)

        for (layer, target_type, target_code), group in groups.items():
            active_states = [r.state_code for r in group]

            # 最初の結果をプライマリとしてサマリーを構築
            primary = group[0]
            summary = {
                "primary_state": primary.state_code,
                "score": primary.score,
                "confidence": primary.confidence,
                "evaluated_at": evaluation_time.isoformat(),
                "evaluator_count": len(group),
            }

            await self._repo.upsert_snapshot(
                layer=layer,
                target_type=target_type,
                target_code=target_code,
                active_state_codes=active_states,
                summary=summary,
            )
