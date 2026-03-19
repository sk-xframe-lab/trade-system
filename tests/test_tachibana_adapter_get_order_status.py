"""
TachibanaBrokerAdapter.get_order_status テスト

テスト方針:
  - TachibanaClient.request と TachibanaSessionManager を mock して隔離
  - OrderPoller が消費する OrderStatusResponse の各フィールドを検証
  - sOrderStatusCode 未知コードの UNKNOWN フォールバックを確認
  - place_order テストと同じエラーパターン（BrokerAuth/Temporary/Maintenance/API）を検証
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from trade_app.brokers.base import (
    BrokerAPIError,
    BrokerAuthError,
    BrokerMaintenanceError,
    BrokerTemporaryError,
    OrderStatusResponse,
)
from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter
from trade_app.brokers.tachibana.client import TachibanaClient
from trade_app.brokers.tachibana.session import TachibanaSessionManager
from trade_app.models.enums import OrderStatus


# ─── テスト用ファクトリ ──────────────────────────────────────────────────────

def _make_client() -> MagicMock:
    client = MagicMock(spec=TachibanaClient)
    client.request = AsyncMock()
    return client


def _make_session(
    is_usable: bool = True,
    url_request: str = "https://virtual.example.com/request",
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


def _make_raw_response(
    eigyou_day: str = "20260316",
    order_number: str = "00123",
    state: str = "1",
    order_qty: str = "100",
    filled_qty: str = "0",
    filled_amount: str = "0",
    cancel_qty: str = "0",
    yakuzyou_list: list | None = None,
) -> dict:
    """注文照会 API の正常レスポンス（最小限フィールド、推定フィールド名）"""
    raw = {
        "sResultCode":       "0",
        "sResultText":       "正常",
        "sEigyouDay":        eigyou_day,
        "sOrderNumber":      order_number,
        "sOrderStatusCode":  state,
        "sOrderSuryou":      order_qty,
        "sYakuzyouSuryou":   filled_qty,
        "sYakuzyouKingaku":  filled_amount,
        "sCancelSuryou":     cancel_qty,
    }
    if yakuzyou_list is not None:
        raw["aYakuzyouSikkouList"] = yakuzyou_list
    return raw


_BROKER_ORDER_ID = "20260316_00123"


# ─── 正常系: OrderStatusResponse の基本フィールド ────────────────────────────

class TestGetOrderStatusSuccess:
    """get_order_status 正常系"""

    @pytest.mark.asyncio
    async def test_returns_order_status_response(self):
        """戻り値が OrderStatusResponse 型である"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert isinstance(result, OrderStatusResponse)

    @pytest.mark.asyncio
    async def test_broker_order_id_preserved(self):
        """broker_order_id が '{sEigyouDay}_{sOrderNumber}' のまま返る"""
        client = _make_client()
        client.request.return_value = _make_raw_response(
            eigyou_day="20260316", order_number="00456"
        )
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status("20260316_00456")

        assert result.broker_order_id == "20260316_00456"

    @pytest.mark.asyncio
    async def test_uses_session_url_request(self):
        """SessionManager の url_request（照会系仮想 URL）を使って送信する"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        session = _make_session(url_request="https://virtual.example.com/req/F0")
        adapter = _make_adapter(client=client, session=session)

        await adapter.get_order_status(_BROKER_ORDER_ID)

        url_used = client.request.call_args[0][0]
        assert url_used == "https://virtual.example.com/req/F0"

    @pytest.mark.asyncio
    async def test_payload_contains_eigyou_day_and_order_number(self):
        """payload に sEigyouDay と sOrderNumber が含まれる"""
        client = _make_client()
        client.request.return_value = _make_raw_response(
            eigyou_day="20260316", order_number="00789"
        )
        adapter = _make_adapter(client=client)

        await adapter.get_order_status("20260316_00789")

        payload = client.request.call_args[0][1]
        assert payload["sEigyouDay"] == "20260316"
        assert payload["sOrderNumber"] == "00789"

    @pytest.mark.asyncio
    async def test_status_submitted(self):
        """sState='1' → status=SUBMITTED"""
        client = _make_client()
        client.request.return_value = _make_raw_response(state="1")
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.status == OrderStatus.SUBMITTED

    @pytest.mark.asyncio
    async def test_status_partial(self):
        """sOrderStatusCode='9' → status=PARTIAL、filled_quantity が反映される"""
        client = _make_client()
        client.request.return_value = _make_raw_response(
            state="9", order_qty="100", filled_qty="30", filled_amount="90000"
        )
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.status == OrderStatus.PARTIAL
        assert result.filled_quantity == 30

    @pytest.mark.asyncio
    async def test_status_filled(self):
        """sOrderStatusCode='10' → status=FILLED"""
        client = _make_client()
        client.request.return_value = _make_raw_response(
            state="10", order_qty="100", filled_qty="100", filled_amount="300000"
        )
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.status == OrderStatus.FILLED
        assert result.filled_quantity == 100

    @pytest.mark.asyncio
    async def test_status_cancelled(self):
        """sOrderStatusCode='7' → status=CANCELLED"""
        client = _make_client()
        client.request.return_value = _make_raw_response(state="7")
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_status_submitted_mid_range(self):
        """sOrderStatusCode='6'（旧コード）→ status=SUBMITTED（3-6 は全て SUBMITTED）"""
        client = _make_client()
        client.request.return_value = _make_raw_response(state="6")
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.status == OrderStatus.SUBMITTED

    @pytest.mark.asyncio
    async def test_status_rejected(self):
        """sOrderStatusCode='2'（受付エラー）→ status=REJECTED"""
        client = _make_client()
        client.request.return_value = _make_raw_response(state="2")
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.status == OrderStatus.REJECTED

    @pytest.mark.asyncio
    async def test_unknown_state_code_maps_to_unknown(self):
        """未知の sOrderStatusCode は UNKNOWN にフォールバックする"""
        client = _make_client()
        client.request.return_value = _make_raw_response(state="99")
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.status == OrderStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_filled_price_weighted_average(self):
        """filled_price は sYakuzyouKingaku / sYakuzyouSuryou の加重平均"""
        client = _make_client()
        client.request.return_value = _make_raw_response(
            state="10",
            order_qty="100",
            filled_qty="100",
            filled_amount="300500",   # 100株 * 3005円
        )
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.filled_price == pytest.approx(3005.0)

    @pytest.mark.asyncio
    async def test_filled_price_none_when_no_execution(self):
        """約定数量が 0 の場合 filled_price は None"""
        client = _make_client()
        client.request.return_value = _make_raw_response(
            state="1", filled_qty="0", filled_amount="0"
        )
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.filled_price is None

    @pytest.mark.asyncio
    async def test_remaining_qty_calculated(self):
        """remaining_qty = order_qty - filled_qty - cancel_qty"""
        client = _make_client()
        client.request.return_value = _make_raw_response(
            state="9",
            order_qty="100",
            filled_qty="30",
            cancel_qty="10",
        )
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.remaining_qty == 60   # 100 - 30 - 10

    @pytest.mark.asyncio
    async def test_execution_key_from_yakuzyou_list(self):
        """aYakuzyouSikkouList がある場合、最新約定から execution_key が生成される"""
        client = _make_client()
        client.request.return_value = _make_raw_response(
            state="10",
            order_qty="100",
            filled_qty="100",
            filled_amount="300000",
            yakuzyou_list=[
                {"sYakuzyouDate": "153045", "sYakuzyouSuryou": "100", "sYakuzyouPrice": "3000"},
            ],
        )
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        # execution_key = "{eigyou_day}_{order_number}_{yakuzyou_date}_{qty}"
        assert result.broker_execution_id == "20260316_00123_153045_100"

    @pytest.mark.asyncio
    async def test_execution_key_none_when_no_yakuzyou_list(self):
        """aYakuzyouSikkouList が存在しない場合 broker_execution_id は None"""
        client = _make_client()
        client.request.return_value = _make_raw_response(state="1")
        adapter = _make_adapter(client=client)

        result = await adapter.get_order_status(_BROKER_ORDER_ID)

        assert result.broker_execution_id is None

    @pytest.mark.asyncio
    async def test_ensure_session_is_called(self):
        """get_order_status は必ず ensure_session を呼ぶ"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        await adapter.get_order_status(_BROKER_ORDER_ID)

        session.ensure_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_auto_retry(self):
        """正常時は client.request を 1 回しか呼ばない（自動再送なし）"""
        client = _make_client()
        client.request.return_value = _make_raw_response()
        adapter = _make_adapter(client=client)

        await adapter.get_order_status(_BROKER_ORDER_ID)

        assert client.request.await_count == 1


# ─── エラー系 ───────────────────────────────────────────────────────────────

class TestGetOrderStatusErrors:
    """get_order_status エラー系"""

    @pytest.mark.asyncio
    async def test_timeout_raises_broker_temporary_error(self):
        """タイムアウト時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Request timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

    @pytest.mark.asyncio
    async def test_network_error_raises_broker_temporary_error(self):
        """ネットワークエラー時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Network error")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

    @pytest.mark.asyncio
    async def test_auth_error_raises_and_invalidates_session(self):
        """認証エラー時に BrokerAuthError が送出され、セッションが無効化される"""
        client = _make_client()
        client.request.side_effect = BrokerAuthError("sResultCode=E001")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

        session.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_maintenance_raises_broker_maintenance_error(self):
        """メンテナンス中に BrokerMaintenanceError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerMaintenanceError("sResultCode=E999")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerMaintenanceError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

    @pytest.mark.asyncio
    async def test_api_error_propagates(self):
        """その他 API エラーが BrokerAPIError として伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerAPIError("sResultCode=X001")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerAPIError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_timeout(self):
        """タイムアウト時に自動再送しない（client.request は 1 回のみ）"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Request timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_network_error(self):
        """ネットワークエラー時も自動再送しない"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Network error")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_auth_error_does_not_retry(self):
        """認証エラー時も自動再送しない"""
        client = _make_client()
        client.request.side_effect = BrokerAuthError("sResultCode=E001")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_session_not_invalidated_on_non_auth_error(self):
        """認証エラー以外のエラーではセッションを無効化しない"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

        session.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_kinsyouhou_midoku_raises_broker_api_error(self):
        """is_usable=False（sKinsyouhouMidokuFlg=1）時に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(is_usable=False)
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="sKinsyouhouMidokuFlg"):
            await adapter.get_order_status(_BROKER_ORDER_ID)

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_url_request_raises_broker_api_error(self):
        """照会用仮想 URL が空の場合に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(url_request="")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="仮想 URL"):
            await adapter.get_order_status(_BROKER_ORDER_ID)

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_broker_order_id_raises_value_error(self):
        """broker_order_id にセパレータ '_' がない場合 ValueError を送出する"""
        adapter = _make_adapter()

        # decode_broker_order_id は split("_", 1) で分割する。
        # アンダースコアを含まない文字列は len(parts) != 2 になり ValueError になる。
        with pytest.raises(ValueError):
            await adapter.get_order_status("nodateseparator")

    @pytest.mark.asyncio
    async def test_login_failure_propagates_auth_error(self):
        """ensure_session() がログイン失敗で BrokerAuthError を送出する場合に伝播する"""
        client = _make_client()
        session = _make_session()
        session.ensure_session.side_effect = BrokerAuthError("login failed")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_order_status(_BROKER_ORDER_ID)

        client.request.assert_not_awaited()


# cancel_order は Phase 10-C で実装済み。
# 詳細テストは test_tachibana_adapter_cancel_order.py を参照。
