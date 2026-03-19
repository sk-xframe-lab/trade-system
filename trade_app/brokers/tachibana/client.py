"""
立花証券 e_api 低レベル HTTP クライアント

責務:
  - 非同期 HTTP 通信（httpx ベース）
  - Shift-JIS デコード
  - GET リクエストの共通化（JSON クエリ文字列形式）
  - p_no 単調増加・p_sd_date 自動付与
  - 数値キー → 文字列キーへの正規化
  - タイムアウト制御
  - p_errno（通信レベル）+ sResultCode（業務レベル）の2層エラー変換

責務外（他コンポーネントが担当）:
  - セッション管理・仮想 URL の保持 → TachibanaSessionManager
  - Tachibana 固有 JSON 組み立て・レスポンス変換 → TachibanaMapper

【API 通信方式】
  立花証券 e_api は GET リクエスト + JSON クエリ文字列を使用する。
  認証: GET {base_url}/auth/?{JSON}
  業務: GET {sUrlRequest}?{JSON}
  p_no はリクエストごとに単調増加させること（同一値を再送すると p_errno=6）。
  p_sd_date は JST 形式 yyyy.mm.dd-hh:mn:ss.ttt で付与すること。
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from trade_app.brokers.base import (
    BrokerAPIError,
    BrokerAuthError,
    BrokerMaintenanceError,
    BrokerTemporaryError,
)

logger = logging.getLogger(__name__)


# ─── レスポンス数値キー → 文字列キー変換マップ ─────────────────────────────────
#
# 立花証券 e_api はレスポンスのキーを数値文字列で返す。
# このマップで文字列キーに変換して、後続処理が文字列キーを前提として動けるようにする。
# 未知の数値キーは変換せずそのまま保持する（実測後にマップを追加すること）。

_NUMERIC_KEY_MAP: dict[str, str] = {
    # ─── 共通フィールド ───────────────────────────────────────────────────────
    "286": "p_err_msg",               # p_errno メッセージ（エラー時の説明文）
    "287": "p_errno",                 # 通信レベルエラーコード（0=正常）
    "288": "p_no",                    # リクエスト連番エコー
    "289": "p_sd_date",               # リクエスト日時エコー
    "290": "p_date",                  # レスポンス日時
    "334": "sCLMID",                  # コマンド識別子
    "688": "sResultCode",             # 業務レベル結果コード（"0"=正常）
    "689": "sResultText",             # 業務レベル結果メッセージ
    # ─── ログイン応答 CLMAuthLoginRequest ─────────────────────────────────────
    "700": "sKinsyouhouMidokuFlg",    # 禁止事項未読フラグ（"1"=未読）
    "868": "sUrlEvent",               # イベント通知 URL
    "869": "sUrlEventWebSocket",      # WebSocket URL（仕様書未記載・実測確認済み）
    "870": "sUrlMaster",              # マスターデータ URL
    "871": "sUrlPrice",               # 価格照会 URL
    "872": "sUrlRequest",             # 業務 API URL（注文・照会・残高・建玉）
    # ─── 残高照会 CLMZanKaiKanougaku ──────────────────────────────────────────
    "744": "sSummaryGenkabuKaituke",  # 現物買付可能額（円、整数文字列）
    # ─── 残高照会 CLMZanShinkiKanoIjiritu ─────────────────────────────────────
    "747": "sSummarySinyouSinkidate", # 信用新規建余力（円、整数文字列）
    # ─── 価格照会 CLMMfdsGetMarketPrice (sUrlPrice) ────────────────────────────
    # 注意: sUrlPrice レスポンスは sResultCode ("688") を含まない。
    #       p_errno ("287") = "0" のみで正常を判定する。
    # 配列キー "71" の各要素もネストした数値キーを持つ → _normalize_keys で再帰変換する。
    "71":  "aCLMMfdsMarketPrice",     # 価格データ配列（要素内の "115" も再帰変換される）
    "115": "pDPP",                    # 現在値（円）。取引時間外は空文字列
    # ─── 現物保有照会 CLMGenbutuKabuList ──────────────────────────────────────
    "88":  "aGenbutuKabuList",        # 現物保有一覧配列（実測確認済み）
    "859": "sUriOrderIssueCode",      # 銘柄コード（実測: "6501", "6502", "9984"）
    "860": "sUriOrderZanKabuSuryou",  # 残数量（純整数文字列: "200", "2000", "400"）
    "854": "sUriOrderGaisanBokaTanka",# 概算簿価単価（小数文字列: "4801.0000"）
    # ─── 信用建玉照会 CLMShinyouTategyokuList ─────────────────────────────────
    "95":  "aShinyouTategyokuList",   # 信用建玉一覧配列（実測確認済み）
    "638": "sOrderIssueCode",         # 銘柄コード（実測: "6504", "6505", "9001"）
    "618": "sBaibaiKubun",            # 売買区分（実測確認: "3"=買, "1"=売）
    "667": "sOrderTategyokuSuryou",   # 建玉数量（純整数文字列: "4000", "200", "1300"）
    "668": "sOrderTategyokuTanka",    # 建値（小数文字列: "580.0000", "1900.0000"）
}


# ─── p_errno 判定（通信レベル） ────────────────────────────────────────────────
#
# p_errno は通信・セッション制御レベルのエラーコード。sResultCode とは独立した層。
# 0 以外はエラー。認証系コードは再ログインが必要なことを示す。

_P_ERRNO_OK = 0

# 暫定: p_errno 認証エラーコード（セッション切れ・未ログイン）
# TODO: 仕様書で p_errno コード体系を確認すること。
_P_ERRNO_AUTH_CODES: frozenset[int] = frozenset({
    10001,   # 暫定: セッション認証エラー（再ログイン必要）
})


# ─── sResultCode 判定（業務レベル） ───────────────────────────────────────────

_RESULT_OK = "0"

# 仕様書確認済み認証エラーコード
_AUTH_ERROR_CODES: frozenset[str] = frozenset({
    "10031",   # ログイン認証失敗
    "900002",  # パスワード不正
    "991036",  # 第二暗証番号エラー
})

# 仕様書確認済みメンテナンスコード
# 991012: 実測確認済み「只今、一時的にこの業務はご利用できません。」（2026-03-18 発注 API 調査時）
_MAINTENANCE_CODES: frozenset[str] = frozenset({
    "990002", "990003", "990004", "990005", "990006", "990007",
    "991012",  # 一時的なサービス停止（実測確認済み）
})


class TachibanaClient:
    """
    立花証券 e_api 低レベル HTTP クライアント。

    Transport・Shift-JIS デコード・sResultCode エラー変換を担当する。
    業務ロジック・セッション管理は一切持たない。

    使い方:
        client = TachibanaClient(timeout_sec=10.0)
        data = await client.request(url, {"sCLMID": "CLMAuthLoginRequest", ...})
        await client.close()

    または async context manager として:
        async with TachibanaClient() as client:
            data = await client.request(url, payload)

    p_no と p_sd_date はリクエストごとに自動付与される。
    呼び出し側はこれらを payload に含めないこと（上書きされる）。
    """

    def __init__(self, timeout_sec: float = 10.0) -> None:
        self._http = httpx.AsyncClient(timeout=timeout_sec)
        # p_no: リクエストごとに単調増加させるセッション連番
        # 同一値の再送は p_errno=6 になるため、インスタンスで管理する
        self._p_no: int = 0

    def _next_p_no(self) -> str:
        """p_no を 1 増加させて文字列で返す。"""
        self._p_no += 1
        return str(self._p_no)

    _JST = timezone(timedelta(hours=9))

    @staticmethod
    def _p_sd_date() -> str:
        """
        現在時刻（JST 固定）を認証 I/F 仕様の形式で返す。
        形式: yyyy.mm.dd-hh:mn:ss.ttt
        例  : 2026.03.18-09:30:00.123

        注意: API サーバーは JST を前提とした時刻範囲チェック (p_errno=8) を行う。
        datetime.now() はコンテナの TZ 設定に依存するため、明示的に JST を使う。
        """
        now = datetime.now(TachibanaClient._JST)
        ms = now.microsecond // 1000
        return now.strftime("%Y.%m.%d-%H:%M:%S.") + f"{ms:03d}"

    async def request(self, url: str, payload: dict[str, str]) -> dict[str, Any]:
        """
        指定 URL に GET リクエストを送信し、Shift-JIS デコード・数値キー正規化して dict を返す。

        立花証券 e_api はログイン URL（/auth/）と仮想 URL（業務 API）の両方に対して
        GET + JSON クエリ文字列形式で通信する。
        p_no と p_sd_date はこのメソッドが自動付与する。

        Args:
            url:     送信先の完全 URL（認証 URL または仮想 URL）
            payload: リクエストパラメータ（p_no / p_sd_date は含めないこと）

        Returns:
            数値キー正規化済みの JSON レスポンス dict

        Raises:
            BrokerAuthError:        sResultCode が認証エラーコード
            BrokerMaintenanceError: sResultCode がメンテナンスコード
            BrokerAPIError:         その他 sResultCode エラー・HTTP エラー・JSON パース失敗
            BrokerTemporaryError:   タイムアウト・ネットワークエラー
        """
        full_payload = {
            "p_no": self._next_p_no(),
            "p_sd_date": self._p_sd_date(),
            **payload,
        }
        query_string = json.dumps(full_payload, ensure_ascii=False, separators=(",", ":"))
        try:
            response = await self._http.get(f"{url}?{query_string}")
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise BrokerTemporaryError(f"Request timeout: {url}") from exc
        except httpx.NetworkError as exc:
            raise BrokerTemporaryError(f"Network error: {url}") from exc
        except httpx.HTTPStatusError as exc:
            raise BrokerAPIError(
                f"HTTP {exc.response.status_code}: {url}"
            ) from exc

        data = self._decode_response(response.content, url)
        self._check_p_errno(data, url)
        self._check_result_code(data, url)
        return data

    def _decode_response(self, raw: bytes, url: str = "") -> dict[str, Any]:
        """
        Shift-JIS エンコードのレスポンスボディを dict にデコードし、数値キーを正規化して返す。

        立花証券 e_api は Shift-JIS エンコードの JSON を返す。
        Shift-JIS デコードに失敗した場合は UTF-8 にフォールバックしてログを出力する。
        デコード後に _normalize_keys() で数値キーを文字列キーに変換する。

        Raises:
            BrokerAPIError: JSON パースに失敗した場合
        """
        try:
            text = raw.decode("shift-jis")
        except UnicodeDecodeError:
            logger.warning(
                "Shift-JIS decode failed (url=%s), falling back to UTF-8", url
            )
            text = raw.decode("utf-8", errors="replace")

        try:
            raw_dict = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BrokerAPIError(
                f"Invalid JSON response from {url}: {text[:200]!r}"
            ) from exc

        return self._normalize_keys(raw_dict)

    @staticmethod
    def _normalize_keys(data: dict[str, Any]) -> dict[str, Any]:
        """
        数値キー形式のレスポンスを文字列キー形式に変換して返す（再帰対応）。

        _NUMERIC_KEY_MAP に登録済みのキーは対応する文字列キーに変換する。
        未知の数値キーはそのまま保持する（実測後にマップを追加すること）。
        元の data は変更しない。

        CLMMfdsGetMarketPrice の aCLMMfdsMarketPrice 配列のように、
        配列要素がネストした dict の場合も再帰的に変換する。
        """
        result: dict[str, Any] = {}
        for k, v in data.items():
            mapped = _NUMERIC_KEY_MAP.get(str(k))
            new_key = mapped if mapped else k
            if isinstance(v, dict):
                result[new_key] = TachibanaClient._normalize_keys(v)
            elif isinstance(v, list):
                result[new_key] = [
                    TachibanaClient._normalize_keys(elem) if isinstance(elem, dict) else elem
                    for elem in v
                ]
            else:
                result[new_key] = v
        return result

    def _check_p_errno(self, data: dict[str, Any], url: str = "") -> None:
        """
        p_errno（通信レベルエラー）を確認し、エラー時は例外を送出する。

        p_errno が存在しないか 0 の場合は何もしない。
        認証エラー p_errno は BrokerAuthError、それ以外は BrokerAPIError。

        暫定: p_errno コード体系は仕様書未確認の部分あり。
        TODO: 仕様書の p_errno 定義を確認して _P_ERRNO_AUTH_CODES を更新すること。
        """
        raw = data.get("p_errno")
        if raw is None:
            return
        try:
            errno = int(raw)
        except (ValueError, TypeError):
            return
        if errno == _P_ERRNO_OK:
            return

        msg = f"p_errno={errno} url={url}"
        if errno in _P_ERRNO_AUTH_CODES:
            raise BrokerAuthError(msg)
        raise BrokerAPIError(msg)

    def _check_result_code(self, data: dict[str, Any], url: str = "") -> None:
        """
        sResultCode を確認し、エラーコードに応じた例外を送出する。

        sResultCode == "0" または sResultCode キーが存在しない場合は何もしない（正常）。

        注意:
            CLMMfdsGetMarketPrice (sUrlPrice) のレスポンスは sResultCode を含まない。
            sResultCode が存在しない場合は p_errno=0 のみで正常と判定する（p_errno チェックは
            _check_p_errno が別途担当）。
            発注拒否系のコード（残高不足・時間外等）はここでは BrokerAPIError として送出する。
            adapter 側で sResultCode を参照して BrokerOrderError に変換すること。
            TODO: 発注拒否コードの体系は仕様書未確認。adapter 実装時に確認すること。
        """
        raw_code = data.get("sResultCode")
        if raw_code is None:
            # sResultCode が存在しない API（CLMMfdsGetMarketPrice 等）→ p_errno で正常判定済み
            return
        code = str(raw_code)
        if code == _RESULT_OK:
            return

        text = data.get("sResultText", "")
        msg = f"sResultCode={code} sResultText={text!r} url={url}"

        if code in _AUTH_ERROR_CODES:
            raise BrokerAuthError(msg)
        if code in _MAINTENANCE_CODES:
            raise BrokerMaintenanceError(msg)
        raise BrokerAPIError(msg)

    async def close(self) -> None:
        """HTTP クライアントをクローズする"""
        await self._http.aclose()

    async def __aenter__(self) -> "TachibanaClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
