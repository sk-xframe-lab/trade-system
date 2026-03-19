"""
TachibanaBrokerAdapter.cancel_order テスト

テスト方針:
  - TachibanaClient.request と TachibanaSessionManager を mock して隔離
  - 取消受付モデル（is_pending=True）の検証
  - place_order / get_order_status と同一エラーパターンを検証
  - 取消完了判定は OrderPoller / get_order_status に委ねる設計の確認
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from trade_app.brokers.base import (
    BrokerAPIError,
    BrokerAuthError,
    BrokerMaintenanceError,
    BrokerTemporaryError,
    CancelResult,
)
from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter
from trade_app.brokers.tachibana.client import TachibanaClient
from trade_app.brokers.tachibana.session import TachibanaSessionManager


# ─── テスト用ファクトリ ──────────────────────────────────────────────────────

def _make_client() -> MagicMock:
    client = MagicMock(spec=TachibanaClient)
    client.request = AsyncMock()
    return client


def _make_session(
    is_usable: bool = True,
    url_request: str = "https://virtual.example.com/order",
    second_password: str = "pass2nd",
) -> MagicMock:
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
    if client is None:
        client = _make_client()
    if session is None:
        session = _make_session()
    adapter = TachibanaBrokerAdapter.__new__(TachibanaBrokerAdapter)
    adapter._client = client
    adapter._session = session
    settings = MagicMock()
    settings.TACHIBANA_DEFAULT_TAX_TYPE = "3"
    settings.TACHIBANA_DEFAULT_MARKET = "00"
    adapter._settings = settings
    return adapter


def _make_cancel_response() -> dict:
    """取消受付成功レスポンス（最小限フィールド）"""
    return {
        "sResultCode": "0",
        "sResultText": "取消受付",
    }


_BROKER_ORDER_ID = "20260316_00123"


# ─── 正常系: 取消受付モデルの検証 ─────────────────────────────────────────────

class TestCancelOrderSuccess:
    """cancel_order 正常系 — 取消受付モデルの検証"""

    @pytest.mark.asyncio
    async def test_returns_cancel_result(self):
        """戻り値が CancelResult 型である"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        adapter = _make_adapter(client=client)

        result = await adapter.cancel_order(_BROKER_ORDER_ID)

        assert isinstance(result, CancelResult)

    @pytest.mark.asyncio
    async def test_success_is_true(self):
        """取消受付成功時 success=True"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        adapter = _make_adapter(client=client)

        result = await adapter.cancel_order(_BROKER_ORDER_ID)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_is_pending_is_true(self):
        """取消受付は「受付済み・未完了」= is_pending=True"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        adapter = _make_adapter(client=client)

        result = await adapter.cancel_order(_BROKER_ORDER_ID)

        assert result.is_pending is True

    @pytest.mark.asyncio
    async def test_is_already_terminal_is_false(self):
        """取消受付成功時は is_already_terminal=False（終端状態ではない）"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        adapter = _make_adapter(client=client)

        result = await adapter.cancel_order(_BROKER_ORDER_ID)

        assert result.is_already_terminal is False

    @pytest.mark.asyncio
    async def test_uses_url_order(self):
        """取消は注文系仮想 URL (url_request) を使って送信する"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        session = _make_session(url_request="https://virtual.example.com/order/F1")
        adapter = _make_adapter(client=client, session=session)

        await adapter.cancel_order(_BROKER_ORDER_ID)

        url_used = client.request.call_args[0][0]
        assert url_used == "https://virtual.example.com/order/F1"

    @pytest.mark.asyncio
    async def test_payload_contains_eigyou_day_and_order_number(self):
        """payload に sEigyouDay と sOrderNumber が含まれる"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        adapter = _make_adapter(client=client)

        await adapter.cancel_order("20260316_00456")

        payload = client.request.call_args[0][1]
        assert payload["sEigyouDay"] == "20260316"
        assert payload["sOrderNumber"] == "00456"

    @pytest.mark.asyncio
    async def test_second_password_is_included(self):
        """second_password が payload に含まれる"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        session = _make_session(second_password="mySecretPass2")
        adapter = _make_adapter(client=client, session=session)

        await adapter.cancel_order(_BROKER_ORDER_ID)

        payload = client.request.call_args[0][1]
        assert payload.get("sSecondPassword") == "mySecretPass2"

    @pytest.mark.asyncio
    async def test_ensure_session_is_called(self):
        """cancel_order は必ず ensure_session を呼ぶ"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        await adapter.cancel_order(_BROKER_ORDER_ID)

        session.ensure_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_auto_retry(self):
        """正常時は client.request を 1 回しか呼ばない（自動再送なし）"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        adapter = _make_adapter(client=client)

        await adapter.cancel_order(_BROKER_ORDER_ID)

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_different_broker_order_ids_decode_correctly(self):
        """異なる broker_order_id でも正しく decode される"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        adapter = _make_adapter(client=client)

        await adapter.cancel_order("20260317_00999")

        payload = client.request.call_args[0][1]
        assert payload["sEigyouDay"] == "20260317"
        assert payload["sOrderNumber"] == "00999"


# ─── エラー系 ───────────────────────────────────────────────────────────────

class TestCancelOrderErrors:
    """cancel_order エラー系"""

    @pytest.mark.asyncio
    async def test_timeout_raises_broker_temporary_error(self):
        """タイムアウト時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Request timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

    @pytest.mark.asyncio
    async def test_network_error_raises_broker_temporary_error(self):
        """ネットワークエラー時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Network error")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

    @pytest.mark.asyncio
    async def test_auth_error_raises_and_invalidates_session(self):
        """認証エラー時に BrokerAuthError が送出され、セッションが無効化される"""
        client = _make_client()
        client.request.side_effect = BrokerAuthError("sResultCode=E001")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

        session.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_maintenance_raises_broker_maintenance_error(self):
        """メンテナンス中に BrokerMaintenanceError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerMaintenanceError("sResultCode=E999")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerMaintenanceError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

    @pytest.mark.asyncio
    async def test_api_error_propagates(self):
        """取消不可状態等の API エラーが BrokerAPIError として伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerAPIError("sResultCode=X001 取消不可")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerAPIError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_timeout(self):
        """タイムアウト時に自動再送しない（client.request は 1 回のみ）"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Request timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_network_error(self):
        """ネットワークエラー時も自動再送しない"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Network error")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_auth_error_does_not_retry(self):
        """認証エラー時も自動再送しない"""
        client = _make_client()
        client.request.side_effect = BrokerAuthError("sResultCode=E001")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_session_not_invalidated_on_non_auth_error(self):
        """認証エラー以外のエラーではセッションを無効化しない"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerTemporaryError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

        session.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_kinsyouhou_midoku_raises_broker_api_error(self):
        """is_usable=False（sKinsyouhouMidokuFlg=1）時に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(is_usable=False)
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="sKinsyouhouMidokuFlg"):
            await adapter.cancel_order(_BROKER_ORDER_ID)

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_url_order_raises_broker_api_error(self):  # noqa: N802 (kept for backward compat)
        """仮想 URL が空の場合に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(url_request="")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="仮想 URL"):
            await adapter.cancel_order(_BROKER_ORDER_ID)

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_broker_order_id_raises_value_error(self):
        """broker_order_id にセパレータ '_' がない場合 ValueError を送出する"""
        adapter = _make_adapter()

        with pytest.raises(ValueError):
            await adapter.cancel_order("nodateseparator")

    @pytest.mark.asyncio
    async def test_login_failure_propagates_auth_error(self):
        """ensure_session() がログイン失敗で BrokerAuthError を送出する場合に伝播する"""
        client = _make_client()
        session = _make_session()
        session.ensure_session.side_effect = BrokerAuthError("login failed")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.cancel_order(_BROKER_ORDER_ID)

        client.request.assert_not_awaited()


# ─── 取消完了判定は poller に委ねる設計の確認 ────────────────────────────────

class TestCancelOrderPendingModel:
    """
    取消非同期モデルの設計検証。

    cancel_order はあくまでも「取消受付」を行うだけ。
    取消が実際に完了したかどうか（sState=CANCELLED への遷移）は
    OrderPoller → get_order_status が確認する。
    """

    @pytest.mark.asyncio
    async def test_success_and_pending_together(self):
        """success=True かつ is_pending=True が同時に成立する"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        adapter = _make_adapter(client=client)

        result = await adapter.cancel_order(_BROKER_ORDER_ID)

        # 取消受付: 成功しているが完了はまだ
        assert result.success is True
        assert result.is_pending is True
        assert result.is_already_terminal is False

    @pytest.mark.asyncio
    async def test_cancel_does_not_call_get_order_status(self):
        """cancel_order は get_order_status を呼ばない（1回のリクエストで完結）"""
        client = _make_client()
        client.request.return_value = _make_cancel_response()
        adapter = _make_adapter(client=client)

        await adapter.cancel_order(_BROKER_ORDER_ID)

        # client.request は 1 回のみ（取消リクエスト1本のみ）
        assert client.request.await_count == 1
