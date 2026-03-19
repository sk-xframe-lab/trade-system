"""
認証スキーマ (SCR-01 / SCR-02)

仕様書: 管理画面仕様書 v0.3 §2(SCR-01, SCR-02)
設計書: docs/admin/design_i4_auth_gaps.md（I-4 確定済み）

【確定事項】
- OAuth フロー: Authorization Code Flow with PKCE
- セッショントークン返却: HttpOnly Cookie（trade_admin_session）
- PKCE: code_verifier はフロント生成・保持、code_challenge はフロントが計算
- state パラメータ: フロントが生成・検証（CSRF 防止）
"""
from datetime import datetime
from pydantic import BaseModel, Field


# ─── GET /auth/login レスポンス ──────────────────────────────────────────────


class OAuthLoginUrlResponse(BaseModel):
    """Google OAuth authorization_url を返す。フロントがこの URL にリダイレクトする。"""
    authorization_url: str


# ─── POST /auth/callback リクエスト ─────────────────────────────────────────


class GoogleOAuthCallbackRequest(BaseModel):
    """
    Google OAuth コールバック処理リクエスト。

    フロントエンドが Google からコールバックを受け取った後、
    code と code_verifier をバックエンドに送信して code exchange を依頼する。

    フロー:
      1. フロントが code_verifier を生成（sessionStorage 保存）
      2. フロントが code_challenge = BASE64URL(SHA-256(code_verifier)) を計算
      3. フロントが GET /auth/login?code_challenge=xxx&state=yyy を呼ぶ
      4. フロントが返ってきた authorization_url に redirect
      5. Google がフロントの redirect_uri に ?code=xxx&state=yyy で戻る
      6. フロントが state を sessionStorage と照合（CSRF 検証）
      7. フロントがこの body で POST /auth/callback を呼ぶ
    """
    code: str = Field(..., description="Google OAuth 認可コード")
    code_verifier: str = Field(..., description="フロントが生成した PKCE code_verifier")
    state: str = Field(..., description="CSRF 防止用 state（フロントが生成・検証済み）")


# ─── POST /auth/callback レスポンス ─────────────────────────────────────────


class OAuthLoginResponse(BaseModel):
    """
    OAuth ログイン成功後のレスポンス。

    セッショントークンは HttpOnly Cookie (trade_admin_session) で返す。
    session_id は frontend でのデバッグ・ログ表示用のみ（機密ではない）。
    """
    session_id: str
    requires_2fa: bool = True
    user_email: str
    user_display_name: str | None = None


# ─── 2FA ───────────────────────────────────────────────────────────────────


class TotpVerifyRequest(BaseModel):
    """TOTP 認証コード検証リクエスト"""
    session_id: str = Field(..., description="OAuth ログイン後に発行されたセッションID")
    totp_code: str = Field(..., min_length=6, max_length=6, description="6桁のTOTPコード")


class TotpVerifyResponse(BaseModel):
    """
    TOTP 認証成功後のレスポンス。

    セッショントークンは HttpOnly Cookie（trade_admin_session）で返す。
    Cookie の max_age は SESSION_TTL_SEC（8時間）に延長される。
    session_token はレスポンス body には含めない（Cookie ベース設計）。
    """
    user_id: str
    user_email: str
    role: str
    expires_at: datetime


# ─── ログアウト ────────────────────────────────────────────────────────────


class LogoutResponse(BaseModel):
    message: str = "ログアウトしました"


# ─── 現在ユーザー情報 ──────────────────────────────────────────────────────


class CurrentUserResponse(BaseModel):
    """認証済みユーザーの情報"""
    user_id: str
    email: str
    display_name: str | None
    role: str
    totp_enabled: bool
    last_login_at: datetime | None

    model_config = {"from_attributes": True}


# ─── TOTP セットアップ（SCR-13 ユーザー設定） ────────────────────────────────


class TotpSetupResponse(BaseModel):
    """
    TOTP 新規設定時のレスポンス。
    秘密鍵を QR コード URI 形式で返す。表示後は再取得できない。
    TODO(I-3): 暗号化方式確定後に秘密鍵の保存実装を追加すること。
    """
    totp_uri: str = Field(..., description="otpauth:// 形式の URI（QRコード生成用）")
    backup_codes: list[str] = Field(
        default_factory=list,
        description="バックアップコード（紛失時用）。この時点のみ表示される。"
    )
