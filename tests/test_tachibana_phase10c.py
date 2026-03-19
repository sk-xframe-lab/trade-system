"""
Phase 10-C: TachibanaClient / TachibanaSessionManager / TachibanaMapper テスト

ネットワーク実接続なし。httpx._http.post を AsyncMock でモックする。

カバー範囲:
  - Shift-JIS レスポンス処理
  - ログイン応答から仮想 URL 群を保持できること
  - sKinsyouhouMidokuFlg=1 で利用不可扱いになること
  - broker_order_id encode / decode
  - status code mapping
  - cancel pending の表現
  - execution_key 生成
  - OrderRequest → Tachibana リクエスト JSON mapping
  - Tachibana レスポンス → 内部レスポンス mapping
  - TachibanaClient のエラーハンドリング
  - TachibanaSessionManager の再ログイン制御
"""
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from trade_app.brokers.base import (
    BrokerAPIError,
    BrokerAuthError,
    BrokerMaintenanceError,
    BrokerTemporaryError,
    CancelResult,
    OrderRequest,
)
from trade_app.brokers.tachibana.client import TachibanaClient
from trade_app.brokers.tachibana.mapper import (
    ExecutionDetail,
    decode_broker_order_id,
    encode_broker_order_id,
    make_execution_key,
    map_balance,
    map_new_order_request,
    map_order_list_detail,
    map_order_response,
    map_order_status,
    map_positions,
    _map_status_code,
)
from trade_app.brokers.tachibana.session import TachibanaSessionManager
from trade_app.models.enums import OrderStatus, OrderType, Side


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _shiftjis_response(data: dict, status_code: int = 200) -> MagicMock:
    """Shift-JIS エンコードの JSON レスポンスモックを作成する"""
    body = json.dumps(data, ensure_ascii=False).encode("shift-jis")
    mock_resp = MagicMock()
    mock_resp.content = body
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _http_error_response(status_code: int) -> MagicMock:
    """HTTP エラーレスポンスモック (raise_for_status が HTTPStatusError を送出)"""
    mock_resp = MagicMock()
    mock_resp.content = b""
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock(status_code=status_code)
        )
    )
    return mock_resp


def _patch_client(client: TachibanaClient, response: MagicMock) -> None:
    """TachibanaClient._http をモックに差し替える"""
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=response)
    mock_http.aclose = AsyncMock()
    client._http = mock_http


def _make_order_request(**kwargs) -> OrderRequest:
    defaults = dict(
        client_order_id="order-001",
        ticker="7203",
        order_type=OrderType.MARKET,
        side=Side.BUY,
        quantity=100,
        account_type="cash",
    )
    defaults.update(kwargs)
    return OrderRequest(**defaults)


def _make_login_data(
    result_code: str = "0",
    kinsyouhou: str = "0",
) -> dict:
    """ログインレスポンスモック（仕様書確認済み名前付き URL フィールド）"""
    return {
        "sResultCode":           result_code,
        "sResultText":           "正常終了",
        "sKinsyouhouMidokuFlg":  kinsyouhou,
        "sUrlRequest":           "https://virtual.example.com/request",
        "sUrlMaster":            "https://virtual.example.com/master",
        "sUrlPrice":             "https://virtual.example.com/price",
        "sUrlEvent":             "https://virtual.example.com/event",
    }


def _make_session(mock_client) -> TachibanaSessionManager:
    return TachibanaSessionManager(
        client=mock_client,
        login_url="https://login.example.com/",
        user_id="test_user",
        password="test_pass",
        second_password="test_2nd",
    )


# ─── TachibanaClient: Shift-JIS デコード ─────────────────────────────────────

class TestTachibanaClientShiftJis:

    @pytest.mark.asyncio
    async def test_shiftjis_decoded_correctly(self):
        """Shift-JIS エンコードの日本語レスポンスが正しくデコードされる"""
        client = TachibanaClient()
        _patch_client(client, _shiftjis_response({
            "sResultCode": "0",
            "sResultText": "正常終了",
            "sUrlRequest": "https://example.com/api",
        }))

        result = await client.request("https://example.com/login", {"sCLMID": "test"})

        assert result["sResultText"] == "正常終了"
        assert result["sUrlRequest"] == "https://example.com/api"
        await client.close()

    @pytest.mark.asyncio
    async def test_multibyte_chars_decoded(self):
        """マルチバイト文字（日本語銘柄名等）も正しくデコードされる"""
        client = TachibanaClient()
        _patch_client(client, _shiftjis_response({
            "sResultCode": "0",
            "sMeisyou": "トヨタ自動車株式会社",
        }))

        result = await client.request("https://example.com/api", {})
        assert result["sMeisyou"] == "トヨタ自動車株式会社"
        await client.close()

    @pytest.mark.asyncio
    async def test_result_code_zero_returns_data(self):
        """sResultCode="0" でデータが正常に返る"""
        client = TachibanaClient()
        _patch_client(client, _shiftjis_response({
            "sResultCode": "0",
            "sOrderNumber": "00123",
        }))

        result = await client.request("https://example.com/api", {})
        assert result["sOrderNumber"] == "00123"
        await client.close()


# ─── TachibanaClient: エラーハンドリング ─────────────────────────────────────

class TestTachibanaClientErrors:

    @pytest.mark.asyncio
    async def test_non_zero_result_code_raises_broker_api_error(self):
        """sResultCode != "0" で BrokerAPIError を送出する"""
        client = TachibanaClient()
        _patch_client(client, _shiftjis_response({"sResultCode": "B001", "sResultText": "残高不足"}))

        with pytest.raises(BrokerAPIError, match="B001"):
            await client.request("https://example.com/api", {})
        await client.close()

    @pytest.mark.asyncio
    async def test_auth_error_code_raises_broker_auth_error(self):
        """認証エラーコード（仕様書確認済み: 900002）で BrokerAuthError を送出する"""
        client = TachibanaClient()
        _patch_client(client, _shiftjis_response({"sResultCode": "900002", "sResultText": "パスワード不正"}))

        with pytest.raises(BrokerAuthError):
            await client.request("https://example.com/login", {})
        await client.close()

    @pytest.mark.asyncio
    async def test_maintenance_code_raises_broker_maintenance_error(self):
        """メンテナンスコード（仕様書確認済み: 990002）で BrokerMaintenanceError を送出する"""
        client = TachibanaClient()
        _patch_client(client, _shiftjis_response({"sResultCode": "990002", "sResultText": "メンテ中"}))

        with pytest.raises(BrokerMaintenanceError):
            await client.request("https://example.com/api", {})
        await client.close()

    @pytest.mark.asyncio
    async def test_login_failed_code_raises_broker_auth_error(self):
        """ログイン失敗コード（10031）で BrokerAuthError を送出する"""
        client = TachibanaClient()
        _patch_client(client, _shiftjis_response({"sResultCode": "10031", "sResultText": "ログイン認証失敗"}))

        with pytest.raises(BrokerAuthError):
            await client.request("https://example.com/login", {})
        await client.close()

    @pytest.mark.asyncio
    async def test_second_password_error_raises_broker_auth_error(self):
        """第二暗証番号エラー（991036）で BrokerAuthError を送出する"""
        client = TachibanaClient()
        _patch_client(client, _shiftjis_response({"sResultCode": "991036", "sResultText": "第二暗証番号エラー"}))

        with pytest.raises(BrokerAuthError):
            await client.request("https://example.com/api", {})
        await client.close()

    @pytest.mark.asyncio
    async def test_timeout_raises_broker_temporary_error(self):
        """タイムアウトで BrokerTemporaryError を送出する"""
        client = TachibanaClient()
        mock_http = MagicMock()
        mock_http.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_http.aclose = AsyncMock()
        client._http = mock_http

        with pytest.raises(BrokerTemporaryError, match="timeout"):
            await client.request("https://example.com/api", {})
        await client.close()

    @pytest.mark.asyncio
    async def test_network_error_raises_broker_temporary_error(self):
        """ネットワークエラーで BrokerTemporaryError を送出する"""
        client = TachibanaClient()
        mock_http = MagicMock()
        mock_http.get = AsyncMock(side_effect=httpx.NetworkError("connection refused"))
        mock_http.aclose = AsyncMock()
        client._http = mock_http

        with pytest.raises(BrokerTemporaryError, match="Network error"):
            await client.request("https://example.com/api", {})
        await client.close()

    @pytest.mark.asyncio
    async def test_http_5xx_raises_broker_api_error(self):
        """HTTP 5xx で BrokerAPIError を送出する"""
        client = TachibanaClient()
        _patch_client(client, _http_error_response(500))

        with pytest.raises(BrokerAPIError):
            await client.request("https://example.com/api", {})
        await client.close()

    @pytest.mark.asyncio
    async def test_invalid_json_raises_broker_api_error(self):
        """JSON パース失敗で BrokerAPIError を送出する"""
        client = TachibanaClient()
        mock_resp = MagicMock()
        mock_resp.content = "not json".encode("shift-jis")
        mock_resp.raise_for_status = MagicMock()
        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.aclose = AsyncMock()
        client._http = mock_http

        with pytest.raises(BrokerAPIError, match="Invalid JSON"):
            await client.request("https://example.com/api", {})
        await client.close()


# ─── TachibanaSessionManager: ログイン・仮想 URL ─────────────────────────────

class TestTachibanaSessionLogin:

    @pytest.mark.asyncio
    async def test_login_stores_virtual_urls(self):
        """ログイン後に sUrlRequest/sUrlMaster/sUrlPrice/sUrlEvent が保持される"""
        mock_client = MagicMock(spec=TachibanaClient)
        mock_client.request = AsyncMock(return_value=_make_login_data())
        session = _make_session(mock_client)

        await session.login()

        assert session.url_request == "https://virtual.example.com/request"
        assert session.url_master  == "https://virtual.example.com/master"
        assert session.url_price   == "https://virtual.example.com/price"
        assert session.url_event   == "https://virtual.example.com/event"
        # url_order は url_request の alias
        assert session.url_order   == "https://virtual.example.com/request"

    @pytest.mark.asyncio
    async def test_login_usable_when_kinsyouhou_zero(self):
        """sKinsyouhouMidokuFlg=0 → is_usable=True"""
        mock_client = MagicMock(spec=TachibanaClient)
        mock_client.request = AsyncMock(return_value=_make_login_data(kinsyouhou="0"))
        session = _make_session(mock_client)

        await session.login()

        assert session.is_usable is True
        assert session.kinsyouhou_midoku is False

    @pytest.mark.asyncio
    async def test_kinsyouhou_midoku_makes_unusable(self):
        """sKinsyouhouMidokuFlg=1 → is_usable=False、kinsyouhou_midoku=True"""
        mock_client = MagicMock(spec=TachibanaClient)
        mock_client.request = AsyncMock(return_value=_make_login_data(kinsyouhou="1"))
        session = _make_session(mock_client)

        await session.login()

        assert session.is_usable is False
        assert session.kinsyouhou_midoku is True

    @pytest.mark.asyncio
    async def test_kinsyouhou_virtual_urls_still_stored(self):
        """sKinsyouhouMidokuFlg=1 でも仮想 URL は保持される（将来の参考のため）"""
        mock_client = MagicMock(spec=TachibanaClient)
        mock_client.request = AsyncMock(return_value=_make_login_data(kinsyouhou="1"))
        session = _make_session(mock_client)

        await session.login()

        # URL は保持されているが is_usable=False で使えない状態
        assert session.url_request == "https://virtual.example.com/request"
        assert session.is_usable is False


# ─── TachibanaSessionManager: ensure_session・invalidate ─────────────────────

class TestTachibanaSessionEnsure:

    @pytest.mark.asyncio
    async def test_ensure_session_logs_in_if_not_usable(self):
        """未ログイン時に ensure_session がログインを実行する"""
        mock_client = MagicMock(spec=TachibanaClient)
        mock_client.request = AsyncMock(return_value=_make_login_data())
        session = _make_session(mock_client)

        assert not session.is_usable
        await session.ensure_session()

        assert session.is_usable is True
        assert mock_client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_ensure_session_no_relogin_if_usable(self):
        """ログイン済み かつ usable なら ensure_session が再ログインしない"""
        mock_client = MagicMock(spec=TachibanaClient)
        mock_client.request = AsyncMock(return_value=_make_login_data())
        session = _make_session(mock_client)

        await session.login()
        count_after_login = mock_client.request.call_count

        await session.ensure_session()
        assert mock_client.request.call_count == count_after_login  # 追加なし

    @pytest.mark.asyncio
    async def test_invalidate_clears_session(self):
        """invalidate() 後は is_usable=False になり仮想 URL が消える"""
        mock_client = MagicMock(spec=TachibanaClient)
        mock_client.request = AsyncMock(return_value=_make_login_data())
        session = _make_session(mock_client)

        await session.login()
        assert session.is_usable is True

        session.invalidate()

        assert session.is_usable is False
        assert session.url_request == ""
        assert session.url_order   == ""

    @pytest.mark.asyncio
    async def test_invalidate_then_ensure_session_relogins(self):
        """invalidate() 後の ensure_session は再ログインする"""
        mock_client = MagicMock(spec=TachibanaClient)
        mock_client.request = AsyncMock(return_value=_make_login_data())
        session = _make_session(mock_client)

        await session.login()
        session.invalidate()
        await session.ensure_session()

        assert session.is_usable is True
        assert mock_client.request.call_count == 2  # 初回 + 再ログイン

    @pytest.mark.asyncio
    async def test_get_url_before_login_returns_empty(self):
        """ログイン前は get_url() / url_request が空文字列を返す"""
        mock_client = MagicMock(spec=TachibanaClient)
        session = _make_session(mock_client)

        assert session.get_url(0) == ""
        assert session.url_request == ""
        assert session.url_order   == ""

    @pytest.mark.asyncio
    async def test_auth_error_on_login_clears_state(self):
        """ログイン中に BrokerAuthError が発生するとセッション状態がクリアされる"""
        mock_client = MagicMock(spec=TachibanaClient)
        mock_client.request = AsyncMock(side_effect=BrokerAuthError("auth failed"))
        session = _make_session(mock_client)

        with pytest.raises(BrokerAuthError):
            await session.login()

        assert session.is_usable is False
        assert session.url_request == ""


# ─── broker_order_id encode / decode ─────────────────────────────────────────

class TestBrokerOrderId:

    def test_encode(self):
        assert encode_broker_order_id("20260316", "00123") == "20260316_00123"

    def test_encode_with_leading_zeros(self):
        assert encode_broker_order_id("20260316", "00001") == "20260316_00001"

    def test_decode(self):
        day, num = decode_broker_order_id("20260316_00123")
        assert day == "20260316"
        assert num == "00123"

    def test_roundtrip(self):
        eigyou_day, order_number = "20260316", "99999"
        encoded = encode_broker_order_id(eigyou_day, order_number)
        decoded = decode_broker_order_id(encoded)
        assert decoded == (eigyou_day, order_number)

    def test_decode_invalid_no_sep(self):
        with pytest.raises(ValueError):
            decode_broker_order_id("invalid")

    def test_decode_invalid_empty(self):
        with pytest.raises(ValueError):
            decode_broker_order_id("")

    def test_decode_invalid_missing_part(self):
        """セパレータはあるが片方が空"""
        with pytest.raises(ValueError):
            decode_broker_order_id("_00123")

    def test_different_eigyou_days_differ(self):
        id1 = encode_broker_order_id("20260316", "00123")
        id2 = encode_broker_order_id("20260317", "00123")
        assert id1 != id2


# ─── execution_key 生成 ───────────────────────────────────────────────────────

class TestExecutionKey:

    def test_basic_format(self):
        key = make_execution_key("20260316", "00123", "153045", 100)
        assert key == "20260316_00123_153045_100"

    def test_different_time_different_key(self):
        k1 = make_execution_key("20260316", "00123", "100000", 50)
        k2 = make_execution_key("20260316", "00123", "100100", 50)
        assert k1 != k2

    def test_different_qty_different_key(self):
        k1 = make_execution_key("20260316", "00123", "100000", 50)
        k2 = make_execution_key("20260316", "00123", "100000", 100)
        assert k1 != k2

    def test_same_inputs_same_key(self):
        k1 = make_execution_key("20260316", "00123", "100000", 100)
        k2 = make_execution_key("20260316", "00123", "100000", 100)
        assert k1 == k2

    def test_different_order_number_different_key(self):
        k1 = make_execution_key("20260316", "00123", "100000", 100)
        k2 = make_execution_key("20260316", "00456", "100000", 100)
        assert k1 != k2


# ─── status code mapping（仕様書確認済み） ────────────────────────────────────

class TestStatusCodeMapping:

    def test_pending(self):
        """0=受付未済 → PENDING"""
        assert _map_status_code("0") == OrderStatus.PENDING

    def test_submitted(self):
        """1=未約定 → SUBMITTED"""
        assert _map_status_code("1") == OrderStatus.SUBMITTED

    def test_rejected(self):
        """2=受付エラー → REJECTED"""
        assert _map_status_code("2") == OrderStatus.REJECTED

    def test_amendment_and_cancel_in_progress_is_submitted(self):
        """3=訂正中, 6=取消中 → SUBMITTED（注文はまだ有効）"""
        assert _map_status_code("3") == OrderStatus.SUBMITTED
        assert _map_status_code("6") == OrderStatus.SUBMITTED

    def test_amendment_complete_is_submitted(self):
        """4=訂正完了 → SUBMITTED（注文はまだ有効）"""
        assert _map_status_code("4") == OrderStatus.SUBMITTED

    def test_cancelled(self):
        """7=取消完了 → CANCELLED"""
        assert _map_status_code("7") == OrderStatus.CANCELLED

    def test_partial(self):
        """9=一部約定 → PARTIAL"""
        assert _map_status_code("9") == OrderStatus.PARTIAL

    def test_filled(self):
        """10=全部約定 → FILLED"""
        assert _map_status_code("10") == OrderStatus.FILLED

    def test_unknown_code_returns_unknown(self):
        assert _map_status_code("99") == OrderStatus.UNKNOWN

    def test_empty_code_returns_unknown(self):
        assert _map_status_code("") == OrderStatus.UNKNOWN


# ─── CancelResult.is_pending ─────────────────────────────────────────────────

class TestCancelResultIsPending:

    def test_default_is_pending_false(self):
        """デフォルトは is_pending=False（後方互換）"""
        r = CancelResult(success=True)
        assert r.is_pending is False

    def test_is_pending_true(self):
        """取消受付済みだが完了未確認の状態を表現できる"""
        r = CancelResult(success=True, is_pending=True, reason="取消受付済み・完了未確認")
        assert r.is_pending is True
        assert r.success is True

    def test_is_pending_independent_of_is_already_terminal(self):
        """is_pending と is_already_terminal は独立したフラグ"""
        r = CancelResult(success=False, is_already_terminal=True, is_pending=False)
        assert r.is_already_terminal is True
        assert r.is_pending is False

    def test_is_pending_with_success_false(self):
        """success=False でも is_pending=True にできる（取消送信後の通信失敗）"""
        r = CancelResult(success=False, is_pending=True, reason="通信エラー")
        assert r.is_pending is True
        assert r.success is False


# ─── OrderRequest → Tachibana リクエスト変換 ─────────────────────────────────

class TestMapNewOrderRequest:

    def test_market_buy_cash(self):
        req = _make_order_request(order_type=OrderType.MARKET, side=Side.BUY, account_type="cash")
        payload = map_new_order_request(req, second_password="pass2", tax_type="3", market_code="00")

        assert payload["sCLMID"]               == "CLMKabuNewOrder"
        assert payload["sIssueCode"]           == "7203"
        assert payload["sBaibaiKubun"]         == "3"    # 買方向
        assert payload["sGenkinShinyouKubun"]  == "0"    # 現物
        assert payload["sCondition"]           == "2"    # 成行
        assert payload["sOrderPrice"]          == "0"
        assert payload["sSecondPassword"]      == "pass2"
        assert payload["sZyoutoekiKazeiC"]     == "3"    # 仕様書確認済み（旧: sTaxType）
        assert payload["sSizyouC"]             == "00"   # 仕様書確認済み（旧: sSizyouCode）
        assert payload["sOrderSuryou"]         == "100"

    def test_limit_sell_cash(self):
        req = _make_order_request(
            order_type=OrderType.LIMIT, side=Side.SELL,
            quantity=200, limit_price=1500.0, account_type="cash",
        )
        payload = map_new_order_request(req, second_password="pass2")

        assert payload["sBaibaiKubun"]        == "1"      # 売方向
        assert payload["sGenkinShinyouKubun"] == "0"      # 現物
        assert payload["sCondition"]          == "0"      # 指値
        assert payload["sOrderPrice"]         == "1500.0"
        assert payload["sOrderSuryou"]        == "200"

    def test_margin_buy(self):
        req = _make_order_request(side=Side.BUY, account_type="margin")
        payload = map_new_order_request(req, second_password="pass2")
        assert payload["sBaibaiKubun"]        == "3"   # 買方向
        assert payload["sGenkinShinyouKubun"] == "2"   # 信用新規（暫定）

    def test_margin_sell(self):
        req = _make_order_request(side=Side.SELL, account_type="margin")
        payload = map_new_order_request(req, second_password="pass2")
        assert payload["sBaibaiKubun"]        == "1"   # 売方向
        assert payload["sGenkinShinyouKubun"] == "2"   # 信用新規（暫定）

    def test_all_values_are_str(self):
        """e_api の form-encoded に合わせて全値が str 型であること"""
        req = _make_order_request()
        payload = map_new_order_request(req, second_password="pass2")
        for k, v in payload.items():
            assert isinstance(v, str), f"{k}: {v!r} is not str"

    def test_market_price_is_zero(self):
        """成行注文の sOrderPrice は '0'"""
        req = _make_order_request(order_type=OrderType.MARKET)
        payload = map_new_order_request(req, second_password="pass2")
        assert payload["sOrderPrice"] == "0"

    def test_unsupported_combination_raises(self):
        """サポートしていない side/account_type の組み合わせで ValueError"""
        req = _make_order_request(side=Side.BUY, account_type="unknown_type")
        with pytest.raises(ValueError, match="Unsupported"):
            map_new_order_request(req, second_password="pass2")


# ─── Tachibana レスポンス → 内部レスポンス変換 ───────────────────────────────

class TestMapOrderResponse:

    def test_basic(self):
        raw = {"sResultCode": "0", "sResultText": "受付", "sOrderNumber": "00123", "sEigyouDay": "20260316"}
        resp = map_order_response(raw)

        assert resp.broker_order_id == "20260316_00123"
        assert resp.status          == OrderStatus.SUBMITTED
        assert resp.message         == "受付"

    def test_broker_order_id_is_decodable(self):
        """生成された broker_order_id が decode_broker_order_id で復元できる"""
        raw = {"sResultCode": "0", "sOrderNumber": "99999", "sEigyouDay": "20260101"}
        resp = map_order_response(raw)

        day, num = decode_broker_order_id(resp.broker_order_id)
        assert day == "20260101"
        assert num == "99999"


class TestMapOrderStatus:

    def test_filled_order(self):
        raw = {
            "sOrderNumber": "00123", "sEigyouDay": "20260316",
            "sOrderStatusCode": "10",   # 全部約定
            "sOrderSuryou": "100", "sYakuzyouSuryou": "100",
            "sYakuzyouKingaku": "150000", "sCancelSuryou": "0",
        }
        resp = map_order_status(raw)

        assert resp.status          == OrderStatus.FILLED
        assert resp.filled_quantity == 100
        assert resp.filled_price    == pytest.approx(1500.0)
        assert resp.remaining_qty   == 0
        assert resp.cancel_qty      == 0

    def test_partial_order(self):
        raw = {
            "sOrderNumber": "00456", "sEigyouDay": "20260316",
            "sOrderStatusCode": "9",    # 一部約定
            "sOrderSuryou": "100", "sYakuzyouSuryou": "30",
            "sYakuzyouKingaku": "45000", "sCancelSuryou": "0",
        }
        resp = map_order_status(raw)

        assert resp.status          == OrderStatus.PARTIAL
        assert resp.filled_quantity == 30
        assert resp.filled_price    == pytest.approx(1500.0)
        assert resp.remaining_qty   == 70

    def test_cancelled_order(self):
        raw = {
            "sOrderNumber": "00789", "sEigyouDay": "20260316",
            "sOrderStatusCode": "7",    # 取消完了
            "sOrderSuryou": "100", "sYakuzyouSuryou": "0",
            "sYakuzyouKingaku": "0", "sCancelSuryou": "100",
        }
        resp = map_order_status(raw)

        assert resp.status     == OrderStatus.CANCELLED
        assert resp.cancel_qty == 100

    def test_execution_key_from_yakuzyou_list(self):
        """aYakuzyouSikkouList があれば最新約定の execution_key が broker_execution_id になる"""
        raw = {
            "sOrderNumber": "00123", "sEigyouDay": "20260316",
            "sOrderStatusCode": "10",
            "sOrderSuryou": "100", "sYakuzyouSuryou": "100",
            "sYakuzyouKingaku": "150000", "sCancelSuryou": "0",
            "aYakuzyouSikkouList": [
                {"sYakuzyouDate": "100000", "sYakuzyouSuryou": "100", "sYakuzyouPrice": "1500"},
            ],
        }
        resp = map_order_status(raw)
        assert resp.broker_execution_id == "20260316_00123_100000_100"

    def test_no_execution_key_without_yakuzyou_list(self):
        """aYakuzyouSikkouList がない場合は broker_execution_id=None"""
        raw = {
            "sOrderNumber": "00123", "sEigyouDay": "20260316",
            "sOrderStatusCode": "1",
            "sOrderSuryou": "100", "sYakuzyouSuryou": "0",
            "sYakuzyouKingaku": "0", "sCancelSuryou": "0",
        }
        resp = map_order_status(raw)
        assert resp.broker_execution_id is None

    def test_comma_formatted_numbers(self):
        """カンマ区切り数値も正しく変換される"""
        raw = {
            "sOrderNumber": "00123", "sEigyouDay": "20260316",
            "sOrderStatusCode": "10",
            "sOrderSuryou": "1,000", "sYakuzyouSuryou": "1,000",
            "sYakuzyouKingaku": "1,500,000", "sCancelSuryou": "0",
        }
        resp = map_order_status(raw)
        assert resp.filled_quantity == 1000
        assert resp.filled_price    == pytest.approx(1500.0)


class TestMapOrderListDetail:

    def test_basic(self):
        """sYakuzyouDate フィールド（旧: sYakuzyouTime）を使用する"""
        raw = {"sYakuzyouSuryou": "100", "sYakuzyouPrice": "2500", "sYakuzyouDate": "153045"}
        detail = map_order_list_detail(raw, eigyou_day="20260316", order_number="00123")

        assert isinstance(detail, ExecutionDetail)
        assert detail.execution_key  == "20260316_00123_153045_100"
        assert detail.qty            == 100
        assert detail.price          == pytest.approx(2500.0)
        assert detail.yakuzyou_time  == "153045"
        assert detail.eigyou_day     == "20260316"
        assert detail.order_number   == "00123"


class TestMapBalance:

    def test_basic(self):
        """CLMZanKaiKanougaku + CLMZanShinkiKanoIjiritu の2API方式"""
        raw_cash   = {"sSummaryGenkabuKaituke": "1000000"}
        raw_margin = {"sSummarySinyouSinkidate": "500000"}
        balance = map_balance(raw_cash, raw_margin)

        assert balance.cash_balance     == pytest.approx(1000000.0)
        assert balance.margin_available == pytest.approx(500000.0)

    def test_comma_formatted(self):
        raw_cash   = {"sSummaryGenkabuKaituke": "1,000,000"}
        raw_margin = {"sSummarySinyouSinkidate": "500,000"}
        balance = map_balance(raw_cash, raw_margin)
        assert balance.cash_balance == pytest.approx(1000000.0)

    def test_missing_margin_defaults_to_zero(self):
        """raw_margin=None の場合 margin_available=0（信用口座なしデグレード）"""
        raw_cash = {"sSummaryGenkabuKaituke": "1000000"}
        balance = map_balance(raw_cash, None)
        assert balance.cash_balance     == pytest.approx(1000000.0)
        assert balance.margin_available == 0.0

    def test_missing_fields_default_to_zero(self):
        balance = map_balance({}, {})
        assert balance.cash_balance     == 0.0
        assert balance.margin_available == 0.0


class TestMapPositions:
    """map_positions は map_margin_positions の後方互換エイリアス（信用建玉変換）"""

    def test_single_sell_position(self):
        """sBaibaiKubun="1"（売方向）→ Side.SELL"""
        raw = [{
            "sOrderIssueCode": "7203", "sBaibaiKubun": "1",
            "sOrderTategyokuSuryou": "100", "sOrderTategyokuTanka": "2500",
        }]
        positions = map_positions(raw)

        assert len(positions) == 1
        pos = positions[0]
        assert pos.ticker        == "7203"
        assert pos.side          == Side.SELL
        assert pos.quantity      == 100
        assert pos.average_price == pytest.approx(2500.0)

    def test_single_buy_position(self):
        """sBaibaiKubun="3"（買方向）→ Side.BUY"""
        raw = [{
            "sOrderIssueCode": "9984", "sBaibaiKubun": "3",
            "sOrderTategyokuSuryou": "50", "sOrderTategyokuTanka": "3000",
        }]
        positions = map_positions(raw)
        assert positions[0].side == Side.BUY

    def test_empty_list(self):
        assert map_positions([]) == []

    def test_multiple_positions(self):
        raw = [
            {"sOrderIssueCode": "7203", "sBaibaiKubun": "1",
             "sOrderTategyokuSuryou": "100", "sOrderTategyokuTanka": "2500"},
            {"sOrderIssueCode": "9984", "sBaibaiKubun": "3",
             "sOrderTategyokuSuryou": "50", "sOrderTategyokuTanka": "3000"},
        ]
        positions = map_positions(raw)
        assert len(positions) == 2
        assert positions[0].ticker == "7203"
        assert positions[0].side   == Side.SELL
        assert positions[1].ticker == "9984"
        assert positions[1].side   == Side.BUY
