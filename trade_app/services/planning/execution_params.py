"""
ExecutionParams — 執行パラメータ候補の生成

signal と PlannerContext から「発注実行時に使う可能性のあるパラメータ」を生成する。
これはあくまで「候補」であり、実際の発注はOrderRouterが行う。

Phase 9 設計:
  - order_type_candidate: signal.order_type をそのまま引き継ぐ（変更は Phase 10+）
  - limit_price: signal.limit_price をそのまま引き継ぐ
  - stop_price: signal.stop_price をそのまま引き継ぐ
  - max_slippage_bps: 成行発注時のみ推奨値を設定（デフォルト 30bps）
  - participation_rate_cap: 低流動性時は低めに設定
  - entry_timeout_seconds: デフォルト 300 秒（5分）
"""
from __future__ import annotations

from dataclasses import dataclass

from trade_app.services.planning.context import PlannerContext


@dataclass
class ExecutionParams:
    """
    planning 結果として提案する執行パラメータ候補。

    OrderRouter はこれを参照して発注する（Phase 9 では参考のみ）。
    """
    order_type_candidate: str
    limit_price: float | None
    stop_price: float | None
    max_slippage_bps: float | None
    participation_rate_cap: float | None
    entry_timeout_seconds: int | None

    def as_dict(self) -> dict:
        return {
            "order_type_candidate": self.order_type_candidate,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "max_slippage_bps": self.max_slippage_bps,
            "participation_rate_cap": self.participation_rate_cap,
            "entry_timeout_seconds": self.entry_timeout_seconds,
        }


class ExecutionParamsBuilder:
    """
    PlannerContext と signal から ExecutionParams を生成する。

    Phase 9:
      - order_type, limit_price, stop_price は signal から引き継ぐ
      - max_slippage_bps は成行発注時のみ設定（30bps = 0.3%）
      - participation_rate_cap は volume_ratio が低い場合に小さく設定
      - entry_timeout_seconds は 300 秒固定（5分、将来設定化可能）
    """

    DEFAULT_SLIPPAGE_BPS: float = 30.0      # 成行発注時のデフォルト許容スリッページ
    DEFAULT_TIMEOUT_SEC: int = 300          # エントリータイムアウト（秒）
    LOW_LIQUIDITY_PARTICIPATION_CAP: float = 0.05   # 低流動性時の参加率上限 5%
    NORMAL_PARTICIPATION_CAP: float = 0.10          # 平常時の参加率上限 10%

    def build(self, ctx: PlannerContext) -> ExecutionParams:
        """
        PlannerContext から ExecutionParams を生成する。

        Args:
            ctx: PlannerContext

        Returns:
            ExecutionParams
        """
        signal = ctx.signal

        # ─── max_slippage_bps ─────────────────────────────────────────
        # 成行 (market) 発注のみ設定。指値は price で制御済みなので不要。
        max_slippage_bps: float | None = None
        if signal.order_type == "market":
            max_slippage_bps = self.DEFAULT_SLIPPAGE_BPS

        # ─── participation_rate_cap ───────────────────────────────────
        # 低流動性時は小さい参加率で発注（市場インパクト制限）
        if ctx.volume_ratio < 0.3:
            participation_rate_cap = self.LOW_LIQUIDITY_PARTICIPATION_CAP
        else:
            participation_rate_cap = self.NORMAL_PARTICIPATION_CAP

        return ExecutionParams(
            order_type_candidate=signal.order_type,
            limit_price=signal.limit_price,
            stop_price=signal.stop_price,
            max_slippage_bps=max_slippage_bps,
            participation_rate_cap=participation_rate_cap,
            entry_timeout_seconds=self.DEFAULT_TIMEOUT_SEC,
        )
