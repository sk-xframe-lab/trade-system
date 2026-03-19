"""
立花証券 e_api セッション管理

責務:
  - CLMAuthLoginRequest によるログイン
  - 仮想 URL (sUrlRequest / sUrlMaster / sUrlPrice / sUrlEvent) の保持
  - 必要時の再ログイン (ensure_session)
  - sKinsyouhouMidokuFlg=1 の検出（禁止事項未読 → 取引不可状態の検知）
  - シングルセッション管理（asyncio.Lock による同時ログイン防止）

設計メモ:
  - ログイン URL は固定（TACHIBANA_BASE_URL で指定）
  - 業務 API は仮想 URL（ログイン後に取得）を使う
  - 業務 API (注文・取消・照会・残高・建玉) は sUrlRequest を使用する
  - 価格照会は sUrlPrice を使用する
  - 古い仮想 URL は invalidate() で無効化し、次回 ensure_session() で再ログインする
  - BrokerAuthError 受信時は adapter から invalidate() を呼ぶこと
"""
import asyncio
import logging
from dataclasses import dataclass

from trade_app.brokers.base import BrokerAuthError
from trade_app.brokers.tachibana.client import TachibanaClient

logger = logging.getLogger(__name__)


# ─── 定数 ─────────────────────────────────────────────────────────────────────

# ログイン API の識別子
_CLMID_LOGIN = "CLMAuthLoginRequest"

# sKinsyouhouMidokuFlg: 禁止事項の未読通知がある場合 "1"
# この状態では注文・照会等の業務 API が使用できない
_KINSYOUHOU_MIDOKU_FLAG = "1"


# ─── ユーティリティ ───────────────────────────────────────────────────────────

def _build_auth_url(base_url: str) -> str:
    """
    TACHIBANA_BASE_URL から認証エンドポイント URL を構築する。

    /auth/ で終わっていない場合は自動補完する。
      https://demo-kabuka.e-shiten.jp/e_api_v4r8/
        → https://demo-kabuka.e-shiten.jp/e_api_v4r8/auth/
      https://demo-kabuka.e-shiten.jp/e_api_v4r8/auth/
        → そのまま
    """
    stripped = base_url.rstrip("/")
    if stripped.endswith("/auth"):
        return stripped + "/"
    return stripped + "/auth/"


# ─── セッション状態 ───────────────────────────────────────────────────────────

@dataclass
class _SessionState:
    """
    セッション状態の snapshot。

    atomic な差し替えのため dataclass に集約する。
    新しい状態を作成して self._state に代入することで、読み取り中の state が
    中途半端に変更されることを防ぐ。
    """
    # ログイン応答から取得する仮想 URL（名前付きフィールド）
    url_request: str = ""   # 業務 API URL（注文・取消・照会・残高・建玉）
    url_master:  str = ""   # マスターデータ URL
    url_price:   str = ""   # 価格照会 URL
    url_event:   str = ""   # イベント通知 URL
    logged_in: bool = False
    # ログイン済み かつ sKinsyouhouMidokuFlg=0 の場合のみ True
    is_usable: bool = False
    # 禁止事項未読フラグ。True の場合は業務 API が使用できない
    kinsyouhou_midoku: bool = False


# ─── セッションマネージャー ───────────────────────────────────────────────────

class TachibanaSessionManager:
    """
    立花証券 e_api セッション管理クラス。

    シングルトンまたは共有インスタンスとして使用することを想定している。
    TachibanaBrokerAdapter のコンストラクタでインスタンス化して保持すること。

    使い方:
        session = TachibanaSessionManager(
            client=client,
            login_url=settings.TACHIBANA_BASE_URL,
            user_id=settings.TACHIBANA_USER_ID,
            password=settings.TACHIBANA_PASSWORD,
            second_password=settings.TACHIBANA_SECOND_PASSWORD,
        )
        await session.ensure_session()   # 必要時にのみログインが走る
        url = session.url_request        # 業務系仮想 URL を取得
    """

    def __init__(
        self,
        client: TachibanaClient,
        login_url: str,
        user_id: str,
        password: str,
        second_password: str,
    ) -> None:
        self._client = client
        # /auth/ を自動補完: TACHIBANA_BASE_URL がベース URL（/auth/ より上）のため
        self._login_url = _build_auth_url(login_url)
        self._user_id = user_id
        self._password = password
        self._second_password = second_password
        self._state: _SessionState = _SessionState()
        # 同時ログインを防ぐロック
        self._lock: asyncio.Lock = asyncio.Lock()

    # ─── 公開 API ──────────────────────────────────────────────────────────────

    async def ensure_session(self) -> None:
        """
        セッションが usable でない場合にログインを実行する。
        すでにログイン済み かつ usable なら何もしない。

        Raises:
            BrokerAuthError: ログイン失敗
        """
        # fast-path: ロック取得前にチェックして無駄な競合を避ける
        if self._state.is_usable:
            return

        async with self._lock:
            # ロック取得後に再チェック（二重ログイン防止）
            if self._state.is_usable:
                return
            await self._do_login()

    async def login(self) -> None:
        """
        明示的にログインを実行する（再ログイン用）。

        ensure_session と異なり、現在のセッション状態に関わらず常にログインを実行する。
        sKinsyouhouMidokuFlg=1 で取引不可になった後、ユーザーが通知を確認した場合などに使用する。

        Raises:
            BrokerAuthError: ログイン失敗
        """
        async with self._lock:
            await self._do_login()

    def invalidate(self) -> None:
        """
        セッションを強制無効化する。

        次回 ensure_session 呼び出し時に再ログインが実行される。
        BrokerAuthError を受信した adapter から呼ぶこと。
        古い仮想 URL を無効化して安全側に倒す。
        """
        logger.info("TachibanaSessionManager: セッション無効化")
        self._state = _SessionState()

    # ─── 状態プロパティ ────────────────────────────────────────────────────────

    @property
    def is_usable(self) -> bool:
        """取引可能状態かどうか（ログイン済み かつ sKinsyouhouMidokuFlg=0）"""
        return self._state.is_usable

    @property
    def kinsyouhou_midoku(self) -> bool:
        """
        禁止事項未読フラグ。

        True の場合は業務 API が使用できない。
        立花証券の Web サイトにログインして通知を確認後、login() を呼び直すこと。
        """
        return self._state.kinsyouhou_midoku

    @property
    def second_password(self) -> str:
        """第二パスワード（注文・取消リクエストで mapper に渡すために公開）"""
        return self._second_password

    # ─── 仮想 URL アクセス ─────────────────────────────────────────────────────

    @property
    def url_request(self) -> str:
        """業務 API URL (sUrlRequest) — 注文・取消・照会・残高・建玉"""
        return self._state.url_request

    @property
    def url_master(self) -> str:
        """マスターデータ URL (sUrlMaster)"""
        return self._state.url_master

    @property
    def url_price(self) -> str:
        """価格照会 URL (sUrlPrice)"""
        return self._state.url_price

    @property
    def url_event(self) -> str:
        """イベント通知 URL (sUrlEvent)"""
        return self._state.url_event

    @property
    def url_order(self) -> str:
        """
        後方互換エイリアス。仕様書確定により全業務 API が sUrlRequest を使用するため
        url_request の alias とする。新規コードは url_request を使用すること。
        """
        return self._state.url_request

    def get_url(self, index: int) -> str:
        """
        後方互換ラッパ。新規コードは名前付きプロパティを使用すること。

        index 0 → url_request, 1 → url_master, 2 → url_price, 3 → url_event
        """
        _map = {
            0: self._state.url_request,
            1: self._state.url_master,
            2: self._state.url_price,
            3: self._state.url_event,
        }
        return _map.get(index, "")

    # ─── 内部実装 ──────────────────────────────────────────────────────────────

    async def _do_login(self) -> None:
        """
        実際のログイン処理。_lock を保持した状態で呼ぶこと。

        失敗した場合はセッション状態をクリアして BrokerAuthError を再送出する。
        """
        payload = {
            "sCLMID": _CLMID_LOGIN,
            "sUserId": self._user_id,
            "sPassword": self._password,
        }
        logger.info("TachibanaSessionManager: ログイン開始 user_id=%s", self._user_id)
        try:
            data = await self._client.request(self._login_url, payload)
        except BrokerAuthError:
            # ログイン失敗時はセッション状態をクリアして安全側に倒す
            self._state = _SessionState()
            raise

        self._apply_login_response(data)
        logger.info(
            "TachibanaSessionManager: ログイン%s is_usable=%s kinsyouhou_midoku=%s",
            "成功" if self._state.is_usable else "完了（取引不可）",
            self._state.is_usable,
            self._state.kinsyouhou_midoku,
        )

    def _apply_login_response(self, data: dict) -> None:
        """
        ログインレスポンスを解析して _state を更新する。

        sKinsyouhouMidokuFlg=1 の場合は is_usable=False として記録し、
        警告ログを出力する（例外は送出しない）。
        """
        url_request = data.get("sUrlRequest", "")
        url_master  = data.get("sUrlMaster",  "")
        url_price   = data.get("sUrlPrice",   "")
        url_event   = data.get("sUrlEvent",   "")
        kinsyouhou = data.get("sKinsyouhouMidokuFlg", "0") == _KINSYOUHOU_MIDOKU_FLAG

        if kinsyouhou:
            logger.warning(
                "sKinsyouhouMidokuFlg=1: 禁止事項の未読通知があります。"
                "立花証券の Web サイトにログインして通知を確認してください。"
                "確認後に TachibanaSessionManager.login() を呼び直すと取引可能になります。"
            )

        # 新しい state を atomic に差し替える
        self._state = _SessionState(
            url_request=url_request,
            url_master=url_master,
            url_price=url_price,
            url_event=url_event,
            logged_in=True,
            is_usable=not kinsyouhou,
            kinsyouhou_midoku=kinsyouhou,
        )
