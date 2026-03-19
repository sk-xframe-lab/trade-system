"""
TachibanaBrokerAdapter.place_order テスト

テスト方針:
  - TachibanaClient.request と TachibanaSessionManager を mock して隔離
  - ネットワーク通信・実際の認証は一切行わない
  - adapter.py の place_order の責務のみを検証する
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from trade_app.brokers.base import (
    BrokerAPIError,
    BrokerAuthError,
    BrokerMaintenanceError,
    BrokerTemporaryError,
    OrderRequest,
    OrderResponse,
)
from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter
from trade_app.brokers.tachibana.client import TachibanaClient
from trade_app.brokers.tachibana.session import TachibanaSessionManager
from trade_app.models.enums import OrderStatus, OrderType, Side


# ─── テスト用ファクトリ ──────────────────────────────────────────────────────

def _make_client() -> MagicMock:
    """TachibanaClient のモック"""
    client = MagicMock(spec=TachibanaClient)
    client.request = AsyncMock()
    return client


def _make_session(
    is_usable: bool = True,
    url_request: str = "https://virtual.example.com/request",
    second_password: str = "pass2nd",
) -> MagicMock:
    """TachibanaSessionManager のモック"""
    session = MagicMock(spec=TachibanaSessionManager)
    session.ensure_session = AsyncMock()
    session.is_usable = is_usable
    session.url_request = url_request
    session.second_password = second_password
    session.invalidate = MagicMock()
    return session


def _make_adapter(
    client: MagicMock | None = None,
    session: MagicMock | None = None,
) -> TachibanaBrokerAdapter:
    """テスト用 TachibanaBrokerAdapter（DI 経由）"""
    if client is None:
        client = _make_client()
    if session is None:
        session = _make_session()
    # config.get_settings() を呼ばないよう直接 DI
    adapter = TachibanaBrokerAdapter.__new__(TachibanaBrokerAdapter)
    adapter._client = client
    adapter._session = session
    # settings は mock（必要な属性のみ設定）
    settings = MagicMock()
    settings.TACHIBANA_DEFAULT_TAX_TYPE = "3"
    settings.TACHIBANA_DEFAULT_MARKET = "00"
    adapter._settings = settings
    return adapter


def _make_request(
    ticker: str = "7203",
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    quantity: int = 100,
    limit_price: float | None = None,
    account_type: str = "cash",
) -> OrderRequest:
    return OrderRequest(
        client_order_id="test-order-001",
        ticker=ticker,
        side=side,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
        account_type=account_type,
    )


def _make_raw_response(
    eigyou_day: str = "20260316",
    order_number: str = "00123",
) -> dict:
    """CLMKabuNewOrder の正常レスポンス（最小限フィールド）"""
    return {
        "sResultCode": "0",
        "sResultText": "正常",
        "sEigyouDay": eigyou_day,
        "sOrderNumber": order_number,
    }


# ─── 正常系テスト ────────────────────────────────────────────────────────────

class TestPlaceOrderSuccess:
    """place_order 正常系"""

    @pytest.mark.asyncio
    async def test_returns_order_response(self):
        """place_order が正常応答を OrderResponse に変換できる"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        adapter = _make_adapter(client=client)

        result = await adapter.place_order(_make_request())

        assert isinstance(result, OrderResponse)

    @pytest.mark.asyncio
    async def test_broker_order_id_format(self):
        """broker_order_id が '{sEigyouDay}_{sOrderNumber}' 形式になる"""
        client = _make_client()
        client.request.return_value = _make_raw_response(
            eigyou_day="20260316", order_number="00456"
        )
        adapter = _make_adapter(client=client)

        result = await adapter.place_order(_make_request())

        assert result.broker_order_id == "20260316_00456"

    @pytest.mark.asyncio
    async def test_uses_session_virtual_url(self):
        """SessionManager の url_request（仮想 URL）を使って送信する"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        session = _make_session(url_request="https://virtual.example.com/request/F0")
        adapter = _make_adapter(client=client, session=session)

        await adapter.place_order(_make_request())

        # client.request の第1引数が仮想 URL であること
        url_used = client.request.call_args[0][0]
        assert url_used == "https://virtual.example.com/request/F0"

    @pytest.mark.asyncio
    async def test_second_password_is_included(self):
        """second_password が payload に含まれる"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        session = _make_session(second_password="mySecretPass2")
        adapter = _make_adapter(client=client, session=session)

        await adapter.place_order(_make_request())

        payload = client.request.call_args[0][1]
        assert payload.get("sSecondPassword") == "mySecretPass2"

    @pytest.mark.asyncio
    async def test_status_is_submitted(self):
        """発注受付後のステータスは SUBMITTED"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        adapter = _make_adapter(client=client)

        result = await adapter.place_order(_make_request())

        assert result.status == OrderStatus.SUBMITTED.value

    @pytest.mark.asyncio
    async def test_ensure_session_is_called(self):
        """place_order は必ず ensure_session を呼ぶ"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        await adapter.place_order(_make_request())

        session.ensure_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cash_buy_market(self):
        """現物買・成行の payload に正しいフィールドが含まれる"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        adapter = _make_adapter(client=client)

        await adapter.place_order(
            _make_request(ticker="7203", side=Side.BUY, order_type=OrderType.MARKET, quantity=100)
        )

        payload = client.request.call_args[0][1]
        # 銘柄コード
        assert payload.get("sIssueCode") == "7203"
        # 成行: 注文価格は "0"
        assert payload.get("sOrderPrice") == "0"
        # 数量
        assert payload.get("sOrderSuryou") == "100"

    @pytest.mark.asyncio
    async def test_cash_sell_limit(self):
        """現物売・指値の payload に正しいフィールドが含まれる"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        adapter = _make_adapter(client=client)

        await adapter.place_order(
            _make_request(
                ticker="9984", side=Side.SELL, order_type=OrderType.LIMIT, quantity=200, limit_price=1500.0
            )
        )

        payload = client.request.call_args[0][1]
        assert payload.get("sIssueCode") == "9984"
        assert payload.get("sOrderPrice") == "1500.0"
        assert payload.get("sOrderSuryou") == "200"

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_success(self):
        """正常時は client.request を 1 回しか呼ばない（自動再送なし）"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        adapter = _make_adapter(client=client)

        await adapter.place_order(_make_request())

        assert client.request.await_count == 1


# ─── エラー系テスト ──────────────────────────────────────────────────────────

class TestPlaceOrderErrors:
    """place_order エラー系"""

    @pytest.mark.asyncio
    async def test_timeout_raises_broker_temporary_error(self):
        """タイムアウト時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Request timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_network_error_raises_broker_temporary_error(self):
        """ネットワークエラー時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Network error")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_auth_error_raises_and_invalidates_session(self):
        """認証エラー時に BrokerAuthError が送出され、セッションが無効化される"""
        client = _make_client()
        client.request.side_effect = BrokerAuthError("sResultCode=E001")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.place_order(_make_request())

        # セッションが無効化されていること
        session.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_maintenance_raises_broker_maintenance_error(self):
        """メンテナンス中に BrokerMaintenanceError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerMaintenanceError("sResultCode=E999")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerMaintenanceError):
            await adapter.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_api_error_propagates(self):
        """その他の API エラー（残高不足等）が BrokerAPIError として伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerAPIError("sResultCode=B001 残高不足")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerAPIError):
            await adapter.place_order(_make_request())

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_timeout(self):
        """タイムアウト時に自動再送しない（client.request は 1 回のみ）"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Request timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.place_order(_make_request())

        # 再送なし: 1回のみ
        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_network_error(self):
        """ネットワークエラー時にも自動再送しない"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Network error")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.place_order(_make_request())

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_auth_error_does_not_retry(self):
        """認証エラー時も自動再送しない"""
        client = _make_client()
        client.request.side_effect = BrokerAuthError("sResultCode=E001")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.place_order(_make_request())

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_session_not_invalidated_on_non_auth_error(self):
        """認証エラー以外のエラーではセッションを無効化しない"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerTemporaryError):
            await adapter.place_order(_make_request())

        session.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_kinsyouhou_midoku_raises_broker_api_error(self):
        """is_usable=False（sKinsyouhouMidokuFlg=1）時に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(is_usable=False)
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="sKinsyouhouMidokuFlg"):
            await adapter.place_order(_make_request())

        # クライアントは呼ばれない
        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_order_url_raises_broker_api_error(self):
        """仮想 URL が空の場合に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(url_request="")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="仮想 URL"):
            await adapter.place_order(_make_request())

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_login_failure_propagates_auth_error(self):
        """ensure_session() がログイン失敗で BrokerAuthError を送出する場合に伝播する"""
        client = _make_client()
        session = _make_session()
        session.ensure_session.side_effect = BrokerAuthError("login failed")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.place_order(_make_request())

        # ログイン失敗時はクライアントを呼ばない
        client.request.assert_not_awaited()


# ─── stub メソッドのテスト ────────────────────────────────────────────────────

class TestUnimplementedStubs:
    """未実装メソッドが NotImplementedError を送出することを確認する"""

    # cancel_order は Phase 10-C で実装済み。
    # 詳細テストは test_tachibana_adapter_cancel_order.py を参照。

    # get_order_status は Phase 10-C で実装済み。
    # 詳細テストは test_tachibana_adapter_get_order_status.py を参照。

    # get_balance は Phase 10-C で実装済み。
    # 詳細テストは test_tachibana_adapter_get_balance.py を参照。

    # get_positions は Phase 10 get_positions で実装済み。
    # 詳細テストは test_tachibana_adapter_get_positions.py を参照。

    # get_market_price は Phase 10 get_market_price で実装済み。
    # 詳細テストは test_tachibana_adapter_get_market_price.py を参照。


# ─── adapter 名のテスト ──────────────────────────────────────────────────────

class TestAdapterProperties:
    def test_name(self):
        adapter = _make_adapter()
        assert adapter.name == "TachibanaE-API"
