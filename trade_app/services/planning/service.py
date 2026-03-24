"""
SignalPlanningService — Signal Planning Layer オーケストレーター

責務:
  1. Signal / decision 検証（defense in depth）
  2. ベースサイズ計算
  3. strategy size_ratio 適用
  4. 市場・銘柄 tradability チェック
  5. liquidity / spread / ATR / volatility による縮小
  6. lot size 丸め
  7. 0 または minimum lot 未満なら reject
  8. execution params 生成
  9. signal_plans 保存
  10. signal_plan_reasons 保存（縮小・拒否理由）

設計制約（必須）:
  - BrokerAdapter を直接呼ばない
  - OrderRouter を直接呼ばない
  - PositionManager を直接呼ばない
  - RiskManager を置き換えない（前段計画層として動く）
  - 発注確定はしない

適用範囲:
  - signal_type = "entry" の Signal に適用
  - signal_type = "exit" はサイズ変更なし（bypass）で accepted を返す
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.config import get_settings
from trade_app.models.signal import TradeSignal
from trade_app.models.signal_plan import SignalPlan
from trade_app.models.signal_plan_reason import SignalPlanReason
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.planning.adjusters import (
    AdjustmentResult,
    LiquidityAdjuster,
    MarketTradabilityChecker,
    SpreadAdjuster,
    VolatilityAdjuster,
)
from trade_app.services.planning.context import PlannerContext
from trade_app.services.planning.execution_params import ExecutionParamsBuilder
from trade_app.services.planning.reasons import PlanningReasonCode, PlanningStatus
from trade_app.services.planning.sizer import BaseSizer

logger = logging.getLogger(__name__)

# signal_strategy_decisions の最大許容古さ（stale 判定用）
# config.SIGNAL_MAX_DECISION_AGE_SEC と同じ設定を参照する
def _get_max_decision_age_sec() -> int:
    return get_settings().SIGNAL_MAX_DECISION_AGE_SEC


class SignalPlanRejectedError(Exception):
    """Planning Layer がシグナルを拒否した場合に送出"""

    def __init__(
        self,
        plan_id: str,
        reason_code: PlanningReasonCode,
        detail: str,
    ) -> None:
        self.plan_id = plan_id
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"planning rejected: {reason_code.value}: {detail}")


class SignalPlanningService:
    """
    Signal Planning Layer。

    PlannerContext を受け取り、発注可否・サイズ・執行パラメータ案を計算して
    signal_plans / signal_plan_reasons に保存する。

    決して発注しない。BrokerAdapter / OrderRouter には依存しない。
    """

    def __init__(self, db: AsyncSession, audit: AuditLogger) -> None:
        self._db = db
        self._audit = audit
        self._sizer = BaseSizer()
        self._tradability = MarketTradabilityChecker()
        self._liquidity = LiquidityAdjuster()
        self._spread = SpreadAdjuster()
        self._volatility = VolatilityAdjuster()
        self._params_builder = ExecutionParamsBuilder()

    async def plan(
        self,
        signal: TradeSignal,
        ctx: PlannerContext,
    ) -> SignalPlan:
        """
        シグナルの執行計画を立案し signal_plans に保存する。

        - accepted / reduced → SignalPlan を返す
        - rejected → SignalPlan を保存してから SignalPlanRejectedError を送出

        Args:
            signal: 対象シグナル
            ctx: PlannerContext（PlannerContextBuilder で構築済み）

        Returns:
            SignalPlan（accepted / reduced）

        Raises:
            SignalPlanRejectedError: 発注不可と判定した場合
        """
        now = datetime.now(timezone.utc)

        # exit signal は planning をバイパス（ゲートと同じ安全設計）
        if signal.signal_type != "entry":
            return await self._bypass_plan(signal, ctx, now)

        trace: list[dict[str, Any]] = []
        reasons: list[tuple[PlanningReasonCode, str, dict, float | None, float | None]] = []

        # ─── Step 1: Strategy Decision 検証 ──────────────────────────────
        rejection = self._validate_decision(ctx, now)
        if rejection:
            plan = await self._save_plan(
                signal=signal,
                ctx=ctx,
                status=PlanningStatus.REJECTED,
                planned_qty=0,
                exec_params=None,
                rejection_reason_code=rejection[0],
                trace=[{"stage": "decision_validation", "rejected": True,
                        "reason_code": rejection[0].value, "reason_detail": rejection[1]}],
                reasons=[(rejection[0], rejection[1],
                          {"signal_strategy_decision_id": ctx.signal_strategy_decision_id}, None, None)],
                now=now,
            )
            raise SignalPlanRejectedError(plan.id, rejection[0], rejection[1])

        # ─── Step 2: ベースサイズ + strategy size_ratio 適用 ────────────
        size_result = self._sizer.calculate(ctx.base_quantity, ctx.size_ratio)
        current_qty = size_result.after_ratio_qty

        base_entry = {
            "stage": "base_size",
            "base_qty": size_result.base_qty,
            "size_ratio": size_result.applied_size_ratio,
            "after_ratio_qty": current_qty,
        }
        trace.append(base_entry)

        if size_result.applied_size_ratio < 1.0:
            reasons.append((
                PlanningReasonCode.SIZE_RATIO_APPLIED,
                f"strategy size_ratio={ctx.size_ratio:.2f} 適用 qty {size_result.base_qty}→{current_qty}",
                {"base_qty": size_result.base_qty, "size_ratio": ctx.size_ratio},
                float(size_result.base_qty),
                float(current_qty),
            ))

        # ─── Step 3: 市場・銘柄 tradability チェック ─────────────────────
        tradability = self._tradability.check(current_qty, ctx)
        trace.append(tradability.as_trace_entry())
        if tradability.rejected:
            plan = await self._save_plan(
                signal=signal, ctx=ctx,
                status=PlanningStatus.REJECTED,
                planned_qty=0,
                exec_params=None,
                rejection_reason_code=tradability.reason_code,
                trace=trace,
                reasons=[(tradability.reason_code, tradability.reason_detail or "",
                          {}, float(current_qty), 0.0)],
                now=now,
            )
            raise SignalPlanRejectedError(
                plan.id, tradability.reason_code, tradability.reason_detail or ""
            )

        # ─── Step 4: Liquidity 調整 ───────────────────────────────────────
        liquidity = self._liquidity.adjust(current_qty, ctx)
        trace.append(liquidity.as_trace_entry())
        current_qty, reasons = self._apply_adjustment(liquidity, current_qty, reasons)
        if liquidity.rejected:
            plan = await self._save_plan(
                signal=signal, ctx=ctx,
                status=PlanningStatus.REJECTED,
                planned_qty=0,
                exec_params=None,
                rejection_reason_code=liquidity.reason_code,
                trace=trace,
                reasons=reasons,
                now=now,
            )
            raise SignalPlanRejectedError(
                plan.id, liquidity.reason_code, liquidity.reason_detail or ""
            )

        # ─── Step 5: Spread 調整 ─────────────────────────────────────────
        spread = self._spread.adjust(current_qty, ctx)
        trace.append(spread.as_trace_entry())
        current_qty, reasons = self._apply_adjustment(spread, current_qty, reasons)
        if spread.rejected:
            plan = await self._save_plan(
                signal=signal, ctx=ctx,
                status=PlanningStatus.REJECTED,
                planned_qty=0,
                exec_params=None,
                rejection_reason_code=spread.reason_code,
                trace=trace,
                reasons=reasons,
                now=now,
            )
            raise SignalPlanRejectedError(
                plan.id, spread.reason_code, spread.reason_detail or ""
            )

        # ─── Step 6: Volatility / ATR 調整 ───────────────────────────────
        volatility = self._volatility.adjust(current_qty, ctx)
        trace.append(volatility.as_trace_entry())
        current_qty, reasons = self._apply_adjustment(volatility, current_qty, reasons)
        if volatility.rejected:
            plan = await self._save_plan(
                signal=signal, ctx=ctx,
                status=PlanningStatus.REJECTED,
                planned_qty=0,
                exec_params=None,
                rejection_reason_code=volatility.reason_code,
                trace=trace,
                reasons=reasons,
                now=now,
            )
            raise SignalPlanRejectedError(
                plan.id, volatility.reason_code, volatility.reason_detail or ""
            )

        # ─── Step 7: Lot 丸め ─────────────────────────────────────────────
        before_round = current_qty
        current_qty = self._sizer.round_to_lot(current_qty, ctx.symbol_lot_size)
        if current_qty != before_round:
            detail = f"lot rounding: {before_round}→{current_qty} (lot_size={ctx.symbol_lot_size})"
            trace.append({
                "stage": "lot_rounding",
                "before": before_round,
                "after": current_qty,
                "lot_size": ctx.symbol_lot_size,
            })
            reasons.append((
                PlanningReasonCode.LOT_SIZE_BELOW_MIN,
                detail,
                {"lot_size": ctx.symbol_lot_size},
                float(before_round),
                float(current_qty),
            ))

        # ─── Step 8: ゼロサイズチェック ──────────────────────────────────
        if current_qty <= 0:
            trace.append({"stage": "zero_check", "rejected": True, "qty": current_qty})
            plan = await self._save_plan(
                signal=signal, ctx=ctx,
                status=PlanningStatus.REJECTED,
                planned_qty=0,
                exec_params=None,
                rejection_reason_code=PlanningReasonCode.PLANNED_SIZE_ZERO,
                trace=trace,
                reasons=reasons + [(
                    PlanningReasonCode.PLANNED_SIZE_ZERO,
                    f"lot 丸め後に数量がゼロになりました (lot_size={ctx.symbol_lot_size})",
                    {"lot_size": ctx.symbol_lot_size},
                    float(before_round),
                    0.0,
                )],
                now=now,
            )
            raise SignalPlanRejectedError(
                plan.id,
                PlanningReasonCode.PLANNED_SIZE_ZERO,
                f"planned_qty=0 after lot rounding (lot_size={ctx.symbol_lot_size})",
            )

        # ─── Step 9: 執行パラメータ生成 ──────────────────────────────────
        exec_params = self._params_builder.build(ctx)
        trace.append({
            "stage": "execution_params",
            "params": exec_params.as_dict(),
        })

        # ─── Step 10: ステータス決定 ──────────────────────────────────────
        status = (
            PlanningStatus.REDUCED
            if current_qty < ctx.base_quantity
            else PlanningStatus.ACCEPTED
        )

        plan = await self._save_plan(
            signal=signal,
            ctx=ctx,
            status=status,
            planned_qty=current_qty,
            exec_params=exec_params,
            rejection_reason_code=None,
            trace=trace,
            reasons=reasons,
            now=now,
        )

        logger.info(
            "SignalPlanningService: %s signal_id=%s ticker=%s qty=%d→%d size_ratio=%.2f",
            status.value, signal.id, signal.ticker,
            ctx.base_quantity, current_qty, ctx.size_ratio,
        )
        return plan

    # ─── 内部ヘルパー ──────────────────────────────────────────────────────

    def _validate_decision(
        self,
        ctx: PlannerContext,
        now: datetime,
    ) -> tuple[PlanningReasonCode, str] | None:
        """
        Strategy decision の検証（defense in depth）。

        Gate が通過済みでも planning 層で独立して検証する。

        Returns:
            (reason_code, detail) tuple if rejected, else None
        """
        if ctx.signal_strategy_decision_id is None:
            return (
                PlanningReasonCode.DECISION_MISSING,
                "strategy gate の判定結果が見つかりません",
            )

        if ctx.decision_evaluation_time is not None:
            eval_time = ctx.decision_evaluation_time
            if eval_time.tzinfo is None:
                eval_time = eval_time.replace(tzinfo=timezone.utc)
            age_sec = (now - eval_time).total_seconds()
            max_age = _get_max_decision_age_sec()
            if age_sec > max_age:
                return (
                    PlanningReasonCode.DECISION_STALE,
                    f"strategy decision が古すぎます: {age_sec:.0f}秒前 (上限: {max_age}秒)",
                )

        return None

    def _apply_adjustment(
        self,
        result: AdjustmentResult,
        current_qty: int,
        reasons: list,
    ) -> tuple[int, list]:
        """
        AdjustmentResult を適用して current_qty と reasons を更新する。
        """
        if result.was_reduced and result.reason_code:
            reasons = reasons + [(
                result.reason_code,
                result.reason_detail or "",
                {},
                float(result.input_qty),
                float(result.output_qty),
            )]
        return result.output_qty, reasons

    async def _bypass_plan(
        self,
        signal: TradeSignal,
        ctx: PlannerContext,
        now: datetime,
    ) -> SignalPlan:
        """exit signal のバイパス: サイズ変更なしで accepted を保存して返す"""
        exec_params = self._params_builder.build(ctx)
        plan = await self._save_plan(
            signal=signal,
            ctx=ctx,
            status=PlanningStatus.ACCEPTED,
            planned_qty=signal.quantity,
            exec_params=exec_params,
            rejection_reason_code=None,
            trace=[{"stage": "bypass", "signal_type": signal.signal_type, "qty": signal.quantity}],
            reasons=[],
            now=now,
        )
        logger.debug(
            "SignalPlanningService: bypass (signal_type=%s) signal_id=%s",
            signal.signal_type, signal.id,
        )
        return plan

    async def _save_plan(
        self,
        signal: TradeSignal,
        ctx: PlannerContext,
        status: PlanningStatus,
        planned_qty: int,
        exec_params,
        rejection_reason_code: PlanningReasonCode | None,
        trace: list[dict],
        reasons: list[tuple],
        now: datetime,
    ) -> SignalPlan:
        """signal_plans + signal_plan_reasons を INSERT する"""
        # 想定金額計算
        price = exec_params.limit_price if exec_params else signal.limit_price
        planned_notional = (price * planned_qty) if (price and planned_qty > 0) else None

        # Phase T: execution_guard_hints を trace に追記して execution 側に運ぶ
        full_trace: list[dict[str, Any]] = list(trace) + [{
            "stage": "execution_guard_hints",
            "hints": ctx.execution_guard_hints,
        }]

        plan = SignalPlan(
            id=str(uuid.uuid4()),
            signal_id=signal.id,
            signal_strategy_decision_id=ctx.signal_strategy_decision_id,
            planning_status=status.value,
            planned_order_qty=planned_qty,
            planned_notional=planned_notional,
            order_type_candidate=exec_params.order_type_candidate if exec_params else None,
            limit_price=exec_params.limit_price if exec_params else None,
            stop_price=exec_params.stop_price if exec_params else None,
            max_slippage_bps=exec_params.max_slippage_bps if exec_params else None,
            participation_rate_cap=exec_params.participation_rate_cap if exec_params else None,
            entry_timeout_seconds=exec_params.entry_timeout_seconds if exec_params else None,
            applied_size_ratio=ctx.size_ratio,
            rejection_reason_code=rejection_reason_code.value if rejection_reason_code else None,
            planning_trace_json=full_trace,
            created_at=now,
        )
        self._db.add(plan)
        await self._db.flush()

        # reason レコードを保存
        for reason_code, reason_detail, snapshot, adj_before, adj_after in reasons:
            reason_row = SignalPlanReason(
                id=str(uuid.uuid4()),
                signal_plan_id=plan.id,
                reason_code=reason_code.value,
                reason_detail=reason_detail or None,
                input_snapshot_json=snapshot or None,
                adjustment_before=adj_before,
                adjustment_after=adj_after,
                created_at=now,
            )
            self._db.add(reason_row)

        await self._db.flush()
        return plan
