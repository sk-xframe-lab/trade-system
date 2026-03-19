"""
TachibanaBrokerAdapter.get_market_price テスト

テスト方針:
  - TachibanaClient.request と TachibanaSessionManager を mock して隔離
  - 正常価格 / 価格取得不能な正常系（None）/ エラー系を検証
  - 価格取得不能な正常系 = 取引時間外・pDPP="0"・空文字・配列空・フィールド欠損 → None
  - ExitWatcher への接続を意識した設計（None で TP/SL スキップ）を確認
  - 他メソッド（get_balance 等）と同一エラーパターンを検証
  - mapper 単体テスト（map_market_price / map_market_price_from_entry）を含む

API 仕様:
  CLMMfdsGetMarketPrice / url_price (sUrlPrice)
  リクエスト: sTargetIssueCode（銘柄コード）+ sTargetColumn="pDPP"（暫定）
  レスポンス: aCLMMfdsMarketPrice 配列の先頭要素 pDPP（現在値 暫定）
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from trade_app.brokers.base import (
    BrokerAPIError,
    BrokerAuthError,
    BrokerMaintenanceError,
    BrokerTemporaryError,
)
from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter
from trade_app.brokers.tachibana.client import TachibanaClient
from trade_app.brokers.tachibana.session import TachibanaSessionManager
from trade_app.brokers.tachibana import mapper


# ─── テスト用ファクトリ ──────────────────────────────────────────────────────

def _make_client() -> MagicMock:
    client = MagicMock(spec=TachibanaClient)
    client.request = AsyncMock()
    return client


def _make_session(
    is_usable: bool = True,
    url_price: str = "https://virtual.example.com/price",
) -> MagicMock:
    session = MagicMock(spec=TachibanaSessionManager)
    session.ensure_session = AsyncMock()
    session.is_usable = is_usable
    session.url_price = url_price
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
    adapter._settings = MagicMock()
    return adapter


def _make_price_response(pdpp: str = "2500") -> dict:
    """
    CLMMfdsGetMarketPrice の正常レスポンス。
    pDPP が空文字の場合は配列要素に空文字を入れて "取得不能" を模倣する。
    """
    return {
        "sResultCode":          "0",
        "sResultText":          "正常",
        "aCLMMfdsMarketPrice":  [{"pDPP": pdpp}],
    }


def _make_empty_array_response() -> dict:
    """aCLMMfdsMarketPrice が空配列のレスポンス（価格なし）"""
    return {
        "sResultCode":          "0",
        "sResultText":          "正常",
        "aCLMMfdsMarketPrice":  [],
    }


# ─── 正常系: 価格返却 ─────────────────────────────────────────────────────────

class TestGetMarketPriceSuccess:
    """get_market_price 正常系（価格あり）"""

    @pytest.mark.asyncio
    async def test_returns_float(self):
        """正常価格取得時は float を返す"""
        client = _make_client()
        client.request.return_value = _make_price_response(pdpp="2500")
        adapter = _make_adapter(client=client)

        result = await adapter.get_market_price("7203")

        assert isinstance(result, float)

    @pytest.mark.asyncio
    async def test_returns_pdpp_value(self):
        """aCLMMfdsMarketPrice[0]["pDPP"] の値が float で返る"""
        client = _make_client()
        client.request.return_value = _make_price_response(pdpp="2750")
        adapter = _make_adapter(client=client)

        result = await adapter.get_market_price("7203")

        assert result == pytest.approx(2750.0)

    @pytest.mark.asyncio
    async def test_comma_separated_price(self):
        """カンマ区切り価格（"2,750"）も正しく変換される"""
        client = _make_client()
        client.request.return_value = _make_price_response(pdpp="2,750")
        adapter = _make_adapter(client=client)

        result = await adapter.get_market_price("7203")

        assert result == pytest.approx(2750.0)

    @pytest.mark.asyncio
    async def test_uses_url_price(self):
        """価格照会は url_price (sUrlPrice) を使う"""
        client = _make_client()
        client.request.return_value = _make_price_response()
        session = _make_session(url_price="https://virtual.example.com/price/F2")
        adapter = _make_adapter(client=client, session=session)

        await adapter.get_market_price("7203")

        url_used = client.request.call_args[0][0]
        assert url_used == "https://virtual.example.com/price/F2"

    @pytest.mark.asyncio
    async def test_sends_ticker_in_payload(self):
        """sTargetIssueCode に ticker をセットして送信する"""
        client = _make_client()
        client.request.return_value = _make_price_response()
        adapter = _make_adapter(client=client)

        await adapter.get_market_price("9984")

        payload = client.request.call_args[0][1]
        assert payload.get("sTargetIssueCode") == "9984"

    @pytest.mark.asyncio
    async def test_ensure_session_called(self):
        """get_market_price は必ず ensure_session を呼ぶ"""
        client = _make_client()
        client.request.return_value = _make_price_response()
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        await adapter.get_market_price("7203")

        session.ensure_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_auto_retry(self):
        """正常時は client.request を 1 回しか呼ばない（自動再送なし）"""
        client = _make_client()
        client.request.return_value = _make_price_response()
        adapter = _make_adapter(client=client)

        await adapter.get_market_price("7203")

        assert client.request.await_count == 1


# ─── 正常系: 価格取得不能（None 返却）────────────────────────────────────────

class TestGetMarketPriceNone:
    """
    価格取得不能な正常系。
    取引時間外・データなし・pDPP="0"・空文字・配列空 → None を返す（例外ではない）。
    ExitWatcher はこの None を受けて TP/SL をスキップする。
    """

    @pytest.mark.asyncio
    async def test_pdpp_zero_returns_none(self):
        """pDPP が "0" の場合は None を返す（取引時間外想定）"""
        client = _make_client()
        client.request.return_value = _make_price_response(pdpp="0")
        adapter = _make_adapter(client=client)

        result = await adapter.get_market_price("7203")

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_pdpp_returns_none(self):
        """pDPP が空文字の場合は None を返す"""
        client = _make_client()
        client.request.return_value = _make_price_response(pdpp="")
        adapter = _make_adapter(client=client)

        result = await adapter.get_market_price("7203")

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_array_returns_none(self):
        """aCLMMfdsMarketPrice が空配列の場合は None を返す"""
        client = _make_client()
        client.request.return_value = _make_empty_array_response()
        adapter = _make_adapter(client=client)

        result = await adapter.get_market_price("7203")

        assert result is None

    @pytest.mark.asyncio
    async def test_missing_array_key_returns_none(self):
        """aCLMMfdsMarketPrice キーが存在しない場合は None を返す"""
        client = _make_client()
        client.request.return_value = {"sResultCode": "0", "sResultText": "正常"}
        adapter = _make_adapter(client=client)

        result = await adapter.get_market_price("7203")

        assert result is None

    @pytest.mark.asyncio
    async def test_negative_price_returns_none(self):
        """負の価格（異常値）の場合は None を返す"""
        client = _make_client()
        client.request.return_value = _make_price_response(pdpp="-100")
        adapter = _make_adapter(client=client)

        result = await adapter.get_market_price("7203")

        assert result is None


# ─── エラー系 ─────────────────────────────────────────────────────────────────

class TestGetMarketPriceErrors:
    """get_market_price エラー系"""

    @pytest.mark.asyncio
    async def test_timeout_raises_broker_temporary_error(self):
        """タイムアウト時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_market_price("7203")

    @pytest.mark.asyncio
    async def test_auth_error_invalidates_session(self):
        """認証エラー時に BrokerAuthError が送出され、セッションが無効化される"""
        client = _make_client()
        client.request.side_effect = BrokerAuthError("E001")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_market_price("7203")

        session.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_maintenance_raises_broker_maintenance_error(self):
        """メンテナンス中に BrokerMaintenanceError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerMaintenanceError("990002")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerMaintenanceError):
            await adapter.get_market_price("7203")

    @pytest.mark.asyncio
    async def test_api_error_propagates(self):
        """API エラーが BrokerAPIError として伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerAPIError("X001")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerAPIError):
            await adapter.get_market_price("7203")

    @pytest.mark.asyncio
    async def test_kinsyouhou_midoku_raises_broker_api_error(self):
        """is_usable=False 時に BrokerAPIError を送出する（request は呼ばない）"""
        client = _make_client()
        session = _make_session(is_usable=False)
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="sKinsyouhouMidokuFlg"):
            await adapter.get_market_price("7203")

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_url_price_raises_broker_api_error(self):
        """価格照会用仮想 URL (url_price) が空の場合に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(url_price="")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="仮想 URL"):
            await adapter.get_market_price("7203")

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_login_failure_propagates_auth_error(self):
        """ensure_session がログイン失敗で BrokerAuthError を送出する場合に伝播する"""
        client = _make_client()
        session = _make_session()
        session.ensure_session.side_effect = BrokerAuthError("login failed")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_market_price("7203")

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_auto_retry_on_timeout(self):
        """タイムアウト時に自動再送しない（request は 1 回のみ）"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_market_price("7203")

        assert client.request.await_count == 1

    @pytest.mark.asyncio
    async def test_no_session_invalidate_on_non_auth_error(self):
        """認証エラー以外の場合はセッションを無効化しない"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_market_price("7203")

        session.invalidate.assert_not_called()


# ─── mapper 単体テスト: map_market_price / map_market_price_from_entry ────────

class TestMapMarketPrice:
    """mapper.map_market_price の単体テスト（aCLMMfdsMarketPrice + pDPP 形式）"""

    def test_returns_pdpp_as_float(self):
        """aCLMMfdsMarketPrice[0]["pDPP"] が正の値なら float で返す"""
        raw = {"aCLMMfdsMarketPrice": [{"pDPP": "2500"}]}
        assert mapper.map_market_price(raw) == pytest.approx(2500.0)

    def test_returns_none_when_pdpp_zero(self):
        """pDPP が "0" の場合は None を返す"""
        raw = {"aCLMMfdsMarketPrice": [{"pDPP": "0"}]}
        assert mapper.map_market_price(raw) is None

    def test_returns_none_when_pdpp_empty_string(self):
        """pDPP が空文字の場合は None を返す"""
        raw = {"aCLMMfdsMarketPrice": [{"pDPP": ""}]}
        assert mapper.map_market_price(raw) is None

    def test_returns_none_when_array_empty(self):
        """aCLMMfdsMarketPrice が空配列の場合は None を返す"""
        raw = {"aCLMMfdsMarketPrice": []}
        assert mapper.map_market_price(raw) is None

    def test_returns_none_when_array_missing(self):
        """aCLMMfdsMarketPrice キーが存在しない場合は None を返す"""
        raw = {"sResultCode": "0"}
        assert mapper.map_market_price(raw) is None

    def test_returns_none_when_pdpp_negative(self):
        """負の pDPP は None を返す（異常値として安全側に倒す）"""
        raw = {"aCLMMfdsMarketPrice": [{"pDPP": "-100"}]}
        assert mapper.map_market_price(raw) is None

    def test_comma_separated_pdpp(self):
        """カンマ区切り（"2,500"）も正しく変換される"""
        raw = {"aCLMMfdsMarketPrice": [{"pDPP": "2,500"}]}
        assert mapper.map_market_price(raw) == pytest.approx(2500.0)

    def test_uses_first_element_only(self):
        """複数要素がある場合は先頭要素のみ使用する"""
        raw = {"aCLMMfdsMarketPrice": [{"pDPP": "2500"}, {"pDPP": "9999"}]}
        assert mapper.map_market_price(raw) == pytest.approx(2500.0)


class TestMapMarketPriceFromEntry:
    """mapper.map_market_price_from_entry の単体テスト"""

    def test_positive_pdpp_returns_float(self):
        """pDPP が正の値なら float で返す"""
        entry = {"pDPP": "3000"}
        assert mapper.map_market_price_from_entry(entry) == pytest.approx(3000.0)

    def test_zero_pdpp_returns_none(self):
        """pDPP が "0" の場合は None を返す"""
        entry = {"pDPP": "0"}
        assert mapper.map_market_price_from_entry(entry) is None

    def test_missing_pdpp_returns_none(self):
        """pDPP キーが存在しない場合は None を返す"""
        entry = {"other_field": "123"}
        assert mapper.map_market_price_from_entry(entry) is None

    def test_negative_pdpp_returns_none(self):
        """負の pDPP は None を返す"""
        entry = {"pDPP": "-50"}
        assert mapper.map_market_price_from_entry(entry) is None
