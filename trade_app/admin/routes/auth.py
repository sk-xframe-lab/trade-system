"""
認証ルーター — Google OAuth + TOTP 2FA

仕様書: 管理画面仕様書 v0.3 §3(SCR-01, SCR-02), §5(権限制御)
設計書: docs/admin/design_i4_auth_gaps.md（I-4 確定済み）

【実装状況】
(A) ✅ 実装済み — GET /auth/login, POST /auth/callback（I-4 確定）
    - Authorization Code Flow with PKCE
    - redirect_uri: フロントエンドに直接戻す
    - code exchange: バックエンドで実施（httpx）
    - セッション返却: HttpOnly Cookie（trade_admin_session）
    - 新規ユーザー作成: 事前登録必須（ui_users 未登録は 403）

(B) TODO(I-3) — TOTP セットアップ・検証（暗号化実装待ち）
    - POST /auth/totp/setup  : TotpEncryptor 実装後に解禁
    - POST /auth/totp/verify : TotpEncryptor 実装後に解禁

(C) ✅ 実装済み — フロントエンドスタック / OAuth 非依存
    - GET  /auth/me     : セッション Cookie からユーザー情報を返す
    - POST /auth/logout : セッション無効化 + Cookie クリア
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import pyotp
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.constants import AdminAuditEventType
from trade_app.admin.models.ui_session import UiSession
from trade_app.admin.models.ui_user import UiUser
from trade_app.admin.schemas.auth import (
    CurrentUserResponse,
    GoogleOAuthCallbackRequest,
    LogoutResponse,
    OAuthLoginResponse,
    OAuthLoginUrlResponse,
    TotpSetupResponse,
    TotpVerifyRequest,
    TotpVerifyResponse,
)
from trade_app.admin.services.audit_log_service import UiAuditLogService
from trade_app.admin.services.auth_guard import (
    RequireAdmin,
    RequirePreAuth,
    get_admin_db,
    hash_session_token,
)
from trade_app.admin.services.encryption import (
    ConfigurationError as EncryptionConfigError,
    DecryptionError,
    TotpEncryptor,
    UnsupportedVersionError,
)
from trade_app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Admin Auth"])

# Google OAuth エンドポイント
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Cookie 名（design_i4_auth_gaps.md §6 Q5 確定）
SESSION_COOKIE_NAME = "trade_admin_session"


def _get_client_ip(request: Request) -> str | None:
    """クライアント IP を返す（X-Forwarded-For 優先）"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _set_session_cookie(
    response: Response,
    raw_token: str,
    max_age: int,
    cookie_secure: bool,
) -> None:
    """HttpOnly Cookie にセッショントークンをセットする"""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=cookie_secure,   # COOKIE_SECURE 設定による（本番: True / 開発: False）
        samesite="lax",         # OAuth リダイレクト（外部 GET）を許容、CSRF POST は防ぐ
        path="/api/ui-admin/",
        max_age=max_age,
    )


def _clear_session_cookie(response: Response, cookie_secure: bool) -> None:
    """セッション Cookie を削除する"""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/api/ui-admin/",
        httponly=True,
        secure=cookie_secure,
        samesite="lax",
    )


# ─── (A) OAuth フロー ─────────────────────────────────────────────────────────


@router.get("/login", response_model=OAuthLoginUrlResponse)
async def get_login_url(
    code_challenge: str = Query(..., description="BASE64URL(SHA-256(code_verifier))"),
    state: str = Query(..., description="CSRF 防止用ランダム文字列（フロントが生成）"),
) -> OAuthLoginUrlResponse:
    """
    Google OAuth authorization_url を返す。

    フロントが code_challenge と state をクエリパラメータで渡す。
    バックエンドは Google authorization_url を構築して返すだけ（state の保存なし）。
    フロントはこの URL に redirect する。

    PKCE 責務:
      フロント: code_verifier 生成・sessionStorage 保存
      フロント: code_challenge = BASE64URL(SHA-256(code_verifier)) を計算
      フロント: state 生成・sessionStorage 保存
      バックエンド: URL 構築のみ（state / code_challenge は保存しない）
    """
    settings = get_settings()

    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth が設定されていません（GOOGLE_CLIENT_ID 未設定）",
        )

    params = {
        "response_type": "code",
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.OAUTH_REDIRECT_URI,
        "scope": "openid email profile",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "online",
    }
    authorization_url = f"{_GOOGLE_AUTH_URL}?{urlencode(params)}"
    return OAuthLoginUrlResponse(authorization_url=authorization_url)


@router.post("/callback", response_model=OAuthLoginResponse)
async def oauth_callback(
    request: Request,
    response: Response,
    body: GoogleOAuthCallbackRequest,
    db: AsyncSession = Depends(get_admin_db),
) -> OAuthLoginResponse:
    """
    Google OAuth コールバック処理。

    フロントが Google から受け取った code と code_verifier を送信する。
    バックエンドが code exchange を実施し、Pre-2FA セッションを発行して HttpOnly Cookie をセット。

    新規ユーザー作成ポリシー: 事前登録必須
      - ui_users に存在しないメール → 403
      - is_active=False → 403
    """
    settings = get_settings()
    audit_svc = UiAuditLogService(db)
    ip = _get_client_ip(request)
    ua = request.headers.get("User-Agent")

    # 1. Google に code exchange（code + code_verifier → access_token）
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_resp = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "code": body.code,
                    "code_verifier": body.code_verifier,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": settings.OAUTH_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.RequestError as exc:
        logger.error("Google token endpoint への接続エラー: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google 認証サーバーへの接続に失敗しました",
        )

    if token_resp.status_code != 200:
        # Google のエラーレスポンスは JSON {"error": "...", "error_description": "..."} の場合と
        # 非 JSON の場合がある。両方に対応してログを出す。
        try:
            err_json = token_resp.json()
            google_error = err_json.get("error", "unknown")
            google_error_desc = err_json.get("error_description", "")
            logger.warning(
                "Google token exchange 失敗: status=%s error=%s description=%s",
                token_resp.status_code,
                google_error,
                google_error_desc,
            )
        except Exception:
            logger.warning(
                "Google token exchange 失敗: status=%s body=%s",
                token_resp.status_code,
                token_resp.text[:200],
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google OAuth 認証に失敗しました",
        )

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google からアクセストークンが取得できませんでした",
        )

    # 2. Google userinfo endpoint でメールアドレスを取得
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            userinfo_resp = await client.get(
                _GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.RequestError as exc:
        logger.error("Google userinfo endpoint への接続エラー: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ユーザー情報の取得に失敗しました",
        )

    if userinfo_resp.status_code != 200:
        logger.warning(
            "Google userinfo 取得失敗: status=%s body=%s",
            userinfo_resp.status_code,
            userinfo_resp.text[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ユーザー情報の取得に失敗しました",
        )

    userinfo = userinfo_resp.json()
    email = userinfo.get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google アカウントからメールアドレスが取得できませんでした",
        )

    # 3. ui_users でメールアドレスを検索（事前登録必須ポリシー）
    user_result = await db.execute(
        select(UiUser).where(UiUser.email == email)
    )
    user = user_result.scalar_one_or_none()

    if user is None:
        await audit_svc.write(
            AdminAuditEventType.LOGIN_FAILURE,
            user_id=None,
            user_email=email,
            ip_address=ip,
            user_agent=ua,
            after_json={"reason": "unregistered_email"},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="このメールアドレスは登録されていません",
        )

    if not user.is_active:
        await audit_svc.write(
            AdminAuditEventType.LOGIN_FAILURE,
            user_id=user.id,
            user_email=user.email,
            ip_address=ip,
            user_agent=ua,
            after_json={"reason": "inactive_account"},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="アカウントが無効化されています",
        )

    # 4. Pre-2FA セッション発行（is_2fa_completed=False）
    raw_token = str(uuid.uuid4())
    token_hash = hash_session_token(raw_token)
    now = datetime.now(timezone.utc)
    session = UiSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        session_token_hash=token_hash,
        ip_address=ip,
        user_agent=ua,
        is_2fa_completed=False,
        expires_at=now + timedelta(seconds=settings.PRE_2FA_SESSION_TTL_SEC),
    )
    db.add(session)

    # 5. last_login_at 更新
    user.last_login_at = now

    # 6. LOGIN_SUCCESS 監査ログ記録
    await audit_svc.write(
        AdminAuditEventType.LOGIN_SUCCESS,
        user_id=user.id,
        user_email=user.email,
        ip_address=ip,
        user_agent=ua,
    )

    await db.commit()
    logger.info(
        "OAuth ログイン成功: user=%s session=%s (Pre-2FA)",
        user.email,
        session.id[:8],
    )

    # 7. HttpOnly Cookie にセッショントークンをセット
    _set_session_cookie(
        response,
        raw_token=raw_token,
        max_age=settings.PRE_2FA_SESSION_TTL_SEC,
        cookie_secure=settings.COOKIE_SECURE,
    )

    return OAuthLoginResponse(
        session_id=session.id,
        requires_2fa=True,
        user_email=user.email,
        user_display_name=user.display_name,
    )


# ─── (B) TOTP ────────────────────────────────────────────────────────────────


@router.post("/totp/setup", response_model=TotpSetupResponse)
async def setup_totp(
    request: Request,
    response: Response,
    pre_auth_user: RequirePreAuth,
    db: AsyncSession = Depends(get_admin_db),
) -> TotpSetupResponse:
    """
    TOTP シークレット生成・QR コード URI 返却。

    Pre-2FA セッション（is_2fa_completed=False）でも呼び出し可能。
    既に totp_secret_encrypted がある場合は上書きする（再セットアップ対応）。
    totp_enabled は verify 成功後に True になる（setup 時点では変更しない）。

    戻り値の totp_uri はフロントで QR コードを生成して表示すること。
    この URI は再取得不可のため、表示後は破棄する。
    """
    settings = get_settings()

    try:
        encryptor = TotpEncryptor.from_settings(settings)
    except EncryptionConfigError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TOTP 暗号化が設定されていません（TOTP_ENCRYPTION_KEY 未設定）",
        )

    user_result = await db.execute(select(UiUser).where(UiUser.id == pre_auth_user.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ユーザーが見つかりません")

    # TOTP シークレット生成（Base32 エンコード済み）
    totp_secret = pyotp.random_base32()

    # 暗号化して DB に保存（再セットアップ時は上書き）
    encrypted = encryptor.encrypt(totp_secret)
    user.totp_secret_encrypted = encrypted
    # totp_enabled は verify 成功後に True にする（setup 時点では変更しない）

    # QR コード用 otpauth:// URI 生成（issuer は設定値を使用）
    totp = pyotp.TOTP(totp_secret)
    totp_uri = totp.provisioning_uri(name=user.email, issuer_name=settings.TOTP_ISSUER)

    await db.commit()
    logger.info("TOTP setup 完了: user=%s", user.email)

    return TotpSetupResponse(totp_uri=totp_uri, backup_codes=[])


@router.post("/totp/verify", response_model=TotpVerifyResponse)
async def verify_totp(
    request: Request,
    response: Response,
    body: TotpVerifyRequest,
    db: AsyncSession = Depends(get_admin_db),
) -> TotpVerifyResponse:
    """
    TOTP コード検証。成功時にセッションを 2FA 完了に昇格して Cookie の max_age を延長する。

    設計:
    - body.session_id と Cookie のトークンの両方が一致することを確認（二重検証）
    - is_2fa_completed=True への更新は同一セッションへの UPSERT（新規セッション発行なし）
    - Cookie max_age を PRE_2FA_SESSION_TTL_SEC → SESSION_TTL_SEC（8時間）に延長
    """
    settings = get_settings()
    ip = _get_client_ip(request)
    ua = request.headers.get("User-Agent")

    # Cookie からトークンを読み取り（session_id と Cookie の二重検証）
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証が必要です。ログインしてください。",
        )

    token_hash = hash_session_token(raw_token)

    # Pre-2FA セッションを session_id で直接 SELECT（RequireAdmin は使わない）
    session_result = await db.execute(
        select(UiSession)
        .where(UiSession.id == body.session_id)
        .where(UiSession.invalidated_at.is_(None))
    )
    session = session_result.scalar_one_or_none()

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="セッションが見つかりません。再ログインしてください。",
        )

    # Cookie のトークンが session_id のセッションと一致することを確認
    if session.session_token_hash != token_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="セッション情報が一致しません。再ログインしてください。",
        )

    # 期限切れチェック
    now = datetime.now(timezone.utc)
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="セッションが期限切れです。再ログインしてください。",
        )

    # ユーザー取得
    user_result = await db.execute(select(UiUser).where(UiUser.id == session.user_id))
    user = user_result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ユーザーアカウントが無効です。",
        )

    # TOTP シークレット取得・復号
    if not user.totp_secret_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TOTP が設定されていません。先に /auth/totp/setup を実行してください。",
        )

    try:
        encryptor = TotpEncryptor.from_settings(settings)
        totp_secret = encryptor.decrypt(user.totp_secret_encrypted)
    except EncryptionConfigError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TOTP 暗号化が設定されていません（TOTP_ENCRYPTION_KEY 未設定）",
        )
    except (DecryptionError, UnsupportedVersionError):
        logger.error("TOTP 復号失敗: user_id=%s", user.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TOTP 検証中にエラーが発生しました。管理者に連絡してください。",
        )

    # TOTP コード検証（valid_window=1 で前後 30 秒の時刻ずれを許容）
    totp = pyotp.TOTP(totp_secret)
    audit_svc = UiAuditLogService(db)

    if not totp.verify(body.totp_code, valid_window=1):
        await audit_svc.write(
            AdminAuditEventType.TWO_FA_FAILURE,
            user_id=user.id,
            user_email=user.email,
            ip_address=ip,
            user_agent=ua,
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="TOTP コードが正しくありません。",
        )

    # 2FA 成功: セッション昇格（同一セッションを更新）
    new_expires_at = now + timedelta(seconds=settings.SESSION_TTL_SEC)
    session.is_2fa_completed = True
    session.expires_at = new_expires_at

    # totp_enabled フラグを True に（初回 verify 完了を記録）
    user.totp_enabled = True

    # TWO_FA_SUCCESS 監査ログ
    await audit_svc.write(
        AdminAuditEventType.TWO_FA_SUCCESS,
        user_id=user.id,
        user_email=user.email,
        ip_address=ip,
        user_agent=ua,
    )

    await db.commit()

    # Cookie の max_age を SESSION_TTL_SEC（8時間）に延長（同一トークンを再セット）
    _set_session_cookie(
        response,
        raw_token=raw_token,
        max_age=settings.SESSION_TTL_SEC,
        cookie_secure=settings.COOKIE_SECURE,
    )

    logger.info("TOTP 認証成功: user=%s session=%s", user.email, session.id[:8])

    return TotpVerifyResponse(
        user_id=user.id,
        user_email=user.email,
        role=user.role,
        expires_at=new_expires_at,
    )


# ─── (C) 実装済み ────────────────────────────────────────────────────────────


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> LogoutResponse:
    """
    ログアウト — 現在のセッションを無効化して Cookie を削除する。

    UiSession.invalidated_at を設定して Cookie をクリアする。
    以降のリクエストは 401 になる。
    """
    result = await db.execute(
        select(UiSession).where(UiSession.id == current_user.session_id)
    )
    session = result.scalar_one_or_none()
    if session and session.invalidated_at is None:
        session.invalidated_at = datetime.now(timezone.utc)

    # 監査ログ記録
    ip = _get_client_ip(request)
    audit_svc = UiAuditLogService(db)
    await audit_svc.write(
        AdminAuditEventType.LOGOUT,
        user_id=current_user.user_id,
        user_email=current_user.email,
        resource_type="ui_session",
        resource_id=current_user.session_id,
        ip_address=ip,
        user_agent=request.headers.get("User-Agent"),
    )
    await db.commit()

    # HttpOnly Cookie をクリア
    settings = get_settings()
    _clear_session_cookie(response, cookie_secure=settings.COOKIE_SECURE)

    logger.info(
        "ログアウト: user=%s session=%s",
        current_user.email,
        current_user.session_id[:8],
    )
    return LogoutResponse(message="ログアウトしました")


@router.get("/me", response_model=CurrentUserResponse)
async def get_me(
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_admin_db),
) -> CurrentUserResponse:
    """現在の認証済みユーザー情報を返す（totp_enabled / last_login_at を含む）"""
    result = await db.execute(
        select(UiUser).where(UiUser.id == current_user.user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ユーザーが見つかりません",
        )
    return CurrentUserResponse(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        totp_enabled=user.totp_enabled,
        last_login_at=user.last_login_at,
    )
