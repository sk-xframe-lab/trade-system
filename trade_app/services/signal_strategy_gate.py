"""
SignalStrategyGate — Signal Router Integration Gate

責務:
  - Signal を受け取り latest strategy decision を参照する
  - global decision (ticker=None) と symbol decision (ticker=signal.ticker) を両方評価
  - stale / missing / blocked を安全側で reject
  - direction 不一致の strategy を除外
  - size_ratio を min(global, symbol) で算出
  - 判定結果を signal_strategy_decisions に保存（APPEND ONLY）
  - SignalStrategyGateResult を返す

設計制約（必須）:
  - BrokerAdapter を直接呼ばない
  - OrderRouter を直接呼ばない
  - PositionManager を直接呼ばない
  - RiskManager を置き換えない（前段ゲートとして動く）
  - Strategy decision だけで発注可否を最終決定しない

適用範囲:
  - signal_type = "entry" の Signal のみ適用
  - signal_type = "exit" は通過（strategy gate を適用しない）

global / symbol decision 統合ルール:
  1. signal.side から direction を導出: buy → long, sell → short
  2. direction が一致する strategy の decision のみを評価対象とする
     （strategy.direction = "long" or "both" → long signal に適合）
  3. 両方（global + symbol）に方向適合 decision が存在することが必要
  4. どちらか一方でも missing → reject
  5. どちらか一方でも stale → reject
  6. どちらか一方でも entry_allowed=False → reject
  7. size_ratio = min(全 relevant decisions の size_ratio)
  8. size_ratio <= 0 → reject

stale 判定:
  decision.evaluation_time が now - SIGNAL_MAX_DECISION_AGE_SEC より古ければ stale
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.config import get_settings
from trade_app.models.current_strategy_decision import CurrentStrategyDecision
from trade_app.models.signal import TradeSignal
from trade_app.models.signal_strategy_decision import SignalStrategyDecision
from trade_app.services.strategy.decision_repository import DecisionRepository

logger = logging.getLogger(__name__)


class StrategyGateRejectedError(Exception):
    """Strategy Gate がシグナルを拒否した場合に送出"""

    def __init__(self, reason: str, blocking_reasons: list[str]) -> None:
        self.reason = reason
        self.blocking_reasons = blocking_reasons
        super().__init__(reason)


@dataclass
class SignalStrategyGateResult:
    """Strategy Gate の判定結果"""
    entry_allowed: bool
    size_ratio: float
    blocking_reasons: list[str]
    matched_strategy_codes: list[str]
    decision_ids: list[str]
    evaluation_time: datetime
    signal_direction: str
    bypassed: bool = False  # True = exit signal などで gate をスキップ


def _signal_direction(signal: TradeSignal) -> str:
    """signal.side から方向を導出する: buy → long, sell → short"""
    return "long" if signal.side == "buy" else "short"


def _is_direction_compatible(decision: CurrentStrategyDecision, signal_direction: str) -> bool:
    """
    strategy の direction と signal の direction が一致するか確認する。
    evidence_json に direction が含まれていない場合は "both" 扱い（安全側）。
    """
    strategy_direction = (decision.evidence_json or {}).get("direction", "both")
    if strategy_direction == "both":
        return True
    return strategy_direction == signal_direction


def _is_stale(decision: CurrentStrategyDecision, now: datetime, max_age_sec: int) -> bool:
    """decision.evaluation_time が stale かどうかを判定する"""
    eval_time = decision.evaluation_time
    if eval_time.tzinfo is None:
        eval_time = eval_time.replace(tzinfo=timezone.utc)
    return (now - eval_time).total_seconds() > max_age_sec


class SignalStrategyGate:
    """
    Signal に対して Strategy decision を評価する前段ゲート。

    RiskManager より前に実行することで strategy 観点の reject を早期に行う。
    発注・ポジション更新・ブローカー呼び出しは一切行わない。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._decision_repo = DecisionRepository(db)

    async def check(
        self,
        signal: TradeSignal,
        evaluation_time: datetime | None = None,
    ) -> SignalStrategyGateResult:
        """
        Signal に対して Strategy Gate を適用する。

        entry_allowed=False の場合は StrategyGateRejectedError を送出する。
        signal_strategy_decisions テーブルには常に記録する（pass / reject 両方）。

        Args:
            signal: チェック対象の TradeSignal
            evaluation_time: 評価基準時刻（None → 現在時刻）。
                             ⚠️ stale 判定はこの時刻を基準に行う。

        Returns:
            SignalStrategyGateResult

        Raises:
            StrategyGateRejectedError: gate が拒否した場合
        """
        now = evaluation_time or datetime.now(timezone.utc)

        # ─── exit signal は gate をバイパス ────────────────────────────────
        if signal.signal_type != "entry":
            result = SignalStrategyGateResult(
                entry_allowed=True,
                size_ratio=1.0,
                blocking_reasons=[],
                matched_strategy_codes=[],
                decision_ids=[],
                evaluation_time=now,
                signal_direction="",
                bypassed=True,
            )
            logger.debug(
                "SignalStrategyGate: bypass (signal_type=%s) signal_id=%s",
                signal.signal_type, signal.id,
            )
            return result

        settings = get_settings()
        max_age_sec = settings.SIGNAL_MAX_DECISION_AGE_SEC
        sig_direction = _signal_direction(signal)

        # ─── latest decisions 取得 ─────────────────────────────────────────
        global_decisions = await self._decision_repo.get_latest_decisions(ticker=None)
        ticker_decisions = await self._decision_repo.get_latest_decisions(ticker=signal.ticker)

        # ─── direction フィルタ ────────────────────────────────────────────
        relevant_global = [
            d for d in global_decisions if _is_direction_compatible(d, sig_direction)
        ]
        relevant_ticker = [
            d for d in ticker_decisions if _is_direction_compatible(d, sig_direction)
        ]

        blocking_reasons: list[str] = []

        # ─── missing チェック ──────────────────────────────────────────────
        if not relevant_global:
            blocking_reasons.append("decision_missing:global")
        if not relevant_ticker:
            blocking_reasons.append("decision_missing:symbol")

        if blocking_reasons:
            return await self._build_rejected_result(
                signal=signal,
                sig_direction=sig_direction,
                relevant_global=relevant_global,
                relevant_ticker=relevant_ticker,
                blocking_reasons=blocking_reasons,
                now=now,
            )

        # ─── stale チェック ────────────────────────────────────────────────
        for d in relevant_global:
            if _is_stale(d, now, max_age_sec):
                blocking_reasons.append(f"decision_stale:global:{d.strategy_code}")

        for d in relevant_ticker:
            if _is_stale(d, now, max_age_sec):
                blocking_reasons.append(f"decision_stale:symbol:{d.strategy_code}")

        if blocking_reasons:
            return await self._build_rejected_result(
                signal=signal,
                sig_direction=sig_direction,
                relevant_global=relevant_global,
                relevant_ticker=relevant_ticker,
                blocking_reasons=blocking_reasons,
                now=now,
            )

        # ─── entry_allowed チェック ────────────────────────────────────────
        for d in relevant_global:
            if not d.entry_allowed:
                blocking_reasons.append(f"decision_blocked:global:{d.strategy_code}")
                # blocking_reasons から最重要な1件を証跡として追加
                primary_reason = (d.blocking_reasons_json or [None])[0]
                if primary_reason:
                    blocking_reasons.append(f"strategy_reason:{primary_reason}")

        for d in relevant_ticker:
            if not d.entry_allowed:
                blocking_reasons.append(f"decision_blocked:symbol:{d.strategy_code}")
                primary_reason = (d.blocking_reasons_json or [None])[0]
                if primary_reason:
                    blocking_reasons.append(f"strategy_reason:{primary_reason}")

        if blocking_reasons:
            return await self._build_rejected_result(
                signal=signal,
                sig_direction=sig_direction,
                relevant_global=relevant_global,
                relevant_ticker=relevant_ticker,
                blocking_reasons=blocking_reasons,
                now=now,
            )

        # ─── size_ratio 算出 ───────────────────────────────────────────────
        all_ratios = [d.size_ratio for d in relevant_global + relevant_ticker]
        size_ratio = min(all_ratios) if all_ratios else 0.0

        if size_ratio <= 0.0:
            blocking_reasons.append("size_ratio_zero")
            return await self._build_rejected_result(
                signal=signal,
                sig_direction=sig_direction,
                relevant_global=relevant_global,
                relevant_ticker=relevant_ticker,
                blocking_reasons=blocking_reasons,
                now=now,
            )

        # ─── pass ─────────────────────────────────────────────────────────
        matched_codes = list({d.strategy_code for d in relevant_global + relevant_ticker})
        decision_ids = [d.id for d in relevant_global + relevant_ticker]

        await self._save_decision(
            signal=signal,
            sig_direction=sig_direction,
            relevant_global=relevant_global,
            relevant_ticker=relevant_ticker,
            entry_allowed=True,
            size_ratio=size_ratio,
            blocking_reasons=[],
            now=now,
        )

        logger.info(
            "SignalStrategyGate: PASS signal_id=%s ticker=%s direction=%s "
            "size_ratio=%.2f strategies=%s",
            signal.id, signal.ticker, sig_direction, size_ratio, matched_codes,
        )

        return SignalStrategyGateResult(
            entry_allowed=True,
            size_ratio=size_ratio,
            blocking_reasons=[],
            matched_strategy_codes=matched_codes,
            decision_ids=decision_ids,
            evaluation_time=now,
            signal_direction=sig_direction,
        )

    # ─── 内部ヘルパー ──────────────────────────────────────────────────────

    async def _build_rejected_result(
        self,
        signal: TradeSignal,
        sig_direction: str,
        relevant_global: list[CurrentStrategyDecision],
        relevant_ticker: list[CurrentStrategyDecision],
        blocking_reasons: list[str],
        now: datetime,
    ) -> SignalStrategyGateResult:
        """reject 時の共通処理: 保存 → StrategyGateRejectedError 送出"""
        await self._save_decision(
            signal=signal,
            sig_direction=sig_direction,
            relevant_global=relevant_global,
            relevant_ticker=relevant_ticker,
            entry_allowed=False,
            size_ratio=0.0,
            blocking_reasons=blocking_reasons,
            now=now,
        )

        reason_summary = "; ".join(blocking_reasons[:3])  # 最初の3件をサマリとして使用
        logger.info(
            "SignalStrategyGate: REJECT signal_id=%s ticker=%s direction=%s reasons=%s",
            signal.id, signal.ticker, sig_direction, blocking_reasons,
        )
        raise StrategyGateRejectedError(
            reason=f"strategy gate rejected: {reason_summary}",
            blocking_reasons=blocking_reasons,
        )

    async def _save_decision(
        self,
        signal: TradeSignal,
        sig_direction: str,
        relevant_global: list[CurrentStrategyDecision],
        relevant_ticker: list[CurrentStrategyDecision],
        entry_allowed: bool,
        size_ratio: float,
        blocking_reasons: list[str],
        now: datetime,
    ) -> SignalStrategyDecision:
        """signal_strategy_decisions に判定結果を INSERT する"""
        global_dec = relevant_global[0] if relevant_global else None
        symbol_dec = relevant_ticker[0] if relevant_ticker else None

        evidence = {
            "signal_direction": sig_direction,
            "global_strategy_codes": [d.strategy_code for d in relevant_global],
            "symbol_strategy_codes": [d.strategy_code for d in relevant_ticker],
            "global_entry_allowed": [d.entry_allowed for d in relevant_global],
            "symbol_entry_allowed": [d.entry_allowed for d in relevant_ticker],
            "global_size_ratios": [d.size_ratio for d in relevant_global],
            "symbol_size_ratios": [d.size_ratio for d in relevant_ticker],
        }

        row = SignalStrategyDecision(
            id=str(uuid.uuid4()),
            signal_id=signal.id,
            ticker=signal.ticker,
            signal_direction=sig_direction,
            global_decision_id=global_dec.id if global_dec else None,
            symbol_decision_id=symbol_dec.id if symbol_dec else None,
            decision_time=now,
            entry_allowed=entry_allowed,
            size_ratio=size_ratio,
            blocking_reasons_json=blocking_reasons,
            evidence_json=evidence,
            created_at=now,
        )
        self._db.add(row)
        await self._db.flush()
        return row
