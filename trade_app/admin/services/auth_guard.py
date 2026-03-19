"""
認証ガード — FastAPI 依存関数

仕様書: 管理画面仕様書 v0.3 §5(権限制御)
設計書: docs/admin/design_i4_auth_gaps.md（I-4 確定済み）

【I-4 実装済み内容】
- セッショントークンを HttpOnly Cookie（trade_admin_session）から読み取る
- Authorization: Bearer ヘッダーは使用しない
- セッション期限切れ時に SESSION_EXPIRED_ACCESS 監査ログを記録（I-5 確定）

【セッション検証フロー】
1. Cookie "trade_admin_session" からトークンを取得
2. SHA-256 ハッシュで ui_sessions を検索
3. invalidated_at が設定済み → 401（明示的に無効化済み）
4. expires_at 超過 → SESSION_EXPIRED_ACCESS ログ → 401
5. is_2fa_completed=False → 401（TOTP 未完了）
6. UiUser.is_active=False → 401（アカウント無効）

【TODO 一覧】
- TODO(I-3 / Phase 2): operator/viewer ロール分岐の実装
"""
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.constants import AdminAuditEventType
from trade_app.admin.database import get_admin_db  # noqa: F401 — re-export for routes
from trade_app.admin.models.ui_session import UiSession
from trade_app.admin.models.ui_user import UiUser

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "trade_admin_session"


@dataclass
class AdminUser:
    """認証済み管理画面ユーザーの情報。依存関数が返す値。"""
    user_id: str
    email: str
    display_name: str | None
    role: str
    session_id: str


# ─── セッショントークンのハッシュ ──────────────────────────────────────────────


def hash_session_token(raw_token: str) -> str:
    """セッショントークンを SHA-256 でハッシュ化して返す"""
    return hashlib.sha256(raw_token.encode()).hexdigest()


# ─── 認証ガード（メイン） ─────────────────────────────────────────────────────


async def get_current_admin_user(
    request: Request,
    db: AsyncSession = Depends(get_admin_db),
) -> AdminUser:
    """
    管理画面の認証ガード。FastAPI の Depends で使用する。

    HttpOnly Cookie（trade_admin_session）からセッショントークンを読み取り、
    ui_sessions テーブルで検証する。セッション期限切れは SESSION_EXPIRED_ACCESS として記録。
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証が必要です。ログインしてください。",
        )

    token_hash = hash_session_token(raw_token)

    # セッション検索（invalidated_at がないもの）
    session_result = await db.execute(
        select(UiSession)
        .where(UiSession.session_token_hash == token_hash)
        .where(UiSession.invalidated_at.is_(None))
    )
    session = session_result.scalar_one_or_none()

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="セッションが無効です。再ログインしてください。",
        )

    # 期限切れチェック（SESSION_EXPIRED_ACCESS として監査ログ記録）
    # SQLite テストでは expires_at が naive datetime になることがある
    now = datetime.now(timezone.utc)
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        await _log_expired_access(db, session, request)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="セッションが期限切れです。再ログインしてください。",
        )

    # 2FA 完了チェック
    if not session.is_2fa_completed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="2FA 認証が未完了です。",
        )

    # ユーザー情報取得
    user_result = await db.execute(
        select(UiUser).where(UiUser.id == session.user_id)
    )
    user = user_result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ユーザーアカウントが無効です。",
        )

    return AdminUser(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        session_id=session.id,
    )


async def _log_expired_access(
    db: AsyncSession,
    session: UiSession,
    request: Request,
) -> None:
    """期限切れセッションへのアクセスを SESSION_EXPIRED_ACCESS として監査ログ記録する"""
    from trade_app.admin.services.audit_log_service import UiAuditLogService

    try:
        forwarded = request.headers.get("X-Forwarded-For")
        ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else None
        )

        # ユーザー email を取得（セッションからの user_id で）
        user_result = await db.execute(
            select(UiUser).where(UiUser.id == session.user_id)
        )
        user = user_result.scalar_one_or_none()

        audit_svc = UiAuditLogService(db)
        await audit_svc.write(
            AdminAuditEventType.SESSION_EXPIRED_ACCESS,
            user_id=session.user_id,
            user_email=user.email if user else None,
            ip_address=ip,
            user_agent=request.headers.get("User-Agent"),
        )
        await db.commit()
    except Exception as exc:
        # 監査ログ記録失敗でも認証エラーの返却は継続する
        logger.error("SESSION_EXPIRED_ACCESS ログ記録失敗: %s", exc)


# ─── Pre-2FA 認証ガード（TOTP setup/verify 用） ────────────────────────────────


async def get_pre2fa_user(
    request: Request,
    db: AsyncSession = Depends(get_admin_db),
) -> AdminUser:
    """
    Pre-2FA セッションを許可する認証ガード。

    通常の get_current_admin_user と異なり、is_2fa_completed=False のセッションも通過する。
    POST /auth/totp/setup 専用。verify は session_id を body から直接 SELECT するため不要。

    検証フロー:
    1. Cookie からトークンを取得
    2. ui_sessions で検索（invalidated_at がないもの）
    3. expires_at チェック（SESSION_EXPIRED_ACCESS ログ）
    4. is_2fa_completed の制限なし（False でも通過）
    5. UiUser.is_active チェック
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証が必要です。ログインしてください。",
        )

    token_hash = hash_session_token(raw_token)

    session_result = await db.execute(
        select(UiSession)
        .where(UiSession.session_token_hash == token_hash)
        .where(UiSession.invalidated_at.is_(None))
    )
    session = session_result.scalar_one_or_none()

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="セッションが無効です。再ログインしてください。",
        )

    now = datetime.now(timezone.utc)
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        await _log_expired_access(db, session, request)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="セッションが期限切れです。再ログインしてください。",
        )

    user_result = await db.execute(
        select(UiUser).where(UiUser.id == session.user_id)
    )
    user = user_result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ユーザーアカウントが無効です。",
        )

    return AdminUser(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        session_id=session.id,
    )


# ─── ロール要件チェック（将来用インターフェース） ──────────────────────────────


def require_role(*roles: str):
    """
    特定ロールを要求する依存関数ファクトリ。
    Phase 1 では全 admin で動作するため実質チェックを行わない。
    Phase 2 以降で operator/viewer の分岐を実装する。

    使用例:
        @router.post("/symbols", dependencies=[Depends(require_role("admin", "operator"))])
    """
    async def _check_role(
        current_user: Annotated[AdminUser, Depends(get_current_admin_user)]
    ) -> AdminUser:
        # Phase 1: admin のみ存在するため、認証済みなら全て通過
        # TODO(Phase 2): ロール分岐を実装すること
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"この操作には {roles} のいずれかのロールが必要です。",
            )
        return current_user

    return _check_role


# ─── よく使う依存関数エイリアス ────────────────────────────────────────────────

# Phase 1: 全エンドポイントでこれを使う
RequireAdmin = Annotated[AdminUser, Depends(get_current_admin_user)]

# Pre-2FA セッションも許可（TOTP setup 用）
RequirePreAuth = Annotated[AdminUser, Depends(get_pre2fa_user)]
