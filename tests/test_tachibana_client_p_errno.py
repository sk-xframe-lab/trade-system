"""
TachibanaClient._check_p_errno テスト — Phase AN

目的:
  p_errno=2（セッション切断）が BrokerAuthError を送出し、
  adapter.get_market_data() が _handle_auth_error() → session.invalidate() を呼ぶことを検証する。

検証項目:
  1. p_errno=2 → BrokerAuthError（Phase AN の核心）
  2. p_errno=10001 → BrokerAuthError（既存コード: 変更なし）
  3. p_errno=0 → 例外なし（正常）
  4. p_errno 存在しない → 例外なし（正常）
  5. p_errno=99（非認証エラー） → BrokerAPIError
  6. p_errno=2 受信時に adapter が session.invalidate() を呼ぶ（統合確認）
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from trade_app.brokers.base import BrokerAPIError, BrokerAuthError
from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter
from trade_app.brokers.tachibana.client import TachibanaClient
from trade_app.brokers.tachibana.session import TachibanaSessionManager


# ─── ユーティリティ ──────────────────────────────────────────────────────────

def _make_client_instance() -> TachibanaClient:
    """テスト用 TachibanaClient（_http_client は mock で代替）"""
    import httpx
    c = TachibanaClient.__new__(TachibanaClient)
    c._http_client = MagicMock(spec=httpx.AsyncClient)
    return c


def _make_adapter() -> tuple[TachibanaBrokerAdapter, MagicMock, MagicMock]:
    """adapter, mock_client, mock_session を返す"""
    mock_client = MagicMock(spec=TachibanaClient)
    mock_client.request = AsyncMock()
    mock_session = MagicMock(spec=TachibanaSessionManager)
    mock_session.ensure_session = AsyncMock()
    mock_session.url_price = "https://demo.example.com/price/"
    mock_session.invalidate = MagicMock()
    adapter = TachibanaBrokerAdapter.__new__(TachibanaBrokerAdapter)
    adapter._client = mock_client
    adapter._session = mock_session
    return adapter, mock_client, mock_session


# ─── _check_p_errno 単体テスト ───────────────────────────────────────────────

class TestCheckPErrno:
    """TachibanaClient._check_p_errno() の単体テスト"""

    def setup_method(self):
        self.client = _make_client_instance()

    def test_p_errno_2_raises_broker_auth_error(self):
        """p_errno=2（セッション切断）→ BrokerAuthError — Phase AN の核心"""
        data = {"p_errno": "2", "p_err_msg": "セッションが切断しました。"}
        with pytest.raises(BrokerAuthError) as exc_info:
            self.client._check_p_errno(data, url="https://demo.example.com/price/")
        assert "p_errno=2" in str(exc_info.value)

    def test_p_errno_10001_raises_broker_auth_error(self):
        """p_errno=10001（既存認証エラー）→ BrokerAuthError（変更なし）"""
        data = {"p_errno": "10001"}
        with pytest.raises(BrokerAuthError) as exc_info:
            self.client._check_p_errno(data, url="https://demo.example.com/")
        assert "p_errno=10001" in str(exc_info.value)

    def test_p_errno_0_no_exception(self):
        """p_errno=0（正常）→ 例外なし"""
        data = {"p_errno": "0", "sCLMID": "CLMMfdsGetMarketPrice"}
        self.client._check_p_errno(data)  # 例外が出なければ OK

    def test_p_errno_absent_no_exception(self):
        """p_errno キーなし → 例外なし"""
        data = {"sCLMID": "CLMMfdsGetMarketPrice"}
        self.client._check_p_errno(data)  # 例外が出なければ OK

    def test_p_errno_non_auth_raises_broker_api_error(self):
        """p_errno=99（非認証系エラー）→ BrokerAPIError（認証エラーではない）"""
        data = {"p_errno": "99"}
        with pytest.raises(BrokerAPIError) as exc_info:
            self.client._check_p_errno(data)
        assert "p_errno=99" in str(exc_info.value)

    def test_p_errno_2_as_integer_raises_broker_auth_error(self):
        """p_errno=2 が整数で渡された場合も BrokerAuthError"""
        data = {"p_errno": 2}
        with pytest.raises(BrokerAuthError):
            self.client._check_p_errno(data)

    def test_p_errno_2_message_contains_url(self):
        """BrokerAuthError のメッセージに url が含まれる"""
        data = {"p_errno": "2"}
        url = "https://demo.example.com/price/"
        with pytest.raises(BrokerAuthError) as exc_info:
            self.client._check_p_errno(data, url=url)
        assert url in str(exc_info.value)


# ─── adapter.get_market_data() 統合確認 ─────────────────────────────────────

class TestAdapterInvalidatesOnPErrno2:
    """p_errno=2 受信時に adapter が session.invalidate() を呼ぶことを確認"""

    @pytest.mark.asyncio
    async def test_p_errno_2_triggers_session_invalidate(self):
        """
        client.request が BrokerAuthError を送出（p_errno=2 相当）したとき、
        adapter.get_market_data() が _handle_auth_error() → session.invalidate() を呼ぶ。
        """
        adapter, mock_client, mock_session = _make_adapter()
        mock_session.url_price = "https://demo.example.com/price/"

        # client.request が BrokerAuthError を送出（p_errno=2 送出後の動作と等価）
        mock_client.request.side_effect = BrokerAuthError("p_errno=2 url=https://demo.example.com/price/")

        with pytest.raises(BrokerAuthError):
            await adapter.get_market_data("7203")

        mock_session.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_broker_api_error_does_not_invalidate(self):
        """
        BrokerAPIError（非認証系）では session.invalidate() を呼ばない。
        p_errno=2 を BrokerAPIError のままにすると再ログインされないことの対比。
        """
        adapter, mock_client, mock_session = _make_adapter()
        mock_client.request.side_effect = BrokerAPIError("p_errno=99 url=...")

        with pytest.raises(BrokerAPIError):
            await adapter.get_market_data("7203")

        mock_session.invalidate.assert_not_called()
