"""
BrokerAdapter 抽象基底クラス・共通データクラス定義

将来の証券会社差し替えに対応するため、全ブローカー実装はこのインターフェースに準拠する。
Phase 4 以降で TachibanaBrokerAdapter を実装する際もこの契約に従うこと。
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from trade_app.models.enums import OrderStatus, OrderType, Side

logger = logging.getLogger(__name__)


# ─── ブローカーとやり取りするデータクラス ─────────────────────────────────────

@dataclass
class OrderRequest:
    """
    ブローカーへの発注リクエスト。
    内部の Order モデルからこの形式に変換してブローカーへ送信する。
    """
    client_order_id: str       # 内部 Order.id（照合キー）
    ticker: str                # 銘柄コード（ブローカー形式: "7203" など）
    order_type: OrderType      # 成行 / 指値
    side: Side                 # 買い / 売り
    quantity: int              # 株数
    limit_price: Optional[float] = None      # 指値価格（order_type=LIMIT の場合必須）
    stop_price: Optional[float] = None       # 逆指値価格（stop 注文の場合に使用）
    time_in_force: str = "day"               # 有効期間: "day" / "gtc" / "ioc" / "fok"
    account_type: str = "cash"               # 口座区分: "cash" / "margin"


@dataclass
class OrderResponse:
    """ブローカーからの発注レスポンス"""
    broker_order_id: str      # ブローカーが採番した注文ID
    status: OrderStatus       # 発注直後の状態（通常 SUBMITTED）
    message: str = ""         # 任意メッセージ（エラー詳細等）


@dataclass
class OrderStatusResponse:
    """注文状態照会レスポンス"""
    broker_order_id: str
    status: OrderStatus
    filled_quantity: int = 0
    filled_price: Optional[float] = None
    message: str = ""
    remaining_qty: int = 0                   # 未約定残数量
    broker_execution_id: Optional[str] = None  # ブローカー約定ID（重複防止用）
    cancel_qty: int = 0                      # キャンセル済み数量


@dataclass
class CancelResult:
    """
    注文キャンセルの結果。

    success=True でもブローカーが即時確認しない場合がある（is_already_terminal を参照）。
    is_pending=True は取消受付済みだが取消完了が未確認の状態を示す（立花証券 e_api の
    取消非同期モデルに対応）。OrderPoller が次回照会時に完了を確認すること。
    """
    success: bool                    # キャンセル成功（またはキャンセル不要）
    reason: str = ""                 # 失敗理由・補足メッセージ
    is_already_terminal: bool = False  # 既に約定済み・キャンセル済み等の終端状態だった
    is_pending: bool = False           # 取消受付済みだが取消完了未確認（非同期取消）


@dataclass
class BalanceInfo:
    """口座残高情報"""
    cash_balance: float        # 現金残高（円）
    margin_available: float    # 信用取引可能額（円）
    total_equity: float        # 総資産（円）


@dataclass
class BrokerPosition:
    """ブローカー側のポジション情報（照合・同期用）"""
    broker_order_id: str
    ticker: str
    side: Side
    quantity: int
    average_price: float


# ─── 抽象基底クラス ────────────────────────────────────────────────────────────

class BrokerAdapter(ABC):
    """
    証券会社 API アダプターの抽象基底クラス。
    新しい証券会社に対応する場合は本クラスを継承して全抽象メソッドを実装すること。
    """

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResponse:
        """
        発注を送信する。

        Args:
            request: 発注リクエスト

        Returns:
            OrderResponse: ブローカーからの発注レスポンス

        Raises:
            BrokerAPIError: API通信エラー
            BrokerOrderError: 発注拒否（残高不足・制度信用不可等）
        """
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> CancelResult:
        """
        注文をキャンセルする。

        Args:
            broker_order_id: ブローカーが採番した注文ID

        Returns:
            CancelResult: キャンセル結果（success / reason / is_already_terminal）
        """
        ...

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> OrderStatusResponse:
        """
        注文状態を照会する。

        Args:
            broker_order_id: ブローカーが採番した注文ID

        Returns:
            OrderStatusResponse: 現在の注文状態
        """
        ...

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]:
        """
        ブローカー側の現在ポジション一覧を取得する（照合・同期用）。

        Returns:
            List[BrokerPosition]: 現在保有ポジション一覧
        """
        ...

    @abstractmethod
    async def get_balance(self) -> BalanceInfo:
        """
        口座残高を取得する。

        Returns:
            BalanceInfo: 現金残高・信用余力・総資産
        """
        ...

    @abstractmethod
    async def get_market_price(self, ticker: str) -> Optional[float]:
        """
        銘柄の現在市場価格を取得する。

        ExitWatcher が TP/SL 判定に使用する。
        取得できない場合（市場閉場中・データなし等）は None を返す。

        Args:
            ticker: 銘柄コード

        Returns:
            現在価格（円）、取得不可の場合は None
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """アダプター識別名（ログ・監査ログで使用）"""
        ...


# ─── カスタム例外 ─────────────────────────────────────────────────────────────

class BrokerAPIError(Exception):
    """
    ブローカー API との通信エラー（タイムアウト・HTTP 5xx 等）。

    発注が到達したかどうか不確定な場合がある。
    OrderRouter は BrokerAPIError を受け取った場合、Order を SUBMITTED のまま残し
    RecoveryManager による再照会に委ねること。
    """
    pass


class BrokerTemporaryError(BrokerAPIError):
    """
    一時的な通信エラー（タイムアウト・503 等）。

    リトライ可能。発注の到達可否が不確定。
    """
    pass


class BrokerRateLimitError(BrokerAPIError):
    """
    API レート制限エラー（HTTP 429 等）。

    待機後にリトライ可能。
    """
    pass


class BrokerMaintenanceError(BrokerAPIError):
    """
    ブローカーメンテナンス中エラー。

    メンテナンス終了後にリトライ可能。
    """
    pass


class BrokerOrderError(Exception):
    """ブローカーに発注を拒否された場合のエラー（残高不足・制度信用不可等）"""
    pass


class BrokerAuthError(Exception):
    """ブローカー API の認証エラー（セッション切れ等）"""
    pass
