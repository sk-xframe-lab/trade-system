"""
TachibanaBrokerAdapter.get_balance テスト

テスト方針:
  - TachibanaClient.request と TachibanaSessionManager を mock して隔離
  - 2 API 呼び出し設計（CLMZanKaiKanougaku + CLMZanShinkiKanoIjiritu）を確認
  - 信用余力照会失敗時のデグレード設計（margin_available=0）を確認
  - 認証エラー（現物・信用共に）はセッション無効化 + 例外伝播
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from trade_app.brokers.base import (
    BalanceInfo,
    BrokerAPIError,
    BrokerAuthError,
    BrokerMaintenanceError,
    BrokerTemporaryError,
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
    url_request: str = "https://virtual.example.com/request",
) -> MagicMock:
    session = MagicMock(spec=TachibanaSessionManager)
    session.ensure_session = AsyncMock()
    session.is_usable = is_usable
    session.url_request = url_request
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
    adapter._settings = settings
    return adapter


def _make_cash_response(genkabu_kaituke: str = "1000000") -> dict:
    """CLMZanKaiKanougaku の正常レスポンス"""
    return {
        "sResultCode":              "0",
        "sResultText":              "正常",
        "sSummaryGenkabuKaituke":   genkabu_kaituke,
    }


def _make_margin_response(sinyou_sinkidate: str = "500000") -> dict:
    """CLMZanShinkiKanoIjiritu の正常レスポンス"""
    return {
        "sResultCode":              "0",
        "sResultText":              "正常",
        "sSummarySinyouSinkidate":  sinyou_sinkidate,
    }


# ─── 正常系: BalanceInfo フィールドマッピング ─────────────────────────────────

class TestGetBalanceSuccess:
    """get_balance 正常系"""

    @pytest.mark.asyncio
    async def test_returns_balance_info(self):
        """戻り値が BalanceInfo 型である"""
        client = _make_client()
        client.request.side_effect = [_make_cash_response(), _make_margin_response()]
        adapter = _make_adapter(client=client)

        result = await adapter.get_balance()

        assert isinstance(result, BalanceInfo)

    @pytest.mark.asyncio
    async def test_cash_balance_mapped(self):
        """cash_balance が sSummaryGenkabuKaituke から正しくマップされる"""
        client = _make_client()
        client.request.side_effect = [
            _make_cash_response(genkabu_kaituke="1500000"),
            _make_margin_response(),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_balance()

        assert result.cash_balance == pytest.approx(1500000.0)

    @pytest.mark.asyncio
    async def test_margin_available_mapped(self):
        """margin_available が sSummarySinyouSinkidate から正しくマップされる"""
        client = _make_client()
        client.request.side_effect = [
            _make_cash_response(),
            _make_margin_response(sinyou_sinkidate="750000"),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_balance()

        assert result.margin_available == pytest.approx(750000.0)

    @pytest.mark.asyncio
    async def test_total_equity_is_zero_placeholder(self):
        """total_equity は仕様書未確認のため暫定 0.0"""
        client = _make_client()
        client.request.side_effect = [_make_cash_response(), _make_margin_response()]
        adapter = _make_adapter(client=client)

        result = await adapter.get_balance()

        assert result.total_equity == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_all_fields_together(self):
        """cash / margin が同時に正しくマップされる"""
        client = _make_client()
        client.request.side_effect = [
            _make_cash_response(genkabu_kaituke="1000000"),
            _make_margin_response(sinyou_sinkidate="500000"),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_balance()

        assert result.cash_balance     == pytest.approx(1000000.0)
        assert result.margin_available == pytest.approx(500000.0)

    @pytest.mark.asyncio
    async def test_comma_separated_values(self):
        """カンマ区切り数値（"1,000,000"）も正しく変換される"""
        client = _make_client()
        client.request.side_effect = [
            _make_cash_response(genkabu_kaituke="1,000,000"),
            _make_margin_response(sinyou_sinkidate="500,000"),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_balance()

        assert result.cash_balance     == pytest.approx(1000000.0)
        assert result.margin_available == pytest.approx(500000.0)

    @pytest.mark.asyncio
    async def test_missing_cash_field_defaults_to_zero(self):
        """sSummaryGenkabuKaituke が欠損している場合は 0.0 にフォールバックする"""
        client = _make_client()
        client.request.side_effect = [
            {"sResultCode": "0"},   # フィールドなし
            _make_margin_response(),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_balance()

        assert result.cash_balance == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_uses_url_request(self):
        """両 API とも url_request（照会系仮想 URL）を使う"""
        client = _make_client()
        client.request.side_effect = [_make_cash_response(), _make_margin_response()]
        session = _make_session(url_request="https://virtual.example.com/req/F0")
        adapter = _make_adapter(client=client, session=session)

        await adapter.get_balance()

        for call_args in client.request.call_args_list:
            assert call_args[0][0] == "https://virtual.example.com/req/F0"

    @pytest.mark.asyncio
    async def test_two_api_calls_made(self):
        """client.request が現物・信用で計 2 回呼ばれる"""
        client = _make_client()
        client.request.side_effect = [_make_cash_response(), _make_margin_response()]
        adapter = _make_adapter(client=client)

        await adapter.get_balance()

        assert client.request.await_count == 2

    @pytest.mark.asyncio
    async def test_cash_api_called_first(self):
        """現物余力照会（CLMZanKaiKanougaku）が信用より先に呼ばれる"""
        client = _make_client()
        client.request.side_effect = [_make_cash_response(), _make_margin_response()]
        adapter = _make_adapter(client=client)

        await adapter.get_balance()

        first_clmid  = client.request.call_args_list[0][0][1]["sCLMID"]
        second_clmid = client.request.call_args_list[1][0][1]["sCLMID"]
        assert first_clmid  == "CLMZanKaiKanougaku"
        assert second_clmid == "CLMZanShinkiKanoIjiritu"

    @pytest.mark.asyncio
    async def test_ensure_session_is_called(self):
        """get_balance は必ず ensure_session を呼ぶ"""
        client = _make_client()
        client.request.side_effect = [_make_cash_response(), _make_margin_response()]
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        await adapter.get_balance()

        session.ensure_session.assert_awaited_once()


# ─── デグレード設計: 信用余力照会失敗時 ──────────────────────────────────────

class TestGetBalanceDegrade:
    """信用余力照会失敗時のデグレード設計"""

    @pytest.mark.asyncio
    async def test_margin_api_error_degrades_to_zero(self):
        """信用余力照会が BrokerAPIError で失敗した場合 margin_available=0 でデグレード"""
        client = _make_client()
        client.request.side_effect = [
            _make_cash_response(genkabu_kaituke="1200000"),
            BrokerAPIError("信用口座なし"),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_balance()

        assert isinstance(result, BalanceInfo)
        assert result.cash_balance     == pytest.approx(1200000.0)
        assert result.margin_available == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_margin_temporary_error_degrades_to_zero(self):
        """信用余力照会が BrokerTemporaryError で失敗した場合もデグレード"""
        client = _make_client()
        client.request.side_effect = [
            _make_cash_response(),
            BrokerTemporaryError("timeout on margin"),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_balance()

        assert isinstance(result, BalanceInfo)
        assert result.margin_available == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_margin_auth_error_propagates_and_invalidates(self):
        """信用余力照会が BrokerAuthError の場合はデグレードせず伝播 + セッション無効化"""
        client = _make_client()
        client.request.side_effect = [
            _make_cash_response(),
            BrokerAuthError("session expired"),
        ]
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_balance()

        session.invalidate.assert_called_once()


# ─── エラー系: 現物余力照会失敗 ──────────────────────────────────────────────

class TestGetBalanceErrors:
    """get_balance エラー系（現物余力照会が失敗した場合は全て伝播）"""

    @pytest.mark.asyncio
    async def test_cash_timeout_raises_broker_temporary_error(self):
        """現物照会タイムアウト時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Request timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_balance()

    @pytest.mark.asyncio
    async def test_cash_auth_error_raises_and_invalidates_session(self):
        """現物照会で認証エラー時に BrokerAuthError が送出され、セッションが無効化される"""
        client = _make_client()
        client.request.side_effect = BrokerAuthError("sResultCode=900002")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_balance()

        session.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_cash_maintenance_raises_broker_maintenance_error(self):
        """現物照会メンテナンス中に BrokerMaintenanceError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerMaintenanceError("sResultCode=990002")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerMaintenanceError):
            await adapter.get_balance()

    @pytest.mark.asyncio
    async def test_cash_api_error_propagates(self):
        """現物照会 API エラーが BrokerAPIError として伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerAPIError("sResultCode=X001")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerAPIError):
            await adapter.get_balance()

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_cash_timeout(self):
        """タイムアウト時に自動再送しない（request は 1 回のみ）"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("Request timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_balance()

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_session_not_invalidated_on_non_auth_error(self):
        """現物照会で認証エラー以外のエラーではセッションを無効化しない"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_balance()

        session.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_kinsyouhou_midoku_raises_broker_api_error(self):
        """is_usable=False（sKinsyouhouMidokuFlg=1）時に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(is_usable=False)
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="sKinsyouhouMidokuFlg"):
            await adapter.get_balance()

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_url_request_raises_broker_api_error(self):
        """照会用仮想 URL が空の場合に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(url_request="")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="仮想 URL"):
            await adapter.get_balance()

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_login_failure_propagates_auth_error(self):
        """ensure_session() がログイン失敗で BrokerAuthError を送出する場合に伝播する"""
        client = _make_client()
        session = _make_session()
        session.ensure_session.side_effect = BrokerAuthError("login failed")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_balance()

        client.request.assert_not_awaited()
