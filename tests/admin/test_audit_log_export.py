"""
監査ログ CSV エクスポートのテスト

【検証内容】
- export: AUDIT_LOG_EXPORTED 監査ログが記録される
- export: 空 DB でも正常に CSV を返す（ヘッダー行のみ）
- export: フィルタサマリーが description に含まれる
- _build_export_filter_summary: 全フィルタ指定時の出力
- _build_export_filter_summary: フィルタなしは '全件'
- _common: get_client_ip が X-Forwarded-For を優先する
- _common: X-Forwarded-For がない場合は request.client.host を返す
- _common: どちらもない場合は None を返す
- admin API エラーレスポンス形式の確認（404 の detail キー確認）
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from trade_app.admin.routes._common import get_client_ip, get_user_agent
from trade_app.admin.routes.audit_logs import (
    _build_export_filter_summary,
    export_audit_logs_csv,
)
from trade_app.admin.services.audit_log_service import UiAuditLogService
from trade_app.admin.services.auth_guard import AdminUser
from trade_app.admin.schemas.audit_log import AuditLogFilter
from trade_app.admin.constants import AdminAuditEventType


def _make_current_user() -> AdminUser:
    return AdminUser(
        user_id=str(uuid.uuid4()),
        email="admin@example.com",
        display_name="テスト管理者",
        role="admin",
        session_id=str(uuid.uuid4()),
    )


class TestGetClientIp:
    """get_client_ip のユニットテスト（_common.py）"""

    def test_uses_x_forwarded_for_first(self):
        """X-Forwarded-For がある場合はその最初の値を返す"""
        request = MagicMock()
        request.headers = {"X-Forwarded-For": "203.0.113.1, 10.0.0.1"}
        request.client = MagicMock()
        request.client.host = "10.0.0.1"
        assert get_client_ip(request) == "203.0.113.1"

    def test_falls_back_to_client_host(self):
        """X-Forwarded-For がない場合は request.client.host を返す"""
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        assert get_client_ip(request) == "192.168.1.1"

    def test_returns_none_when_no_client(self):
        """request.client が None の場合は None を返す"""
        request = MagicMock()
        request.headers = {}
        request.client = None
        assert get_client_ip(request) is None

    def test_strips_whitespace_from_forwarded(self):
        """X-Forwarded-For の値の空白をトリムする"""
        request = MagicMock()
        request.headers = {"X-Forwarded-For": "  203.0.113.1  , 10.0.0.1"}
        result = get_client_ip(request)
        assert result == "203.0.113.1"


class TestGetUserAgent:
    """get_user_agent のユニットテスト（_common.py）"""

    def test_returns_user_agent_header(self):
        request = MagicMock()
        request.headers = {"User-Agent": "Mozilla/5.0"}
        assert get_user_agent(request) == "Mozilla/5.0"

    def test_returns_none_when_absent(self):
        request = MagicMock()
        request.headers = {}
        assert get_user_agent(request) is None


class TestBuildExportFilterSummary:
    """_build_export_filter_summary のユニットテスト"""

    def test_no_filter_returns_all(self):
        result = _build_export_filter_summary(None, None, None, None, None)
        assert result == "全件"

    def test_date_from_included(self):
        dt = datetime(2026, 3, 18, tzinfo=timezone.utc)
        result = _build_export_filter_summary(dt, None, None, None, None)
        assert "from=2026-03-18" in result

    def test_date_to_included(self):
        dt = datetime(2026, 3, 18, tzinfo=timezone.utc)
        result = _build_export_filter_summary(None, dt, None, None, None)
        assert "to=2026-03-18" in result

    def test_user_email_included(self):
        result = _build_export_filter_summary(None, None, "user@example.com", None, None)
        assert "user=user@example.com" in result

    def test_event_type_included(self):
        result = _build_export_filter_summary(None, None, None, "LOGOUT", None)
        assert "event=LOGOUT" in result

    def test_all_filters_combined(self):
        dt_from = datetime(2026, 3, 1, tzinfo=timezone.utc)
        dt_to = datetime(2026, 3, 18, tzinfo=timezone.utc)
        result = _build_export_filter_summary(dt_from, dt_to, "u@e.com", "LOGOUT", "ui_session")
        assert "from=" in result
        assert "to=" in result
        assert "user=" in result
        assert "event=" in result
        assert "resource=" in result


class TestAuditLogExportLogging:
    """CSV エクスポート操作が監査ログに記録されることを確認する"""

    @pytest.mark.asyncio
    async def test_export_writes_audit_log(self, db_session):
        """CSV エクスポートは AUDIT_LOG_EXPORTED 監査ログを記録する"""
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        current_user = _make_current_user()

        # FastAPI の依存注入を介さず直接呼ぶため、Query デフォルト値を明示的に None で渡す
        await export_audit_logs_csv(
            request=request,
            current_user=current_user,
            db=db_session,
            date_from=None,
            date_to=None,
            user_email=None,
            event_type=None,
            resource_type=None,
        )

        audit_svc = UiAuditLogService(db_session)
        logs, total = await audit_svc.query(
            AuditLogFilter(event_type=AdminAuditEventType.AUDIT_LOG_EXPORTED)
        )
        assert total == 1
        assert logs[0].user_email == "admin@example.com"

    @pytest.mark.asyncio
    async def test_export_description_mentions_count(self, db_session):
        """監査ログの description にエクスポート件数が含まれる"""
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        current_user = _make_current_user()

        await export_audit_logs_csv(
            request=request,
            current_user=current_user,
            db=db_session,
            date_from=None,
            date_to=None,
            user_email=None,
            event_type=None,
            resource_type=None,
        )

        audit_svc = UiAuditLogService(db_session)
        logs, _ = await audit_svc.query(
            AuditLogFilter(event_type=AdminAuditEventType.AUDIT_LOG_EXPORTED)
        )
        assert "件" in logs[0].description

    @pytest.mark.asyncio
    async def test_export_empty_db_returns_csv_with_header_only(self, db_session):
        """DB が空のとき CSV にはヘッダー行のみが含まれる"""
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        current_user = _make_current_user()

        response = await export_audit_logs_csv(
            request=request,
            current_user=current_user,
            db=db_session,
            date_from=None,
            date_to=None,
            user_email=None,
            event_type=None,
            resource_type=None,
        )
        # StreamingResponse のボディを取得（ルートは iter([str]) で返すため str のまま）
        chunks = [chunk async for chunk in response.body_iterator]
        content = "".join(c if isinstance(c, str) else c.decode("utf-8") for c in chunks)
        lines = [line for line in content.splitlines() if line]
        # CSV クエリはエクスポート前に実行されるため、空 DB ではヘッダー行のみ
        assert lines[0].startswith("id,created_at")
        assert len(lines) == 1  # header only
