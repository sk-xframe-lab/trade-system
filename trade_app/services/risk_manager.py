"""
RiskManager サービス
発注前のリスクチェックを一元管理する。

チェック項目（全て AND 条件）:
  1. 市場時間チェック（JST 08:00〜15:35 以外は拒否）
  2. 残高チェック（1ポジションが残高の MAX_POSITION_SIZE_PCT% 以内）
  3. 同時保有ポジション上限チェック（MAX_CONCURRENT_POSITIONS 以内）
  4. 日次損失上限チェック（本日の確定損失が DAILY_LOSS_LIMIT_JPY 未満）
  5. 銘柄集中チェック（同一銘柄のオープンポジションが 1 件以内）
"""
import logging
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.brokers.base import BalanceInfo, BrokerAdapter
from trade_app.config import Settings, get_settings
from trade_app.models.enums import AuditEventType, OrderStatus, PositionStatus, SignalStatus
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.signal import TradeSignal
from trade_app.models.trade_result import TradeResult
from trade_app.services.audit_logger import AuditLogger

logger = logging.getLogger(__name__)

_JST = ZoneInfo("Asia/Tokyo")
# 発注受付時間（JST）: 08:00〜15:35
_MARKET_OPEN_JST = time(8, 0, 0)
_MARKET_CLOSE_JST = time(15, 35, 0)


class RiskRejectedError(Exception):
    """リスクチェックで拒否された場合に送出"""
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RiskManager:
    """
    発注前リスクチェックサービス。
    全チェックを通過した場合のみ発注を許可する。
    """

    def __init__(
        self,
        db: AsyncSession,
        broker: BrokerAdapter,
        audit: AuditLogger,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._broker = broker
        self._audit = audit
        self._settings = settings or get_settings()

    async def check(self, signal: TradeSignal, planned_qty: int | None = None) -> BalanceInfo:
        """
        シグナルに対してすべてのリスクチェックを実行する。

        Args:
            signal: チェック対象のシグナル
            planned_qty: Planning Layer が算出した発注数量（None の場合は signal.quantity を使用）

        Returns:
            BalanceInfo: 残高情報（発注計算に使用）

        Raises:
            RiskRejectedError: いずれかのチェックで拒否された場合
        """
        # ─── 0. 取引停止チェック（halt が最優先）────────────────────────
        await self._check_trading_halt()

        # ─── 1. 市場時間チェック ─────────────────────────────────────────
        self._check_market_hours()

        # ─── 2. 残高取得（以降のチェックで使用）──────────────────────────
        balance = await self._broker.get_balance()

        # ─── 3. ポジションサイズチェック ─────────────────────────────────
        await self._check_position_size(signal, balance, planned_qty=planned_qty)

        # ─── 4. 同時保有ポジション上限チェック ────────────────────────────
        await self._check_max_positions()

        # ─── 5. 日次損失上限チェック ──────────────────────────────────────
        await self._check_daily_loss()

        # ─── 6. 銘柄集中チェック ──────────────────────────────────────────
        await self._check_ticker_concentration(signal.ticker)

        # ─── 7. 未解決注文チェック ─────────────────────────────────────────
        await self._check_unresolved_orders(signal.ticker)

        logger.info(
            "リスクチェック通過: signal_id=%s ticker=%s side=%s qty=%d",
            signal.id, signal.ticker, signal.side, signal.quantity,
        )
        return balance

    # ─── 個別チェック ─────────────────────────────────────────────────────

    async def _check_trading_halt(self) -> None:
        """
        取引停止（halt）状態のチェック。
        DB の trading_halts テーブルに is_active=True のレコードがあれば拒否する。
        halt 状態はDB正本のため、起動直後でも正しい状態が反映される。
        """
        from trade_app.services.halt_manager import HaltManager
        halt_mgr = HaltManager()
        is_halted, reason = await halt_mgr.is_halted(self._db)
        if is_halted:
            raise RiskRejectedError(
                f"取引停止中: {reason} — 管理者による手動解除が必要です"
            )

    def _check_market_hours(self) -> None:
        """
        現在時刻が市場発注受付時間内かチェックする。
        JST 08:00〜15:35 の範囲外は拒否。
        """
        now_jst = datetime.now(_JST).time()
        if not (_MARKET_OPEN_JST <= now_jst <= _MARKET_CLOSE_JST):
            raise RiskRejectedError(
                f"市場時間外 (現在: JST {now_jst.strftime('%H:%M:%S')}、"
                f"受付: {_MARKET_OPEN_JST}〜{_MARKET_CLOSE_JST})"
            )

    async def _check_position_size(
        self, signal: TradeSignal, balance: BalanceInfo, planned_qty: int | None = None
    ) -> None:
        """
        1ポジションの発注金額が残高の MAX_POSITION_SIZE_PCT% 以内かチェックする。
        指値の場合は limit_price × quantity で計算。
        成行の場合は残高チェックをスキップ（価格不明のため）。
        planned_qty が指定された場合は signal.quantity の代わりに使用する。
        """
        if signal.order_type != "limit" or signal.limit_price is None:
            return  # 成行注文は価格不明のためスキップ

        qty = planned_qty if planned_qty is not None else signal.quantity
        order_amount = signal.limit_price * qty
        max_amount = balance.cash_balance * (self._settings.MAX_POSITION_SIZE_PCT / 100.0)

        if order_amount > max_amount:
            raise RiskRejectedError(
                f"発注金額超過: {order_amount:,.0f}円 > 上限 {max_amount:,.0f}円 "
                f"(残高 {balance.cash_balance:,.0f}円 の "
                f"{self._settings.MAX_POSITION_SIZE_PCT}%)"
            )

    async def _check_max_positions(self) -> None:
        """
        現在のオープンポジション数が上限以内かチェックする。
        """
        result = await self._db.execute(
            select(func.count(Position.id)).where(
                Position.status == PositionStatus.OPEN.value
            )
        )
        open_count = result.scalar() or 0

        if open_count >= self._settings.MAX_CONCURRENT_POSITIONS:
            raise RiskRejectedError(
                f"同時保有ポジション上限超過: 現在 {open_count} 件 "
                f"(上限: {self._settings.MAX_CONCURRENT_POSITIONS} 件)"
            )

    async def _check_daily_loss(self) -> None:
        """
        本日の確定損失合計が DAILY_LOSS_LIMIT_JPY を超えていないかチェックする。
        損益がマイナスの合計が上限を超えた場合は新規発注を停止する。
        """
        today_jst = datetime.now(_JST).date()
        # JST の 00:00 を UTC に変換
        today_jst_start = datetime(today_jst.year, today_jst.month, today_jst.day,
                                    tzinfo=_JST)

        result = await self._db.execute(
            select(func.coalesce(func.sum(TradeResult.pnl), 0.0)).where(
                TradeResult.created_at >= today_jst_start,
                TradeResult.pnl < 0,
            )
        )
        daily_loss = abs(result.scalar() or 0.0)

        if daily_loss >= self._settings.DAILY_LOSS_LIMIT_JPY:
            raise RiskRejectedError(
                f"日次損失上限到達: 本日損失 {daily_loss:,.0f}円 "
                f"(上限: {self._settings.DAILY_LOSS_LIMIT_JPY:,.0f}円) "
                f"→ 本日の新規発注を停止"
            )

    async def _check_ticker_concentration(self, ticker: str) -> None:
        """
        同一銘柄のオープンポジションが既に存在する場合は重複発注として拒否する。
        """
        result = await self._db.execute(
            select(func.count(Position.id)).where(
                Position.ticker == ticker,
                Position.status.in_([PositionStatus.OPEN.value, PositionStatus.CLOSING.value]),
            )
        )
        count = result.scalar() or 0

        if count > 0:
            raise RiskRejectedError(
                f"銘柄集中: {ticker} のオープンポジションが既に {count} 件存在します"
            )

    async def _check_unresolved_orders(self, ticker: str) -> None:
        """
        同一銘柄の未解決注文（SUBMITTED / PARTIAL / UNKNOWN）が存在する場合は拒否する。

        これらの状態は OrderPoller が解決するまで新規発注をブロックする。
        UNKNOWN（ブローカーへの照会が失敗した状態）は特に危険なため必ずブロックする。
        """
        # 未解決とみなすステータス
        _UNRESOLVED_STATUSES = [
            OrderStatus.SUBMITTED.value,
            OrderStatus.PARTIAL.value,
            OrderStatus.UNKNOWN.value,
        ]
        result = await self._db.execute(
            select(func.count(Order.id)).where(
                Order.ticker == ticker,
                Order.status.in_(_UNRESOLVED_STATUSES),
            )
        )
        count = result.scalar() or 0

        if count > 0:
            raise RiskRejectedError(
                f"未解決注文あり: {ticker} の注文が未約定・状態不明のまま {count} 件存在します "
                f"(SUBMITTED/PARTIAL/UNKNOWN) — OrderPoller による解決を待ってください"
            )
