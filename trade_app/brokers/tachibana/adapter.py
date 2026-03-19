"""
立花証券 e_api ブローカーアダプター

Phase 10-C 実装状況:
  [x] place_order      : 現物成行 / 指値の買い・売り
  [x] get_order_status : CLMOrderListDetail / sOrderStatusCode（仕様書確認済み）
  [x] cancel_order     : CLMKabuCancelOrder（sCLMID は推定）
  [x] get_balance      : CLMZanKaiKanougaku（現物）+ CLMZanShinkiKanoIjiritu（信用）2API方式
  [x] get_positions    : CLMGenbutuKabuList（現物）+ CLMShinyouTategyokuList（信用）
  [x] get_market_price : CLMMfdsGetMarketPrice / sUrlPrice

設計制約:
  - 各 API はタイムアウト時に自動再送しない（安全側優先）
  - BrokerAuthError 時はセッションを無効化して再スロー
  - Tachibana 固有ロジックは mapper / session / client に閉じ込める
  - pipeline / planning / risk に e_api 仕様を漏らさない
  - cancel_order の正常応答は「取消受付済み」(is_pending=True)。取消完了は get_order_status/poller が確認する
"""
import logging
from typing import Optional

from trade_app.brokers.base import (
    BalanceInfo,
    BrokerAPIError,
    BrokerAdapter,
    BrokerAuthError,
    BrokerPosition,
    CancelResult,
    OrderRequest,
    OrderResponse,
    OrderStatusResponse,
)
from trade_app.brokers.tachibana.client import TachibanaClient
from trade_app.brokers.tachibana.session import TachibanaSessionManager
from trade_app.brokers.tachibana import mapper

logger = logging.getLogger(__name__)


class TachibanaBrokerAdapter(BrokerAdapter):
    """
    立花証券 e_api ブローカーアダプター。

    依存コンポーネント:
      TachibanaClient:         低レベル HTTP 通信・Shift-JIS デコード
      TachibanaSessionManager: ログイン・仮想 URL 管理・セッション維持
      TachibanaMapper:         リクエスト/レスポンス変換（mapper モジュール）

    コンストラクタ引数:
      client / session を外部から注入できる（テスト・DI 用）。
      None の場合は settings から自動生成する。
    """

    def __init__(
        self,
        client: Optional[TachibanaClient] = None,
        session: Optional[TachibanaSessionManager] = None,
    ) -> None:
        from trade_app.config import get_settings
        settings = get_settings()

        if client is None:
            client = TachibanaClient(
                timeout_sec=settings.TACHIBANA_REQUEST_TIMEOUT_SEC,
            )
        if session is None:
            session = TachibanaSessionManager(
                client=client,
                login_url=settings.TACHIBANA_BASE_URL,
                user_id=settings.TACHIBANA_USER_ID,
                password=settings.TACHIBANA_PASSWORD,
                second_password=settings.TACHIBANA_SECOND_PASSWORD,
            )

        self._client = client
        self._session = session
        self._settings = settings

    @property
    def name(self) -> str:
        return "TachibanaE-API"

    # ─── 共通ガード ────────────────────────────────────────────────────────────

    async def _ensure_and_guard(self, method_name: str) -> str:
        """
        セッション確保 + 取引可否チェック + 仮想 URL 取得（url_request）。

        Raises:
            BrokerAuthError: ログイン失敗
            BrokerAPIError:  取引不可 or 仮想 URL 空
        """
        try:
            await self._session.ensure_session()
        except BrokerAuthError:
            logger.error("%s: ログイン失敗", method_name)
            raise

        if not self._session.is_usable:
            raise BrokerAPIError(
                "立花証券セッションが取引不可状態です "
                "(sKinsyouhouMidokuFlg=1: 禁止事項の未読通知あり)。"
                "立花証券 Web サイトにログインして通知を確認してください。"
            )

        url = self._session.url_request
        if not url:
            raise BrokerAPIError(
                "仮想 URL が空です。セッションが正常に確立されていない可能性があります。"
            )
        return url

    def _handle_auth_error(self, method_name: str, context: str = "") -> None:
        """認証エラー時のセッション無効化 + ログ。"""
        logger.warning(
            "%s: 認証エラー。セッションを無効化します %s",
            method_name,
            context,
        )
        self._session.invalidate()

    # ─── place_order ──────────────────────────────────────────────────────────

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        """
        現物成行 / 指値注文を発注する。

        対応している注文種別:
          - 現物買 (side=BUY, account_type=cash)
          - 現物売 (side=SELL, account_type=cash)
          - 成行 (order_type=MARKET)
          - 指値 (order_type=LIMIT)

        NOTE: 信用取引 (account_type=margin) は mapper 上は変換できるが、
        本番でのテストは未実施。現物のみ正式サポート。

        タイムアウト発生時は BrokerTemporaryError を送出して終了する。
        自動再送はしない。注文の到達可否は不確定のため OrderRouter が
        Order を SUBMITTED 状態で保留し RecoveryManager に委ねること。

        Raises:
            BrokerTemporaryError:   タイムアウト・ネットワークエラー
            BrokerAuthError:        認証失敗（セッション無効化済み）
            BrokerMaintenanceError: メンテナンス中
            BrokerAPIError:         その他 API エラー（残高不足等を含む）
            ValueError:             未対応の side / account_type 組み合わせ
        """
        # ── 1. セッション確保 + ガード ────────────────────────────────────────
        request_url = await self._ensure_and_guard("place_order")

        # ── 2. リクエスト payload 構築 ────────────────────────────────────────
        payload = mapper.map_new_order_request(
            request,
            second_password=self._session.second_password,
            tax_type=self._settings.TACHIBANA_DEFAULT_TAX_TYPE,
            market_code=self._settings.TACHIBANA_DEFAULT_MARKET,
        )

        logger.info(
            "place_order: 発注 ticker=%s side=%s order_type=%s qty=%d url=%s",
            request.ticker,
            request.side,
            request.order_type,
            request.quantity,
            request_url,
        )

        # ── 3. 送信（タイムアウト時は再送しない） ─────────────────────────────
        try:
            raw = await self._client.request(request_url, payload)
        except BrokerAuthError:
            self._handle_auth_error("place_order", f"ticker={request.ticker}")
            raise

        # ── 4. レスポンス変換 ─────────────────────────────────────────────────
        response = mapper.map_order_response(raw)

        logger.info(
            "place_order: 発注受付 ticker=%s broker_order_id=%s",
            request.ticker,
            response.broker_order_id,
        )
        return response

    # ─── cancel_order ─────────────────────────────────────────────────────────

    async def cancel_order(self, broker_order_id: str) -> CancelResult:
        """
        注文取消リクエストを送信する。

        取消モデル:
          立花証券 e_api の取消は非同期モデル。
          正常応答（sResultCode=0）は「取消受付済み」であり「取消完了」ではない。
          → CancelResult(success=True, is_pending=True) を返す。
          取消完了（sOrderStatusCode=7 への遷移）は OrderPoller / get_order_status が確認する。

        設計制約（place_order / get_order_status と共通）:
          - タイムアウト時は BrokerTemporaryError を送出して終了する。自動再送しない。
          - BrokerAuthError 時はセッションを無効化して再スロー。
          - OrderStatus enum に CANCEL_PENDING は追加しない。
            取消申請中の注文は sOrderStatusCode="6" → SUBMITTED で照会し続ける設計。

        仕様未確定 NOTE（本番投入前に仕様書で確認すること）:
          - NOTE: sCLMID "CLMKabuCancelOrder" は推定。mapper.py の TODO を参照。
          - NOTE: sSecondPassword の要否は仕様書未確認。必須と推定して送信する。

        Raises:
            BrokerTemporaryError:   タイムアウト・ネットワークエラー
            BrokerAuthError:        認証失敗（セッション無効化済み）
            BrokerMaintenanceError: メンテナンス中
            BrokerAPIError:         その他 API エラー（取消不可状態等を含む）
            ValueError:             broker_order_id のフォーマットが不正
        """
        # ── 1. セッション確保 + ガード ────────────────────────────────────────
        request_url = await self._ensure_and_guard("cancel_order")

        # ── 2. broker_order_id デコード ────────────────────────────────────────
        eigyou_day, order_number = mapper.decode_broker_order_id(broker_order_id)

        # ── 3. リクエスト payload 構築 ────────────────────────────────────────
        payload = mapper.map_cancel_request(
            eigyou_day=eigyou_day,
            order_number=order_number,
            second_password=self._session.second_password,
        )

        logger.info(
            "cancel_order: 取消 broker_order_id=%s url=%s",
            broker_order_id,
            request_url,
        )

        # ── 4. 送信 ───────────────────────────────────────────────────────────
        try:
            _raw = await self._client.request(request_url, payload)
        except BrokerAuthError:
            self._handle_auth_error("cancel_order", f"broker_order_id={broker_order_id}")
            raise

        logger.info(
            "cancel_order: 取消受付 broker_order_id=%s (is_pending=True)",
            broker_order_id,
        )
        return CancelResult(success=True, is_pending=True)

    # ─── get_order_status ─────────────────────────────────────────────────────

    async def get_order_status(self, broker_order_id: str) -> OrderStatusResponse:
        """
        注文状態を照会して OrderStatusResponse を返す。

        仕様書確認済み:
          sCLMID: CLMOrderListDetail
          URL:    sUrlRequest
          ステータスコード: sOrderStatusCode（旧: sState）

        設計制約（place_order と共通）:
          - タイムアウト時は BrokerTemporaryError を送出して終了する。自動再送しない。
          - BrokerAuthError 時はセッションを無効化して再スロー。
          - 未知の sOrderStatusCode は UNKNOWN として返す（安全フォールバック）。

        Raises:
            BrokerTemporaryError:   タイムアウト・ネットワークエラー
            BrokerAuthError:        認証失敗（セッション無効化済み）
            BrokerMaintenanceError: メンテナンス中
            BrokerAPIError:         その他 API エラー
            ValueError:             broker_order_id のフォーマットが不正
        """
        # ── 1. セッション確保 + ガード ────────────────────────────────────────
        request_url = await self._ensure_and_guard("get_order_status")

        # ── 2. broker_order_id デコード ────────────────────────────────────────
        eigyou_day, order_number = mapper.decode_broker_order_id(broker_order_id)

        # ── 3. リクエスト payload 構築 ────────────────────────────────────────
        payload = {
            "sCLMID":       "CLMOrderListDetail",  # 仕様書確認済み
            "sEigyouDay":   eigyou_day,
            "sOrderNumber": order_number,
        }

        logger.info(
            "get_order_status: 照会 broker_order_id=%s url=%s",
            broker_order_id,
            request_url,
        )

        # ── 4. 送信 ───────────────────────────────────────────────────────────
        try:
            raw = await self._client.request(request_url, payload)
        except BrokerAuthError:
            self._handle_auth_error("get_order_status", f"broker_order_id={broker_order_id}")
            raise

        # ── 5. レスポンス変換 ─────────────────────────────────────────────────
        response = mapper.map_order_status(raw)

        logger.info(
            "get_order_status: 照会完了 broker_order_id=%s status=%s filled_qty=%d",
            broker_order_id,
            response.status,
            response.filled_quantity,
        )
        return response

    # ─── get_positions ────────────────────────────────────────────────────────

    async def get_positions(self) -> list[BrokerPosition]:
        """
        現物保有 + 信用建玉を照会して統合した BrokerPosition リストを返す。

        照会モデル（2 API 呼び出し）:
          1. 現物保有照会 (CLMGenbutuKabuList)  → 現物株の保有一覧
          2. 信用建玉照会 (CLMShinyouTategyokuList) → 信用建玉の一覧
          両者の結果を結合して返す。

        仕様書確認済み:
          sCLMID (現物): CLMGenbutuKabuList
          sCLMID (信用): CLMShinyouTategyokuList

        仕様未確定 NOTE:
          - 信用口座なしの場合 CLMShinyouTategyokuList がエラーを返す可能性あり。
            将来的に MARGIN_TRADING_ENABLED 設定フラグを追加して信用照会をスキップ可能にすること。
            TODO: 仕様書で信用口座なし時の挙動を確認すること。

        Raises:
            BrokerTemporaryError:   タイムアウト・ネットワークエラー
            BrokerAuthError:        認証失敗（セッション無効化済み）
            BrokerMaintenanceError: メンテナンス中
            BrokerAPIError:         その他 API エラー
        """
        # ── 1. セッション確保 + ガード ────────────────────────────────────────
        request_url = await self._ensure_and_guard("get_positions")

        positions: list[BrokerPosition] = []

        # ── 2. 現物保有照会 ───────────────────────────────────────────────────
        spot_payload = {"sCLMID": "CLMGenbutuKabuList"}
        logger.info("get_positions: 現物保有照会 url=%s", request_url)
        try:
            raw_spot = await self._client.request(request_url, spot_payload)
        except BrokerAuthError:
            self._handle_auth_error("get_positions", "現物照会")
            raise

        positions.extend(mapper.parse_spot_positions_response(raw_spot))

        # ── 3. 信用建玉照会 ───────────────────────────────────────────────────
        margin_payload = {"sCLMID": "CLMShinyouTategyokuList"}
        logger.info("get_positions: 信用建玉照会 url=%s", request_url)
        try:
            raw_margin = await self._client.request(request_url, margin_payload)
        except BrokerAuthError:
            self._handle_auth_error("get_positions", "信用照会")
            raise

        positions.extend(mapper.parse_margin_positions_response(raw_margin))

        logger.info("get_positions: 照会完了 spot+margin count=%d", len(positions))
        return positions

    # ─── get_balance ──────────────────────────────────────────────────────────

    async def get_balance(self) -> BalanceInfo:
        """
        口座残高を照会して BalanceInfo を返す。

        2 API モデル:
          1. CLMZanKaiKanougaku → 現物買付可能額（必須）
          2. CLMZanShinkiKanoIjiritu → 信用新規建余力（オプション: 失敗時は 0 にデグレード）

        1 回目（現物）が失敗した場合は例外を伝播する。
        2 回目（信用）が失敗した場合は margin_available=0 でデグレード（信用口座なし想定）。

        仕様書確認済み:
          sCLMID (現物): CLMZanKaiKanougaku
          sCLMID (信用): CLMZanShinkiKanoIjiritu

        暫定:
          total_equity フィールドは仕様書未確認のため 0.0 を返す。
          TODO: 仕様書で純資産相当フィールドを確認すること。

        Raises:
            BrokerTemporaryError:   タイムアウト・ネットワークエラー
            BrokerAuthError:        認証失敗（セッション無効化済み）
            BrokerMaintenanceError: メンテナンス中
            BrokerAPIError:         その他 API エラー
        """
        # ── 1. セッション確保 + ガード ────────────────────────────────────────
        request_url = await self._ensure_and_guard("get_balance")

        # ── 2. 現物余力照会（必須） ───────────────────────────────────────────
        cash_payload = {"sCLMID": "CLMZanKaiKanougaku"}
        logger.info("get_balance: 現物余力照会 url=%s", request_url)
        try:
            raw_cash = await self._client.request(request_url, cash_payload)
        except BrokerAuthError:
            self._handle_auth_error("get_balance", "現物余力照会")
            raise

        # ── 3. 信用余力照会（オプション: 失敗時は 0 にデグレード） ───────────
        raw_margin: Optional[dict] = None
        margin_payload = {"sCLMID": "CLMZanShinkiKanoIjiritu"}
        logger.info("get_balance: 信用余力照会 url=%s", request_url)
        try:
            raw_margin = await self._client.request(request_url, margin_payload)
        except BrokerAuthError:
            self._handle_auth_error("get_balance", "信用余力照会")
            raise
        except Exception as exc:
            # 信用口座なし等でエラーになる場合は margin_available=0 でデグレード
            logger.warning(
                "get_balance: 信用余力照会でエラー（margin_available=0 にデグレード）: %s", exc
            )

        # ── 4. レスポンス変換 ─────────────────────────────────────────────────
        balance = mapper.map_balance(raw_cash, raw_margin)

        logger.info(
            "get_balance: 照会完了 cash=%.0f margin=%.0f",
            balance.cash_balance,
            balance.margin_available,
        )
        return balance

    # ─── get_market_price ─────────────────────────────────────────────────────

    async def get_market_price(self, ticker: str) -> float | None:
        """
        銘柄の現在価格を照会して返す。

        価格が取得できない正常系（取引時間外・データなし等）は None を返す。
        ExitWatcher は None を受け取ると TP/SL をスキップし TimeStop のみ発火する。

        仕様書確認済み:
          sCLMID:  CLMMfdsGetMarketPrice
          URL:     sUrlPrice
          リクエスト: sTargetIssueCode（銘柄コード）+ sTargetColumn（取得フィールド）
          レスポンス: aCLMMfdsMarketPrice 配列の先頭要素 pDPP

        暫定:
          sTargetColumn = "pDPP" は推定値。
          TODO: 仕様書の CLMMfdsGetMarketPrice リクエスト定義を確認すること。

        設計制約（他メソッドと共通）:
          - タイムアウト時は BrokerTemporaryError を送出。自動再送しない。
          - BrokerAuthError 時はセッションを無効化して再スロー。
          - 価格フィールドが 0 / 欠損の場合は None を返す（安全側）。

        Args:
            ticker: 銘柄コード（例: "7203"）

        Returns:
            現在価格（円）。価格が取得不能な場合は None。

        Raises:
            BrokerTemporaryError:   タイムアウト・ネットワークエラー
            BrokerAuthError:        認証失敗（セッション無効化済み）
            BrokerMaintenanceError: メンテナンス中
            BrokerAPIError:         その他 API エラー
        """
        # ── 1. セッション確保 + 取引可否チェック ─────────────────────────────
        try:
            await self._session.ensure_session()
        except BrokerAuthError:
            logger.error("get_market_price: ログイン失敗 ticker=%s", ticker)
            raise

        if not self._session.is_usable:
            raise BrokerAPIError(
                "立花証券セッションが取引不可状態です "
                "(sKinsyouhouMidokuFlg=1: 禁止事項の未読通知あり)。"
                "立花証券 Web サイトにログインして通知を確認してください。"
            )

        # ── 2. 価格照会専用 URL (sUrlPrice) を取得 ────────────────────────────
        price_url = self._session.url_price
        if not price_url:
            raise BrokerAPIError(
                "仮想 URL が空です。セッションが正常に確立されていない可能性があります。"
            )

        # ── 3. リクエスト payload 構築 ────────────────────────────────────────
        # sTargetColumn = "pDPP" は暫定値。仕様書確認後に更新すること。
        payload = {
            "sCLMID":          "CLMMfdsGetMarketPrice",  # 仕様書確認済み
            "sTargetIssueCode": ticker,                   # 仕様書確認済み
            "sTargetColumn":   "pDPP",                   # 暫定: TODO 仕様書確認
        }

        logger.info("get_market_price: 価格照会 ticker=%s url=%s", ticker, price_url)

        # ── 4. 送信 ───────────────────────────────────────────────────────────
        try:
            raw = await self._client.request(price_url, payload)
        except BrokerAuthError:
            self._handle_auth_error("get_market_price", f"ticker={ticker}")
            raise

        # ── 5. レスポンス変換 ─────────────────────────────────────────────────
        price = mapper.map_market_price(raw)

        logger.info(
            "get_market_price: 照会完了 ticker=%s price=%s",
            ticker,
            price,
        )
        return price
