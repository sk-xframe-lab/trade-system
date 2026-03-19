"""
MockBrokerAdapter
テスト・開発環境用のブローカーアダプター。
実際の発注は行わず、FillBehavior に応じた様々なシナリオをシミュレートする。
BROKER_TYPE=mock の場合にこのアダプターが使用される。
"""
import asyncio
import enum
import logging
import uuid
from datetime import datetime, timezone

from trade_app.brokers.base import (
    BalanceInfo,
    BrokerAdapter,
    BrokerPosition,
    CancelResult,
    OrderRequest,
    OrderResponse,
    OrderStatusResponse,
)
from trade_app.models.enums import OrderStatus, Side

logger = logging.getLogger(__name__)

# Mock口座の初期設定
_MOCK_CASH_BALANCE = 3_000_000.0      # 300万円
_MOCK_MARGIN_AVAILABLE = 9_000_000.0  # 信用取引余力（レバレッジ3倍）
_MOCK_FILL_DELAY_SEC = 0.1            # 約定シミュレーションの遅延（秒）


class FillBehavior(str, enum.Enum):
    """
    MockBroker の約定シナリオ。
    テスト・開発時に任意のシナリオを再現するために使用する。
    """
    IMMEDIATE = "immediate"                 # 即時全量約定（デフォルト）
    PARTIAL_THEN_FULL = "partial_then_full" # 50% 部分約定後に全量約定（部分約定テスト用）
    REJECT_IMMEDIATELY = "reject_immediately" # 発注を即時拒否（リジェクトテスト用）
    CANCEL_AFTER_SUBMIT = "cancel_after_submit" # 送信後にキャンセル（取消テスト用）
    UNKNOWN = "unknown"                     # 状態不明を返す（UNKNOWN ステータステスト用）
    NEVER_FILL = "never_fill"               # 永久に約定しない（タイムアウト・リカバリテスト用）


class MockBrokerAdapter(BrokerAdapter):
    """
    Mock ブローカーアダプター。
    FillBehavior で任意の約定シナリオをシミュレートする。
    テスト・開発時に BROKER_TYPE=mock で自動的に選択される。
    """

    def __init__(
        self,
        cash_balance: float = _MOCK_CASH_BALANCE,
        fill_delay_sec: float = _MOCK_FILL_DELAY_SEC,
        always_reject: bool = False,  # 後方互換性のため維持
        default_behavior: FillBehavior = FillBehavior.IMMEDIATE,
    ) -> None:
        self._cash_balance = cash_balance
        self._fill_delay_sec = fill_delay_sec
        # always_reject=True は REJECT_IMMEDIATELY に変換して統一
        self._default_behavior = (
            FillBehavior.REJECT_IMMEDIATELY if always_reject else default_behavior
        )

        # 発注ログ（broker_order_id → OrderStatusResponse）
        self._orders: dict[str, OrderStatusResponse] = {}
        # 保有ポジション（broker_order_id → BrokerPosition）
        self._positions: dict[str, BrokerPosition] = {}
        # 注文ごとの挙動オーバーライド（broker_order_id → FillBehavior）
        self._order_behaviors: dict[str, FillBehavior] = {}
        # 銘柄ごとのモック価格（ticker → price）
        # set_price() で上書き可能。未設定の場合は None を返す
        self._price_overrides: dict[str, float] = {}

    def queue_behavior(self, behavior: FillBehavior) -> None:
        """
        次の発注に適用する FillBehavior を1件予約する。
        予約した挙動は次の place_order 呼び出し時に消費される。

        使用例（テスト）:
            adapter.queue_behavior(FillBehavior.PARTIAL_THEN_FULL)
            await adapter.place_order(...)  # この発注だけ部分約定シナリオ
        """
        self._queued_behavior: FillBehavior | None = behavior

    @property
    def _next_behavior(self) -> FillBehavior:
        """次の発注挙動を返す（予約があれば消費、なければデフォルト）"""
        if hasattr(self, "_queued_behavior") and self._queued_behavior is not None:
            b = self._queued_behavior
            self._queued_behavior = None
            return b
        return self._default_behavior

    @property
    def name(self) -> str:
        return "MockBroker"

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        """
        発注をシミュレートする。
        FillBehavior に応じた挙動を再現する。
        """
        behavior = self._next_behavior
        logger.info(
            "[MockBroker] place_order: %s %s %d株 @ %s behavior=%s",
            request.side,
            request.ticker,
            request.quantity,
            f"{request.limit_price}円" if request.limit_price else "成行",
            behavior.value,
        )

        # ─── 即時拒否 ────────────────────────────────────────────────────
        if behavior == FillBehavior.REJECT_IMMEDIATELY:
            logger.warning("[MockBroker] 発注を拒否（REJECT_IMMEDIATELY）")
            return OrderResponse(
                broker_order_id="",
                status=OrderStatus.REJECTED,
                message="MockBroker: REJECT_IMMEDIATELY シナリオ",
            )

        broker_order_id = f"MOCK-{uuid.uuid4().hex[:12].upper()}"

        # 発注直後は SUBMITTED 状態
        self._orders[broker_order_id] = OrderStatusResponse(
            broker_order_id=broker_order_id,
            status=OrderStatus.SUBMITTED,
            filled_quantity=0,
        )
        # 注文ごとの挙動を記録（status_query 時に参照）
        self._order_behaviors[broker_order_id] = behavior

        # ─── 非同期で約定処理を実行（遅延シミュレーション）──────────────
        asyncio.create_task(
            self._simulate_fill(broker_order_id, request, behavior)
        )

        logger.info("[MockBroker] 発注受付: broker_order_id=%s", broker_order_id)
        return OrderResponse(
            broker_order_id=broker_order_id,
            status=OrderStatus.SUBMITTED,
            message=f"Mock order submitted (behavior={behavior.value})",
        )

    async def cancel_order(self, broker_order_id: str) -> CancelResult:
        """
        注文をキャンセルする。
        既に約定済み・キャンセル済みの場合は is_already_terminal=True で返す。
        """
        order = self._orders.get(broker_order_id)
        if order is None:
            logger.warning("[MockBroker] cancel: 注文が見つかりません: %s", broker_order_id)
            return CancelResult(
                success=False,
                reason=f"注文が見つかりません: {broker_order_id}",
            )

        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            logger.warning(
                "[MockBroker] cancel: 既に終端状態のためキャンセル不要: %s status=%s",
                broker_order_id, order.status.value,
            )
            return CancelResult(
                success=True,
                reason=f"既に {order.status.value} 状態",
                is_already_terminal=True,
            )

        order.status = OrderStatus.CANCELLED
        logger.info("[MockBroker] キャンセル成功: %s", broker_order_id)
        return CancelResult(success=True)

    async def get_order_status(self, broker_order_id: str) -> OrderStatusResponse:
        """注文状態を返す。UNKNOWN シナリオでは OrderStatus.UNKNOWN を返す。"""
        order = self._orders.get(broker_order_id)
        if order is None:
            return OrderStatusResponse(
                broker_order_id=broker_order_id,
                status=OrderStatus.FAILED,
                message=f"注文が見つかりません: {broker_order_id}",
            )
        # UNKNOWN シナリオでは状態不明を返す（まだ約定していない間のみ）
        behavior = self._order_behaviors.get(broker_order_id, FillBehavior.IMMEDIATE)
        if (
            behavior == FillBehavior.UNKNOWN
            and order.status == OrderStatus.SUBMITTED
        ):
            return OrderStatusResponse(
                broker_order_id=broker_order_id,
                status=OrderStatus.UNKNOWN,
                message="MockBroker: UNKNOWN シナリオ",
            )
        return order

    async def get_positions(self) -> list[BrokerPosition]:
        """現在の保有ポジション一覧を返す"""
        return list(self._positions.values())

    async def get_market_price(self, ticker: str) -> float | None:
        """
        銘柄のモック現在価格を返す。
        set_price() で設定した価格を返す。未設定の場合は None。

        テスト・開発時に ExitWatcher の TP/SL 判定を制御するために使用する。
        例:
            broker.set_price("7203", 1100.0)  # TP トリガーをシミュレート
        """
        return self._price_overrides.get(ticker)

    def set_price(self, ticker: str, price: float) -> None:
        """銘柄のモック価格を設定する（ExitWatcher テスト用）"""
        self._price_overrides[ticker] = price
        logger.debug("[MockBroker] 価格設定: %s = %.0f円", ticker, price)

    def clear_price(self, ticker: str) -> None:
        """銘柄のモック価格設定をクリアする"""
        self._price_overrides.pop(ticker, None)

    async def get_balance(self) -> BalanceInfo:
        """口座残高を返す（Mock固定値）"""
        return BalanceInfo(
            cash_balance=self._cash_balance,
            margin_available=self._cash_balance * 3.0,
            total_equity=self._cash_balance,
        )

    # ─── 内部処理 ──────────────────────────────────────────────────────────

    async def _simulate_fill(
        self,
        broker_order_id: str,
        request: OrderRequest,
        behavior: FillBehavior,
    ) -> None:
        """
        FillBehavior に応じた約定シミュレーションを実行する内部処理。

        シナリオ別の動作:
          IMMEDIATE           : fill_delay_sec 後に全量約定
          PARTIAL_THEN_FULL   : delay 後に 50% 部分約定 → delay 後に全量約定
          CANCEL_AFTER_SUBMIT : delay 後にキャンセル
          UNKNOWN             : 状態を変更しない（get_order_status が UNKNOWN を返す）
          NEVER_FILL          : 何もしない（SUBMITTED のまま残る）
        """
        await asyncio.sleep(self._fill_delay_sec)

        order = self._orders.get(broker_order_id)
        if order is None or order.status == OrderStatus.CANCELLED:
            return

        # 約定価格を決定（指値=指定価格、成行=1000円固定）
        fill_price = request.limit_price if request.limit_price else 1000.0

        if behavior == FillBehavior.CANCEL_AFTER_SUBMIT:
            order.status = OrderStatus.CANCELLED
            logger.info("[MockBroker] キャンセルシミュレーション: %s", broker_order_id)
            return

        if behavior in (FillBehavior.UNKNOWN, FillBehavior.NEVER_FILL):
            # 状態を変更しない
            logger.info("[MockBroker] %s: 状態変更なし: %s", behavior.value, broker_order_id)
            return

        if behavior == FillBehavior.PARTIAL_THEN_FULL:
            # まず 50% 部分約定
            partial_qty = max(1, request.quantity // 2)
            order.status = OrderStatus.PARTIAL
            order.filled_quantity = partial_qty
            order.filled_price = fill_price
            logger.info(
                "[MockBroker] 部分約定シミュレーション: %s %d/%d株",
                broker_order_id, partial_qty, request.quantity,
            )
            # 追加の遅延後に全量約定
            await asyncio.sleep(self._fill_delay_sec)
            order = self._orders.get(broker_order_id)
            if order is None or order.status == OrderStatus.CANCELLED:
                return

        # 全量約定（IMMEDIATE も PARTIAL_THEN_FULL の最終フェーズもここへ）
        order.status = OrderStatus.FILLED
        order.filled_quantity = request.quantity
        order.filled_price = fill_price

        self._positions[broker_order_id] = BrokerPosition(
            broker_order_id=broker_order_id,
            ticker=request.ticker,
            side=request.side,
            quantity=request.quantity,
            average_price=fill_price,
        )

        logger.info(
            "[MockBroker] 約定シミュレーション完了: %s %s %d株 @ %.0f円",
            broker_order_id,
            request.ticker,
            request.quantity,
            fill_price,
        )
