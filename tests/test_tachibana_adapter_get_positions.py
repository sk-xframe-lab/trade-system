"""
TachibanaBrokerAdapter.get_positions テスト

テスト方針:
  - TachibanaClient.request と TachibanaSessionManager を mock して隔離
  - 2 API 呼び出し設計（現物 + 信用）を確認
  - 部分返却なし設計（いずれか失敗 → 例外伝播）を確認
  - place_order / get_balance と同一エラーパターンを検証
  - mapper 単体テスト（map_spot_positions / parse_*_response）を含む

仕様未確定 NOTE:
  フィールド名・API名・リストキーはすべて仕様書未確認の推定値。
  本番投入前に仕様書で確認すること（mapper.py / adapter.py の TODO を参照）。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, call

from trade_app.brokers.base import (
    BrokerAPIError,
    BrokerAuthError,
    BrokerMaintenanceError,
    BrokerPosition,
    BrokerTemporaryError,
)
from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter
from trade_app.brokers.tachibana.client import TachibanaClient
from trade_app.brokers.tachibana.session import TachibanaSessionManager
from trade_app.brokers.tachibana import mapper
from trade_app.models.enums import Side


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
    adapter._settings = MagicMock()
    return adapter


# ─── レスポンスビルダー ───────────────────────────────────────────────────────

def _make_spot_response(items: list | None = None) -> dict:
    """現物保有照会 API の正常レスポンス（リストキー: aGenbutuKabuList）"""
    return {
        "sResultCode":     "0",
        "sResultText":     "正常",
        "aGenbutuKabuList": items if items is not None else [],
    }


def _make_spot_item(
    ticker: str    = "7203",
    qty:    str    = "100",
    price:  str    = "2500",
) -> dict:
    """現物保有1行（仕様書確認済みフィールド名）"""
    return {
        "sUriOrderIssueCode":       ticker,
        "sUriOrderZanKabuSuryou":   qty,    # 残高数量
        "sUriOrderGaisanBokaTanka": price,  # 概算簿価単価
    }


def _make_margin_response(items: list | None = None) -> dict:
    """信用建玉照会 API の正常レスポンス（リストキー: aShinyouTategyokuList）"""
    return {
        "sResultCode":          "0",
        "sResultText":          "正常",
        "aShinyouTategyokuList": items if items is not None else [],
    }


def _make_margin_item(
    ticker:       str = "7203",
    qty:          str = "100",
    price:        str = "2500",
    baibai_kubun: str = "3",      # "3"=買, "1"=売
    eigyou_day:   str = "20260316",
    order_number: str = "00123",
) -> dict:
    """信用建玉1行（仕様書確認済みフィールド名）"""
    return {
        "sOrderIssueCode":      ticker,
        "sBaibaiKubun":         baibai_kubun,
        "sOrderTategyokuSuryou": qty,
        "sOrderTategyokuTanka": price,
        "sEigyouDay":           eigyou_day,
        "sOrderNumber":         order_number,
    }


# ─── 正常系 ──────────────────────────────────────────────────────────────────

class TestGetPositionsSuccess:
    """get_positions 正常系"""

    @pytest.mark.asyncio
    async def test_returns_list(self):
        """戻り値がリスト型である"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response(),
            _make_margin_response(),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_positions()

        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_spot_and_margin_combined(self):
        """現物 + 信用の両方が結合されて返る"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response([_make_spot_item(ticker="7203")]),
            _make_margin_response([_make_margin_item(ticker="9984")]),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_positions()

        assert len(result) == 2
        tickers = {p.ticker for p in result}
        assert tickers == {"7203", "9984"}

    @pytest.mark.asyncio
    async def test_spot_only_margin_empty(self):
        """信用ゼロでも現物だけ返る"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response([_make_spot_item(ticker="7203")]),
            _make_margin_response([]),   # 信用ゼロ
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_positions()

        assert len(result) == 1
        assert result[0].ticker == "7203"

    @pytest.mark.asyncio
    async def test_margin_only_spot_empty(self):
        """現物ゼロでも信用だけ返る"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response([]),     # 現物ゼロ
            _make_margin_response([_make_margin_item(ticker="9984")]),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_positions()

        assert len(result) == 1
        assert result[0].ticker == "9984"

    @pytest.mark.asyncio
    async def test_both_empty_returns_empty_list(self):
        """現物・信用ともにゼロ → 空リストを返す（エラーではない）"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response([]),
            _make_margin_response([]),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_positions()

        assert result == []

    @pytest.mark.asyncio
    async def test_uses_url_request(self):
        """照会は url_request (sUrlF0) を使う"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response(),
            _make_margin_response(),
        ]
        session = _make_session(url_request="https://virtual.example.com/F0")
        adapter = _make_adapter(client=client, session=session)

        await adapter.get_positions()

        for call_args in client.request.call_args_list:
            assert call_args[0][0] == "https://virtual.example.com/F0"

    @pytest.mark.asyncio
    async def test_ensure_session_called(self):
        """get_positions は必ず ensure_session を呼ぶ"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response(),
            _make_margin_response(),
        ]
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        await adapter.get_positions()

        session.ensure_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_two_api_calls_made(self):
        """client.request が現物・信用で計2回呼ばれる"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response(),
            _make_margin_response(),
        ]
        adapter = _make_adapter(client=client)

        await adapter.get_positions()

        assert client.request.await_count == 2

    @pytest.mark.asyncio
    async def test_spot_api_called_first(self):
        """現物照会が信用照会より先に呼ばれる"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response(),
            _make_margin_response(),
        ]
        adapter = _make_adapter(client=client)

        await adapter.get_positions()

        first_clmid  = client.request.call_args_list[0][0][1]["sCLMID"]
        second_clmid = client.request.call_args_list[1][0][1]["sCLMID"]
        assert first_clmid == "CLMGenbutuKabuList"
        assert second_clmid == "CLMShinyouTategyokuList"

    @pytest.mark.asyncio
    async def test_spot_broker_position_fields(self):
        """現物 BrokerPosition のフィールドが正しくマップされる"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response([_make_spot_item(ticker="7203", qty="200", price="2600")]),
            _make_margin_response([]),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_positions()

        assert len(result) == 1
        pos = result[0]
        assert pos.ticker        == "7203"
        assert pos.side          == Side.BUY
        assert pos.quantity      == 200
        assert pos.average_price == pytest.approx(2600.0)
        assert pos.broker_order_id.startswith("spot_")

    @pytest.mark.asyncio
    async def test_margin_broker_position_fields(self):
        """信用 BrokerPosition のフィールドが正しくマップされる"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response([]),
            _make_margin_response([_make_margin_item(
                ticker="9984", qty="100", price="50000", baibai_kubun="3",
                eigyou_day="20260316", order_number="00456",
            )]),
        ]
        adapter = _make_adapter(client=client)

        result = await adapter.get_positions()

        assert len(result) == 1
        pos = result[0]
        assert pos.ticker           == "9984"
        assert pos.side             == Side.BUY
        assert pos.quantity         == 100
        assert pos.average_price    == pytest.approx(50000.0)
        assert pos.broker_order_id  == "margin_9984_3"  # 合成キー: margin_{ticker}_{baibai_kubun}


# ─── エラー系 ─────────────────────────────────────────────────────────────────

class TestGetPositionsErrors:
    """get_positions エラー系"""

    @pytest.mark.asyncio
    async def test_spot_timeout_raises_broker_temporary_error(self):
        """現物照会タイムアウト時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_positions()

    @pytest.mark.asyncio
    async def test_margin_timeout_raises_broker_temporary_error(self):
        """信用照会タイムアウト時に BrokerTemporaryError が伝播する"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response(),
            BrokerTemporaryError("timeout on margin"),
        ]
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_positions()

    @pytest.mark.asyncio
    async def test_spot_auth_error_invalidates_session(self):
        """現物照会で認証エラー → BrokerAuthError + session.invalidate()"""
        client = _make_client()
        client.request.side_effect = BrokerAuthError("E001")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_positions()

        session.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_margin_auth_error_invalidates_session(self):
        """信用照会で認証エラー → BrokerAuthError + session.invalidate()"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response(),
            BrokerAuthError("session expired on margin"),
        ]
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_positions()

        session.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_kinsyouhou_midoku_raises_broker_api_error(self):
        """is_usable=False 時に BrokerAPIError を送出する（request は呼ばない）"""
        client = _make_client()
        session = _make_session(is_usable=False)
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="sKinsyouhouMidokuFlg"):
            await adapter.get_positions()

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_url_request_raises_broker_api_error(self):
        """照会用仮想 URL が空の場合に BrokerAPIError を送出する"""
        client = _make_client()
        session = _make_session(url_request="")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAPIError, match="仮想 URL"):
            await adapter.get_positions()

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_login_failure_propagates_auth_error(self):
        """ensure_session がログイン失敗で BrokerAuthError を送出する場合に伝播する"""
        client = _make_client()
        session = _make_session()
        session.ensure_session.side_effect = BrokerAuthError("login failed")
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerAuthError):
            await adapter.get_positions()

        client.request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_maintenance_raises_broker_maintenance_error(self):
        """メンテナンス中に BrokerMaintenanceError が伝播する"""
        client = _make_client()
        client.request.side_effect = BrokerMaintenanceError("E999")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerMaintenanceError):
            await adapter.get_positions()

    @pytest.mark.asyncio
    async def test_no_session_invalidate_on_non_auth_error(self):
        """認証エラー以外の場合はセッションを無効化しない"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        session = _make_session()
        adapter = _make_adapter(client=client, session=session)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_positions()

        session.invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_auto_retry(self):
        """タイムアウト時に自動再送しない（request は 1 回のみ）"""
        client = _make_client()
        client.request.side_effect = BrokerTemporaryError("timeout")
        adapter = _make_adapter(client=client)

        with pytest.raises(BrokerTemporaryError):
            await adapter.get_positions()

        assert client.request.await_count == 1


# ─── 部分返却なし設計 ─────────────────────────────────────────────────────────

class TestGetPositionsNoPartialReturn:
    """
    部分返却なし設計の確認。

    現物照会・信用照会のどちらかが失敗した場合、BrokerPosition は返さない。
    """

    @pytest.mark.asyncio
    async def test_spot_failure_no_positions_returned(self):
        """現物照会が失敗した場合、例外が発生して positions は返らない"""
        client = _make_client()
        client.request.side_effect = BrokerAPIError("spot API error")
        adapter = _make_adapter(client=client)

        result = None
        with pytest.raises(BrokerAPIError):
            result = await adapter.get_positions()

        assert result is None

    @pytest.mark.asyncio
    async def test_margin_failure_no_partial_return(self):
        """信用照会が失敗した場合、現物だけ返さず例外になる"""
        client = _make_client()
        client.request.side_effect = [
            _make_spot_response([_make_spot_item()]),  # 現物は成功
            BrokerAPIError("margin API error"),         # 信用は失敗
        ]
        adapter = _make_adapter(client=client)

        result = None
        with pytest.raises(BrokerAPIError):
            result = await adapter.get_positions()

        assert result is None


# ─── mapper 単体テスト: map_spot_positions ───────────────────────────────────

class TestMapSpotPositions:
    """mapper.map_spot_positions の単体テスト"""

    def test_maps_ticker(self):
        """sUriOrderIssueCode が ticker にマップされる"""
        items = [_make_spot_item(ticker="7203")]
        result = mapper.map_spot_positions(items)
        assert result[0].ticker == "7203"

    def test_maps_quantity(self):
        """sUriOrderZanKabuSuryou が quantity にマップされる"""
        items = [_make_spot_item(qty="300")]
        result = mapper.map_spot_positions(items)
        assert result[0].quantity == 300

    def test_maps_average_price(self):
        """sUriOrderGaisanBokaTanka が average_price にマップされる"""
        items = [_make_spot_item(price="2750")]
        result = mapper.map_spot_positions(items)
        assert result[0].average_price == pytest.approx(2750.0)

    def test_side_is_always_buy(self):
        """現物保有は常に Side.BUY である"""
        items = [_make_spot_item()]
        result = mapper.map_spot_positions(items)
        assert result[0].side == Side.BUY

    def test_broker_order_id_is_synthetic(self):
        """broker_order_id は 'spot_{ticker}' 形式の合成キーである"""
        items = [_make_spot_item(ticker="9984")]
        result = mapper.map_spot_positions(items)
        assert result[0].broker_order_id == "spot_9984"

    def test_empty_items_returns_empty(self):
        """空リスト → 空リストを返す"""
        result = mapper.map_spot_positions([])
        assert result == []

    def test_missing_ticker_is_skipped(self):
        """sUriOrderIssueCode が空の行はスキップされる"""
        items = [
            {"sUriOrderIssueCode": "", "sUriOrderZanKabuSuryou": "100", "sUriOrderGaisanBokaTanka": "2500"},
            _make_spot_item(ticker="7203"),
        ]
        result = mapper.map_spot_positions(items)
        assert len(result) == 1
        assert result[0].ticker == "7203"

    def test_comma_separated_values(self):
        """カンマ区切り数値（"2,500"）も正しく変換される"""
        items = [_make_spot_item(qty="1,000", price="2,500")]
        result = mapper.map_spot_positions(items)
        assert result[0].quantity      == 1000
        assert result[0].average_price == pytest.approx(2500.0)

    def test_multiple_items_all_returned(self):
        """複数銘柄が全件返る"""
        items = [
            _make_spot_item(ticker="7203"),
            _make_spot_item(ticker="9984"),
        ]
        result = mapper.map_spot_positions(items)
        assert len(result) == 2


# ─── mapper 単体テスト: parse_spot_positions_response ───────────────────────

class TestParseSpotPositionsResponse:
    """mapper.parse_spot_positions_response の単体テスト"""

    def test_extracts_from_genbutu_kabu_list(self):
        """aGenbutuKabuList キーからリストを取り出す"""
        raw = _make_spot_response([_make_spot_item(ticker="7203")])
        result = mapper.parse_spot_positions_response(raw)
        assert len(result) == 1
        assert result[0].ticker == "7203"

    def test_empty_list_returns_empty(self):
        """aGenbutuKabuList が空リストの場合は空リストを返す"""
        raw = _make_spot_response([])
        result = mapper.parse_spot_positions_response(raw)
        assert result == []

    def test_missing_list_key_returns_empty(self):
        """aGenbutuKabuList キーが存在しない場合は空リストを返す"""
        raw = {"sResultCode": "0", "sResultText": "正常"}
        result = mapper.parse_spot_positions_response(raw)
        assert result == []

    def test_non_list_value_returns_empty(self):
        """aGenbutuKabuList の値がリストでない場合は空リストを返す"""
        raw = {"sResultCode": "0", "aGenbutuKabuList": "INVALID"}
        result = mapper.parse_spot_positions_response(raw)
        assert result == []


# ─── mapper 単体テスト: parse_margin_positions_response ─────────────────────

class TestParseMarginPositionsResponse:
    """mapper.parse_margin_positions_response の単体テスト"""

    def test_extracts_from_shinyou_tategyoku_list(self):
        """aShinyouTategyokuList キーからリストを取り出す"""
        raw = _make_margin_response([_make_margin_item(ticker="9984")])
        result = mapper.parse_margin_positions_response(raw)
        assert len(result) == 1
        assert result[0].ticker == "9984"

    def test_empty_list_returns_empty(self):
        """aShinyouTategyokuList が空リストの場合は空リストを返す"""
        raw = _make_margin_response([])
        result = mapper.parse_margin_positions_response(raw)
        assert result == []

    def test_missing_list_key_returns_empty(self):
        """aShinyouTategyokuList キーが存在しない場合は空リストを返す"""
        raw = {"sResultCode": "0", "sResultText": "正常"}
        result = mapper.parse_margin_positions_response(raw)
        assert result == []

    def test_non_list_value_returns_empty(self):
        """aShinyouTategyokuList の値がリストでない場合は空リストを返す"""
        raw = {"sResultCode": "0", "aShinyouTategyokuList": None}
        result = mapper.parse_margin_positions_response(raw)
        assert result == []

    def test_margin_sell_side(self):
        """sBaibaiKubun="1"（売）は Side.SELL にマップされる"""
        raw = _make_margin_response([_make_margin_item(baibai_kubun="1")])
        result = mapper.parse_margin_positions_response(raw)
        assert len(result) == 1
        assert result[0].side == Side.SELL
