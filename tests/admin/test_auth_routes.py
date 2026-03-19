"""
認証ルーター・auth_guard のテスト

【検証内容】
- hash_session_token: SHA-256 ハッシュが決定論的に生成される
- logout: セッション invalidated_at が設定され、監査ログが記録される
- logout: 既に無効化済みのセッションは二重無効化しない
- logout: Cookie がクリアされる
- get_me: UiUser の totp_enabled / last_login_at を含む全フィールドを返す
- get_me: ユーザーが DB に存在しない場合は 404
- get_login_url: code_challenge + state を受け取り authorization_url を返す
- get_login_url: GOOGLE_CLIENT_ID 未設定時は 503
- get_login_url: code_challenge / state 欠落時は 422
- oauth_callback: 未登録メールは 403 + LOGIN_FAILURE ログ
- oauth_callback: is_active=False は 403 + LOGIN_FAILURE ログ
- oauth_callback: 正常時は Pre-2FA セッション発行 + Cookie セット + LOGIN_SUCCESS ログ
- oauth_callback: Google token endpoint エラーは 400
- auth_guard Cookie 読み取り: 有効な Cookie でパス
- auth_guard: Cookie なしは 401
- auth_guard: 無効なトークンは 401
- auth_guard: 期限切れセッションは 401 + SESSION_EXPIRED_ACCESS ログ
- auth_guard: is_2fa_completed=False は 401
- setup_totp: 正常時は totp_uri を返し DB に encrypted secret を保存
- setup_totp: 暗号化設定なし → 503
- setup_totp: Pre-2FA セッションで呼び出し可能
- verify_totp: 正常時はセッション is_2fa_completed=True + Cookie 延長 + TWO_FA_SUCCESS ログ
- verify_totp: 不正コード → 401 + TWO_FA_FAILURE ログ
- verify_totp: セッション未存在 → 401
- verify_totp: Cookie と session_id 不一致 → 401
"""
import base64
import hashlib
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Response

from trade_app.admin.models.ui_session import UiSession
from trade_app.admin.models.ui_user import UiUser
from trade_app.admin.services.auth_guard import (
    AdminUser,
    SESSION_COOKIE_NAME,
    get_current_admin_user,
    get_pre2fa_user,
    hash_session_token,
)
from trade_app.admin.routes.auth import (
    get_login_url,
    get_me,
    logout,
    oauth_callback,
    setup_totp,
    verify_totp,
)
from trade_app.admin.schemas.auth import GoogleOAuthCallbackRequest, TotpVerifyRequest
from trade_app.admin.schemas.audit_log import AuditLogFilter
from trade_app.admin.services.audit_log_service import UiAuditLogService
from trade_app.admin.constants import AdminAuditEventType

# テスト用 TOTP 暗号化鍵（32 バイト固定）
_TEST_KEY_BYTES = b"test_key_32bytes_exactly_padding"
_TEST_KEY_B64 = base64.b64encode(_TEST_KEY_BYTES).decode()


# ─── テストヘルパー ────────────────────────────────────────────────────────────


def _make_user(db_session, email="admin@example.com", **kwargs) -> UiUser:
    kwargs.setdefault("totp_enabled", False)
    kwargs.setdefault("is_active", True)
    user = UiUser(
        id=str(uuid.uuid4()),
        email=email,
        display_name="テスト管理者",
        role="admin",
        **kwargs,
    )
    db_session.add(user)
    return user


def _make_session(
    db_session,
    user_id: str,
    token: str = "raw_token",
    is_2fa_completed: bool = True,
    expires_delta: timedelta = timedelta(hours=8),
) -> UiSession:
    token_hash = hash_session_token(token)
    session = UiSession(
        id=str(uuid.uuid4()),
        user_id=user_id,
        session_token_hash=token_hash,
        ip_address="127.0.0.1",
        user_agent="pytest",
        is_2fa_completed=is_2fa_completed,
        expires_at=datetime.now(timezone.utc) + expires_delta,
    )
    db_session.add(session)
    return session


def _make_current_user(user_id: str, session_id: str, email: str = "admin@example.com") -> AdminUser:
    return AdminUser(
        user_id=user_id,
        email=email,
        display_name="Admin",
        role="admin",
        session_id=session_id,
    )


def _make_request(cookies: dict | None = None, ip: str = "127.0.0.1") -> MagicMock:
    request = MagicMock()
    request.cookies = cookies or {}
    request.headers = {}
    request.client = MagicMock()
    request.client.host = ip
    return request


# ─── TestHashSessionToken ──────────────────────────────────────────────────────


class TestHashSessionToken:
    def test_deterministic(self):
        token = "my_secret_token"
        assert hash_session_token(token) == hash_session_token(token)

    def test_sha256_length(self):
        result = hash_session_token("any_token")
        assert len(result) == 64  # SHA-256 hex digest

    def test_expected_value(self):
        raw = "test_token"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert hash_session_token(raw) == expected

    def test_different_tokens_differ(self):
        assert hash_session_token("token_a") != hash_session_token("token_b")


# ─── TestLogout ───────────────────────────────────────────────────────────────


class TestLogout:
    @pytest.mark.asyncio
    async def test_logout_invalidates_session(self, db_session):
        """logout はセッションの invalidated_at を設定する"""
        user = _make_user(db_session)
        session = _make_session(db_session, user.id)
        await db_session.flush()

        current_user = _make_current_user(user.id, session.id)
        request = _make_request()
        response = Response()

        with patch("trade_app.admin.routes.auth.get_settings") as mock_settings:
            mock_settings.return_value.COOKIE_SECURE = False
            result = await logout(request, response, current_user, db_session)

        assert result.message == "ログアウトしました"
        from sqlalchemy import select
        row = (await db_session.execute(
            select(UiSession).where(UiSession.id == session.id)
        )).scalar_one_or_none()
        assert row is not None
        assert row.invalidated_at is not None

    @pytest.mark.asyncio
    async def test_logout_records_audit_log(self, db_session):
        """logout は LOGOUT 監査ログを記録する"""
        user = _make_user(db_session)
        session = _make_session(db_session, user.id)
        await db_session.flush()

        current_user = _make_current_user(user.id, session.id)
        request = _make_request(ip="10.0.0.1")

        with patch("trade_app.admin.routes.auth.get_settings") as mock_settings:
            mock_settings.return_value.COOKIE_SECURE = False
            await logout(request, Response(), current_user, db_session)

        audit_svc = UiAuditLogService(db_session)
        logs, total = await audit_svc.query(
            AuditLogFilter(event_type=AdminAuditEventType.LOGOUT)
        )
        assert total == 1
        assert logs[0].user_email == "admin@example.com"

    @pytest.mark.asyncio
    async def test_logout_already_invalidated_is_noop(self, db_session):
        """既に無効化済みのセッションを二重無効化しない"""
        user = _make_user(db_session)
        session = _make_session(db_session, user.id)
        earlier = datetime.now(timezone.utc) - timedelta(hours=1)
        session.invalidated_at = earlier
        await db_session.flush()

        current_user = _make_current_user(user.id, session.id)

        with patch("trade_app.admin.routes.auth.get_settings") as mock_settings:
            mock_settings.return_value.COOKIE_SECURE = False
            await logout(_make_request(), Response(), current_user, db_session)

        from sqlalchemy import select
        row = (await db_session.execute(
            select(UiSession).where(UiSession.id == session.id)
        )).scalar_one_or_none()
        assert row.invalidated_at == earlier

    @pytest.mark.asyncio
    async def test_logout_clears_cookie(self, db_session):
        """logout は Cookie クリアの Set-Cookie ヘッダーをセットする"""
        user = _make_user(db_session)
        session = _make_session(db_session, user.id)
        await db_session.flush()

        current_user = _make_current_user(user.id, session.id)
        response = Response()

        with patch("trade_app.admin.routes.auth.get_settings") as mock_settings:
            mock_settings.return_value.COOKIE_SECURE = False
            await logout(_make_request(), response, current_user, db_session)

        # Cookie が削除された（Max-Age=0 または Set-Cookie で value が空になる）
        cookie_header = response.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in cookie_header


# ─── TestGetMe ────────────────────────────────────────────────────────────────


class TestGetMe:
    @pytest.mark.asyncio
    async def test_get_me_returns_full_user(self, db_session):
        """get_me は totp_enabled / last_login_at を含む全フィールドを返す"""
        last_login = datetime(2026, 3, 18, 9, 0, 0, tzinfo=timezone.utc)
        user = _make_user(db_session, totp_enabled=True, last_login_at=last_login)
        await db_session.flush()

        current_user = _make_current_user(user.id, str(uuid.uuid4()))
        response = await get_me(current_user, db_session)

        assert response.user_id == user.id
        assert response.email == "admin@example.com"
        assert response.totp_enabled is True
        assert response.last_login_at == last_login

    @pytest.mark.asyncio
    async def test_get_me_totp_disabled(self, db_session):
        user = _make_user(db_session, totp_enabled=False)
        await db_session.flush()

        current_user = _make_current_user(user.id, str(uuid.uuid4()))
        response = await get_me(current_user, db_session)

        assert response.totp_enabled is False
        assert response.last_login_at is None

    @pytest.mark.asyncio
    async def test_get_me_user_not_found_raises_404(self, db_session):
        """存在しないユーザーIDは 404"""
        from fastapi import HTTPException
        current_user = _make_current_user(str(uuid.uuid4()), str(uuid.uuid4()))

        with pytest.raises(HTTPException) as exc_info:
            await get_me(current_user, db_session)
        assert exc_info.value.status_code == 404


# ─── TestGetLoginUrl ──────────────────────────────────────────────────────────


class TestGetLoginUrl:
    @pytest.mark.asyncio
    async def test_returns_authorization_url(self):
        """code_challenge と state を含む Google authorization_url を返す"""
        with patch("trade_app.admin.routes.auth.get_settings") as mock_settings:
            mock_settings.return_value.GOOGLE_CLIENT_ID = "test-client-id"
            mock_settings.return_value.OAUTH_REDIRECT_URI = "http://localhost:5173/auth/callback"

            result = await get_login_url(
                code_challenge="test_challenge_abc123",
                state="random_state_xyz",
            )

        assert result.authorization_url.startswith(
            "https://accounts.google.com/o/oauth2/v2/auth?"
        )
        assert "code_challenge=test_challenge_abc123" in result.authorization_url
        assert "state=random_state_xyz" in result.authorization_url
        assert "code_challenge_method=S256" in result.authorization_url
        assert "response_type=code" in result.authorization_url
        assert "client_id=test-client-id" in result.authorization_url

    @pytest.mark.asyncio
    async def test_returns_503_when_client_id_not_set(self):
        """GOOGLE_CLIENT_ID が未設定時は 503"""
        from fastapi import HTTPException
        with patch("trade_app.admin.routes.auth.get_settings") as mock_settings:
            mock_settings.return_value.GOOGLE_CLIENT_ID = ""

            with pytest.raises(HTTPException) as exc_info:
                await get_login_url(code_challenge="x", state="y")

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_authorization_url_includes_openid_scope(self):
        """authorization_url に openid email profile スコープが含まれる"""
        with patch("trade_app.admin.routes.auth.get_settings") as mock_settings:
            mock_settings.return_value.GOOGLE_CLIENT_ID = "client-id"
            mock_settings.return_value.OAUTH_REDIRECT_URI = "http://localhost/callback"

            result = await get_login_url(code_challenge="ch", state="st")

        assert "scope=openid" in result.authorization_url

    @pytest.mark.asyncio
    async def test_redirect_uri_included_in_url(self):
        """authorization_url に redirect_uri が含まれる"""
        with patch("trade_app.admin.routes.auth.get_settings") as mock_settings:
            mock_settings.return_value.GOOGLE_CLIENT_ID = "client-id"
            mock_settings.return_value.OAUTH_REDIRECT_URI = "http://localhost:5173/auth/callback"

            result = await get_login_url(code_challenge="ch", state="st")

        assert "redirect_uri=" in result.authorization_url


# ─── TestOAuthCallback ────────────────────────────────────────────────────────


def _mock_httpx_success(email: str = "admin@example.com"):
    """Google token + userinfo の成功レスポンスをモックする"""
    token_response = MagicMock()
    token_response.status_code = 200
    token_response.json.return_value = {"access_token": "fake_access_token"}

    userinfo_response = MagicMock()
    userinfo_response.status_code = 200
    userinfo_response.json.return_value = {
        "email": email,
        "name": "Test Admin",
    }

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=token_response)
    mock_client.get = AsyncMock(return_value=userinfo_response)
    return mock_client


class TestOAuthCallback:
    def _make_callback_body(self) -> GoogleOAuthCallbackRequest:
        return GoogleOAuthCallbackRequest(
            code="auth_code_from_google",
            code_verifier="verifier_that_frontend_generated",
            state="state_that_frontend_generated",
        )

    def _make_settings_mock(self, **kwargs):
        m = MagicMock()
        m.GOOGLE_CLIENT_ID = kwargs.get("GOOGLE_CLIENT_ID", "client-id")
        m.GOOGLE_CLIENT_SECRET = kwargs.get("GOOGLE_CLIENT_SECRET", "client-secret")
        m.OAUTH_REDIRECT_URI = kwargs.get("OAUTH_REDIRECT_URI", "http://localhost/callback")
        m.PRE_2FA_SESSION_TTL_SEC = kwargs.get("PRE_2FA_SESSION_TTL_SEC", 600)
        m.COOKIE_SECURE = kwargs.get("COOKIE_SECURE", False)  # テスト環境は HTTP
        return m

    @pytest.mark.asyncio
    async def test_callback_unregistered_email_returns_403(self, db_session):
        """ui_users に存在しないメールは 403 + LOGIN_FAILURE 監査ログ"""
        from fastapi import HTTPException

        mock_client = _mock_httpx_success(email="unknown@example.com")

        with patch("trade_app.admin.routes.auth.httpx.AsyncClient", return_value=mock_client):
            with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_settings_mock()):
                with pytest.raises(HTTPException) as exc_info:
                    await oauth_callback(
                        _make_request(), Response(), self._make_callback_body(), db_session
                    )

        assert exc_info.value.status_code == 403
        assert "登録されていません" in exc_info.value.detail

        audit_svc = UiAuditLogService(db_session)
        logs, total = await audit_svc.query(
            AuditLogFilter(event_type=AdminAuditEventType.LOGIN_FAILURE)
        )
        assert total == 1
        assert logs[0].user_email == "unknown@example.com"

    @pytest.mark.asyncio
    async def test_callback_inactive_user_returns_403(self, db_session):
        """is_active=False のユーザーは 403 + LOGIN_FAILURE 監査ログ"""
        from fastapi import HTTPException

        user = _make_user(db_session, email="inactive@example.com", is_active=False)
        await db_session.flush()

        mock_client = _mock_httpx_success(email="inactive@example.com")

        with patch("trade_app.admin.routes.auth.httpx.AsyncClient", return_value=mock_client):
            with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_settings_mock()):
                with pytest.raises(HTTPException) as exc_info:
                    await oauth_callback(
                        _make_request(), Response(), self._make_callback_body(), db_session
                    )

        assert exc_info.value.status_code == 403
        assert "無効化" in exc_info.value.detail

        audit_svc = UiAuditLogService(db_session)
        logs, total = await audit_svc.query(
            AuditLogFilter(event_type=AdminAuditEventType.LOGIN_FAILURE)
        )
        assert total == 1

    @pytest.mark.asyncio
    async def test_callback_success_creates_pre2fa_session(self, db_session):
        """正常コールバック: Pre-2FA セッションが発行される"""
        from sqlalchemy import select
        user = _make_user(db_session, email="admin@example.com")
        await db_session.flush()

        mock_client = _mock_httpx_success(email="admin@example.com")
        response = Response()

        with patch("trade_app.admin.routes.auth.httpx.AsyncClient", return_value=mock_client):
            with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_settings_mock()):
                result = await oauth_callback(
                    _make_request(), response, self._make_callback_body(), db_session
                )

        # レスポンスチェック
        assert result.requires_2fa is True
        assert result.user_email == "admin@example.com"

        # セッションが作成された
        rows = (await db_session.execute(
            select(UiSession).where(UiSession.user_id == user.id)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].is_2fa_completed is False

    @pytest.mark.asyncio
    async def test_callback_success_sets_cookie(self, db_session):
        """正常コールバック: HttpOnly Cookie が Set-Cookie ヘッダーにセットされる"""
        _make_user(db_session, email="admin@example.com")
        await db_session.flush()

        mock_client = _mock_httpx_success(email="admin@example.com")
        response = Response()

        with patch("trade_app.admin.routes.auth.httpx.AsyncClient", return_value=mock_client):
            with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_settings_mock()):
                await oauth_callback(
                    _make_request(), response, self._make_callback_body(), db_session
                )

        cookie_header = response.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in cookie_header
        assert "HttpOnly" in cookie_header
        assert "SameSite=lax" in cookie_header

    @pytest.mark.asyncio
    async def test_callback_success_records_login_success_audit(self, db_session):
        """正常コールバック: LOGIN_SUCCESS 監査ログが記録される"""
        _make_user(db_session, email="admin@example.com")
        await db_session.flush()

        mock_client = _mock_httpx_success(email="admin@example.com")

        with patch("trade_app.admin.routes.auth.httpx.AsyncClient", return_value=mock_client):
            with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_settings_mock()):
                await oauth_callback(
                    _make_request(), Response(), self._make_callback_body(), db_session
                )

        audit_svc = UiAuditLogService(db_session)
        logs, total = await audit_svc.query(
            AuditLogFilter(event_type=AdminAuditEventType.LOGIN_SUCCESS)
        )
        assert total == 1
        assert logs[0].user_email == "admin@example.com"

    @pytest.mark.asyncio
    async def test_callback_success_updates_last_login_at(self, db_session):
        """正常コールバック: last_login_at が更新される"""
        from sqlalchemy import select
        user = _make_user(db_session, email="admin@example.com")
        await db_session.flush()

        mock_client = _mock_httpx_success(email="admin@example.com")

        with patch("trade_app.admin.routes.auth.httpx.AsyncClient", return_value=mock_client):
            with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_settings_mock()):
                await oauth_callback(
                    _make_request(), Response(), self._make_callback_body(), db_session
                )

        row = (await db_session.execute(
            select(UiUser).where(UiUser.id == user.id)
        )).scalar_one()
        assert row.last_login_at is not None

    @pytest.mark.asyncio
    async def test_callback_google_token_error_returns_400(self, db_session):
        """Google token endpoint がエラーを返す場合は 400"""
        from fastapi import HTTPException

        error_response = MagicMock()
        error_response.status_code = 400
        error_response.text = "invalid_grant"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=error_response)

        with patch("trade_app.admin.routes.auth.httpx.AsyncClient", return_value=mock_client):
            with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_settings_mock()):
                with pytest.raises(HTTPException) as exc_info:
                    await oauth_callback(
                        _make_request(), Response(), self._make_callback_body(), db_session
                    )

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_callback_google_network_error_returns_502(self, db_session):
        """Google への接続失敗は 502"""
        from fastapi import HTTPException
        import httpx as _httpx

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=_httpx.ConnectError("connection failed"))

        with patch("trade_app.admin.routes.auth.httpx.AsyncClient", return_value=mock_client):
            with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_settings_mock()):
                with pytest.raises(HTTPException) as exc_info:
                    await oauth_callback(
                        _make_request(), Response(), self._make_callback_body(), db_session
                    )

        assert exc_info.value.status_code == 502


# ─── TestAuthGuardCookie ──────────────────────────────────────────────────────


class TestAuthGuardCookie:
    @pytest.mark.asyncio
    async def test_valid_cookie_session_passes(self, db_session):
        """有効な Cookie セッションで認証ガードをパスする"""
        user = _make_user(db_session)
        token = "valid_raw_token"
        _make_session(db_session, user.id, token=token, is_2fa_completed=True)
        await db_session.flush()

        request = _make_request(cookies={SESSION_COOKIE_NAME: token})
        result = await get_current_admin_user(request, db_session)

        assert result.email == "admin@example.com"
        assert result.user_id == user.id

    @pytest.mark.asyncio
    async def test_no_cookie_returns_401(self, db_session):
        """Cookie なし → 401"""
        from fastapi import HTTPException
        request = _make_request(cookies={})

        with pytest.raises(HTTPException) as exc_info:
            await get_current_admin_user(request, db_session)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self, db_session):
        """存在しないトークン → 401"""
        from fastapi import HTTPException
        request = _make_request(cookies={SESSION_COOKIE_NAME: "nonexistent_token"})

        with pytest.raises(HTTPException) as exc_info:
            await get_current_admin_user(request, db_session)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_session_returns_401(self, db_session):
        """期限切れセッション → 401"""
        from fastapi import HTTPException
        user = _make_user(db_session)
        token = "expired_token"
        _make_session(db_session, user.id, token=token, expires_delta=timedelta(hours=-1))
        await db_session.flush()

        request = _make_request(cookies={SESSION_COOKIE_NAME: token})

        with pytest.raises(HTTPException) as exc_info:
            await get_current_admin_user(request, db_session)

        assert exc_info.value.status_code == 401
        assert "期限切れ" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_expired_session_logs_session_expired_access(self, db_session):
        """期限切れセッションアクセス → SESSION_EXPIRED_ACCESS 監査ログが記録される"""
        from fastapi import HTTPException
        user = _make_user(db_session)
        token = "expired_token_for_audit"
        _make_session(db_session, user.id, token=token, expires_delta=timedelta(hours=-1))
        await db_session.flush()

        request = _make_request(cookies={SESSION_COOKIE_NAME: token})

        with pytest.raises(HTTPException):
            await get_current_admin_user(request, db_session)

        audit_svc = UiAuditLogService(db_session)
        logs, total = await audit_svc.query(
            AuditLogFilter(event_type=AdminAuditEventType.SESSION_EXPIRED_ACCESS)
        )
        assert total == 1
        assert logs[0].user_id == user.id

    @pytest.mark.asyncio
    async def test_pre2fa_session_returns_401(self, db_session):
        """is_2fa_completed=False のセッション → 401"""
        from fastapi import HTTPException
        user = _make_user(db_session)
        token = "pre2fa_token"
        _make_session(db_session, user.id, token=token, is_2fa_completed=False)
        await db_session.flush()

        request = _make_request(cookies={SESSION_COOKIE_NAME: token})

        with pytest.raises(HTTPException) as exc_info:
            await get_current_admin_user(request, db_session)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalidated_session_returns_401(self, db_session):
        """invalidated_at が設定済みのセッション → 401"""
        from fastapi import HTTPException
        user = _make_user(db_session)
        token = "invalidated_token"
        session = _make_session(db_session, user.id, token=token)
        session.invalidated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await db_session.flush()

        request = _make_request(cookies={SESSION_COOKIE_NAME: token})

        with pytest.raises(HTTPException) as exc_info:
            await get_current_admin_user(request, db_session)

        assert exc_info.value.status_code == 401


# ─── TestPreAuth ─────────────────────────────────────────────────────────────


class TestPreAuth:
    @pytest.mark.asyncio
    async def test_pre2fa_session_passes_get_pre2fa_user(self, db_session):
        """is_2fa_completed=False のセッションでも get_pre2fa_user はパスする"""
        user = _make_user(db_session)
        token = "pre2fa_token_for_setup"
        _make_session(db_session, user.id, token=token, is_2fa_completed=False)
        await db_session.flush()

        request = _make_request(cookies={SESSION_COOKIE_NAME: token})
        result = await get_pre2fa_user(request, db_session)

        assert result.user_id == user.id
        assert result.email == "admin@example.com"

    @pytest.mark.asyncio
    async def test_no_cookie_returns_401(self, db_session):
        """Cookie なし → 401"""
        from fastapi import HTTPException
        request = _make_request(cookies={})

        with pytest.raises(HTTPException) as exc_info:
            await get_pre2fa_user(request, db_session)

        assert exc_info.value.status_code == 401


# ─── TestTotpSetup ────────────────────────────────────────────────────────────


def _make_totp_settings_mock(**kwargs):
    m = MagicMock()
    m.TOTP_ENCRYPTION_KEY = kwargs.get("TOTP_ENCRYPTION_KEY", _TEST_KEY_B64)
    m.TOTP_ISSUER = kwargs.get("TOTP_ISSUER", "TradeSystem Admin")
    m.COOKIE_SECURE = kwargs.get("COOKIE_SECURE", False)
    return m


class TestTotpSetup:
    @pytest.mark.asyncio
    async def test_setup_returns_totp_uri(self, db_session):
        """正常時は otpauth:// URI が返る"""
        user = _make_user(db_session)
        await db_session.flush()

        pre_auth_user = _make_current_user(user.id, str(uuid.uuid4()))
        request = _make_request()
        response = Response()

        with patch("trade_app.admin.routes.auth.get_settings", return_value=_make_totp_settings_mock()):
            result = await setup_totp(request, response, pre_auth_user, db_session)

        assert result.totp_uri.startswith("otpauth://totp/")
        assert "admin%40example.com" in result.totp_uri or "admin@example.com" in result.totp_uri
        # issuer が設定値（"TradeSystem Admin"）で組み込まれていること
        assert "TradeSystem" in result.totp_uri

    @pytest.mark.asyncio
    async def test_setup_stores_encrypted_secret(self, db_session):
        """setup 後に totp_secret_encrypted が DB に保存される"""
        from sqlalchemy import select as sa_select
        user = _make_user(db_session)
        await db_session.flush()

        pre_auth_user = _make_current_user(user.id, str(uuid.uuid4()))

        with patch("trade_app.admin.routes.auth.get_settings", return_value=_make_totp_settings_mock()):
            await setup_totp(_make_request(), Response(), pre_auth_user, db_session)

        row = (await db_session.execute(
            sa_select(UiUser).where(UiUser.id == user.id)
        )).scalar_one()
        assert row.totp_secret_encrypted is not None
        assert row.totp_secret_encrypted.startswith("gv1:")

    @pytest.mark.asyncio
    async def test_setup_does_not_set_totp_enabled(self, db_session):
        """setup 後は totp_enabled がまだ False（verify 後に True になる）"""
        from sqlalchemy import select as sa_select
        user = _make_user(db_session, totp_enabled=False)
        await db_session.flush()

        pre_auth_user = _make_current_user(user.id, str(uuid.uuid4()))

        with patch("trade_app.admin.routes.auth.get_settings", return_value=_make_totp_settings_mock()):
            await setup_totp(_make_request(), Response(), pre_auth_user, db_session)

        row = (await db_session.execute(
            sa_select(UiUser).where(UiUser.id == user.id)
        )).scalar_one()
        assert row.totp_enabled is False

    @pytest.mark.asyncio
    async def test_setup_encryption_not_configured_returns_503(self, db_session):
        """TOTP_ENCRYPTION_KEY 未設定 → 503"""
        from fastapi import HTTPException
        user = _make_user(db_session)
        await db_session.flush()

        pre_auth_user = _make_current_user(user.id, str(uuid.uuid4()))

        with patch("trade_app.admin.routes.auth.get_settings", return_value=_make_totp_settings_mock(TOTP_ENCRYPTION_KEY="")):
            with pytest.raises(HTTPException) as exc_info:
                await setup_totp(_make_request(), Response(), pre_auth_user, db_session)

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_setup_replaces_existing_secret(self, db_session):
        """2回目の setup で既存の encrypted secret が上書きされる"""
        from sqlalchemy import select as sa_select
        user = _make_user(db_session)
        await db_session.flush()

        pre_auth_user = _make_current_user(user.id, str(uuid.uuid4()))
        settings_mock = _make_totp_settings_mock()

        with patch("trade_app.admin.routes.auth.get_settings", return_value=settings_mock):
            await setup_totp(_make_request(), Response(), pre_auth_user, db_session)

        first_secret = (await db_session.execute(
            sa_select(UiUser).where(UiUser.id == user.id)
        )).scalar_one().totp_secret_encrypted

        with patch("trade_app.admin.routes.auth.get_settings", return_value=settings_mock):
            await setup_totp(_make_request(), Response(), pre_auth_user, db_session)

        second_secret = (await db_session.execute(
            sa_select(UiUser).where(UiUser.id == user.id)
        )).scalar_one().totp_secret_encrypted

        assert second_secret is not None
        # 2回目の暗号文は1回目と異なる（IV が毎回変わる）
        assert first_secret != second_secret


# ─── TestTotpVerify ───────────────────────────────────────────────────────────


class TestTotpVerify:
    def _make_verify_settings_mock(self, **kwargs):
        m = MagicMock()
        m.TOTP_ENCRYPTION_KEY = kwargs.get("TOTP_ENCRYPTION_KEY", _TEST_KEY_B64)
        m.SESSION_TTL_SEC = kwargs.get("SESSION_TTL_SEC", 28800)
        m.COOKIE_SECURE = kwargs.get("COOKIE_SECURE", False)
        return m

    def _setup_user_with_totp(self, db_session, token: str = "pre2fa_token"):
        """TOTP セットアップ済みユーザーとセッションを作成する"""
        import pyotp
        from trade_app.admin.services.encryption import TotpEncryptor

        user = _make_user(db_session, totp_enabled=False)
        totp_secret = pyotp.random_base32()
        enc = TotpEncryptor(_TEST_KEY_B64)
        user.totp_secret_encrypted = enc.encrypt(totp_secret)

        session = _make_session(db_session, user.id, token=token, is_2fa_completed=False)
        return user, session, totp_secret

    @pytest.mark.asyncio
    async def test_verify_success_sets_2fa_completed(self, db_session):
        """正常 verify: is_2fa_completed が True になる"""
        import pyotp
        from sqlalchemy import select as sa_select

        token = "verify_success_token"
        user, session, totp_secret = self._setup_user_with_totp(db_session, token=token)
        await db_session.flush()

        body = TotpVerifyRequest(session_id=session.id, totp_code="000000")
        request = _make_request(cookies={SESSION_COOKIE_NAME: token})
        response = Response()

        with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_verify_settings_mock()):
            with patch("pyotp.TOTP.verify", return_value=True):
                await verify_totp(request, response, body, db_session)

        row = (await db_session.execute(
            sa_select(UiSession).where(UiSession.id == session.id)
        )).scalar_one()
        assert row.is_2fa_completed is True

    @pytest.mark.asyncio
    async def test_verify_success_extends_cookie(self, db_session):
        """正常 verify: Cookie の max_age が SESSION_TTL_SEC に延長される"""
        token = "verify_cookie_token"
        user, session, totp_secret = self._setup_user_with_totp(db_session, token=token)
        await db_session.flush()

        body = TotpVerifyRequest(session_id=session.id, totp_code="000000")
        request = _make_request(cookies={SESSION_COOKIE_NAME: token})
        response = Response()

        with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_verify_settings_mock(SESSION_TTL_SEC=28800)):
            with patch("pyotp.TOTP.verify", return_value=True):
                await verify_totp(request, response, body, db_session)

        cookie_header = response.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in cookie_header
        # max-age が SESSION_TTL_SEC (28800) に設定されていること
        assert "28800" in cookie_header

    @pytest.mark.asyncio
    async def test_verify_success_sets_totp_enabled(self, db_session):
        """正常 verify: user.totp_enabled が True になる"""
        from sqlalchemy import select as sa_select

        token = "verify_totp_flag_token"
        user, session, totp_secret = self._setup_user_with_totp(db_session, token=token)
        await db_session.flush()

        body = TotpVerifyRequest(session_id=session.id, totp_code="000000")
        request = _make_request(cookies={SESSION_COOKIE_NAME: token})

        with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_verify_settings_mock()):
            with patch("pyotp.TOTP.verify", return_value=True):
                await verify_totp(request, Response(), body, db_session)

        row = (await db_session.execute(
            sa_select(UiUser).where(UiUser.id == user.id)
        )).scalar_one()
        assert row.totp_enabled is True

    @pytest.mark.asyncio
    async def test_verify_success_records_two_fa_success(self, db_session):
        """正常 verify: TWO_FA_SUCCESS 監査ログが記録される"""
        token = "verify_audit_token"
        user, session, totp_secret = self._setup_user_with_totp(db_session, token=token)
        await db_session.flush()

        body = TotpVerifyRequest(session_id=session.id, totp_code="000000")
        request = _make_request(cookies={SESSION_COOKIE_NAME: token})

        with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_verify_settings_mock()):
            with patch("pyotp.TOTP.verify", return_value=True):
                await verify_totp(request, Response(), body, db_session)

        audit_svc = UiAuditLogService(db_session)
        logs, total = await audit_svc.query(
            AuditLogFilter(event_type=AdminAuditEventType.TWO_FA_SUCCESS)
        )
        assert total == 1
        assert logs[0].user_email == "admin@example.com"

    @pytest.mark.asyncio
    async def test_verify_invalid_code_returns_401(self, db_session):
        """不正コード → 401"""
        from fastapi import HTTPException

        token = "verify_invalid_code_token"
        user, session, totp_secret = self._setup_user_with_totp(db_session, token=token)
        await db_session.flush()

        body = TotpVerifyRequest(session_id=session.id, totp_code="000000")
        request = _make_request(cookies={SESSION_COOKIE_NAME: token})

        with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_verify_settings_mock()):
            with patch("pyotp.TOTP.verify", return_value=False):
                with pytest.raises(HTTPException) as exc_info:
                    await verify_totp(request, Response(), body, db_session)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_verify_invalid_code_records_two_fa_failure(self, db_session):
        """不正コード → TWO_FA_FAILURE 監査ログが記録される"""
        token = "verify_failure_audit_token"
        user, session, totp_secret = self._setup_user_with_totp(db_session, token=token)
        await db_session.flush()

        body = TotpVerifyRequest(session_id=session.id, totp_code="000000")
        request = _make_request(cookies={SESSION_COOKIE_NAME: token})

        with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_verify_settings_mock()):
            with patch("pyotp.TOTP.verify", return_value=False):
                try:
                    await verify_totp(request, Response(), body, db_session)
                except Exception:
                    pass

        audit_svc = UiAuditLogService(db_session)
        logs, total = await audit_svc.query(
            AuditLogFilter(event_type=AdminAuditEventType.TWO_FA_FAILURE)
        )
        assert total == 1

    @pytest.mark.asyncio
    async def test_verify_session_not_found_returns_401(self, db_session):
        """存在しない session_id → 401"""
        from fastapi import HTTPException

        # 任意のユーザーとトークンが必要（Cookie チェックに使う）
        user = _make_user(db_session)
        token = "orphan_token"
        _make_session(db_session, user.id, token=token, is_2fa_completed=False)
        await db_session.flush()

        body = TotpVerifyRequest(session_id=str(uuid.uuid4()), totp_code="000000")
        request = _make_request(cookies={SESSION_COOKIE_NAME: token})

        with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_verify_settings_mock()):
            with pytest.raises(HTTPException) as exc_info:
                await verify_totp(request, Response(), body, db_session)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_verify_cookie_token_mismatch_returns_401(self, db_session):
        """Cookie のトークンと session_id が対応しない → 401"""
        from fastapi import HTTPException

        user = _make_user(db_session)
        token1 = "token_for_session1"
        token2 = "token_for_session2"
        session1 = _make_session(db_session, user.id, token=token1, is_2fa_completed=False)
        _make_session(db_session, user.id, token=token2, is_2fa_completed=False)
        await db_session.flush()

        # session1 の ID だが Cookie は token2（不一致）
        body = TotpVerifyRequest(session_id=session1.id, totp_code="000000")
        request = _make_request(cookies={SESSION_COOKIE_NAME: token2})

        with patch("trade_app.admin.routes.auth.get_settings", return_value=self._make_verify_settings_mock()):
            with pytest.raises(HTTPException) as exc_info:
                await verify_totp(request, Response(), body, db_session)

        assert exc_info.value.status_code == 401
