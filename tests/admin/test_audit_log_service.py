"""
UiAuditLogService のテスト

【検証内容】
- write(): APPEND ONLY INSERT。before/after_json の秘密情報除去。
- _sanitize(): 再帰的に SENSITIVE_KEYS を [REDACTED] に置換する。
- query(): フィルタ・ページネーション。
- get_by_id(): 1件取得。
- USER_INITIATED_EVENTS で ip_address が None でも警告のみで記録は継続する。
"""
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from trade_app.admin.constants import AdminAuditEventType
from trade_app.admin.services.audit_log_service import (
    SENSITIVE_KEYS,
    UiAuditLogService,
    _sanitize,
)
from trade_app.admin.schemas.audit_log import AuditLogFilter


# ─── _sanitize() のテスト ────────────────────────────────────────────────────


class TestSanitize:
    def test_none_returns_none(self):
        assert _sanitize(None) is None

    def test_non_sensitive_keys_pass_through(self):
        data = {"symbol_code": "7203", "is_enabled": True}
        result = _sanitize(data)
        assert result == data

    def test_password_is_redacted(self):
        data = {"password": "secret123"}
        result = _sanitize(data)
        assert result["password"] == "[REDACTED]"

    def test_totp_secret_is_redacted(self):
        data = {"totp_secret": "JBSWY3DPEHPK3PXP"}
        result = _sanitize(data)
        assert result["totp_secret"] == "[REDACTED]"

    def test_session_token_hash_is_redacted(self):
        data = {"session_token_hash": "abc123"}
        result = _sanitize(data)
        assert result["session_token_hash"] == "[REDACTED]"

    def test_api_key_is_redacted(self):
        data = {"api_key": "sk-secret"}
        result = _sanitize(data)
        assert result["api_key"] == "[REDACTED]"

    def test_nested_dict_is_sanitized(self):
        data = {
            "user": {
                "email": "test@example.com",
                "password": "secret",
            }
        }
        result = _sanitize(data)
        assert result["user"]["email"] == "test@example.com"
        assert result["user"]["password"] == "[REDACTED]"

    def test_non_dict_values_pass_through(self):
        data = {"count": 5, "items": [1, 2, 3], "name": "test"}
        result = _sanitize(data)
        assert result == data

    def test_all_sensitive_keys_are_redacted(self):
        data = {key: "value" for key in SENSITIVE_KEYS}
        result = _sanitize(data)
        for key in SENSITIVE_KEYS:
            assert result[key] == "[REDACTED]"


# ─── UiAuditLogService.write() のテスト ─────────────────────────────────────


class TestAuditLogWrite:
    @pytest.mark.asyncio
    async def test_write_creates_record(self, db_session):
        svc = UiAuditLogService(db_session)
        log = await svc.write(
            AdminAuditEventType.SYMBOL_CREATED,
            user_id=str(uuid.uuid4()),
            user_email="admin@example.com",
            resource_type="symbol_config",
            resource_id=str(uuid.uuid4()),
            resource_label="7203",
            ip_address="192.168.1.1",
            user_agent="Mozilla/5.0",
        )
        assert log.id is not None
        assert log.event_type == AdminAuditEventType.SYMBOL_CREATED
        assert log.user_email == "admin@example.com"
        assert log.resource_label == "7203"

    @pytest.mark.asyncio
    async def test_write_sanitizes_before_json(self, db_session):
        svc = UiAuditLogService(db_session)
        log = await svc.write(
            AdminAuditEventType.SYMBOL_UPDATED,
            before_json={"symbol_code": "7203", "password": "should_be_redacted"},
            after_json={"symbol_code": "7203", "is_enabled": True},
        )
        assert log.before_json["password"] == "[REDACTED]"
        assert log.before_json["symbol_code"] == "7203"
        assert log.after_json["is_enabled"] is True

    @pytest.mark.asyncio
    async def test_write_sanitizes_after_json(self, db_session):
        svc = UiAuditLogService(db_session)
        log = await svc.write(
            AdminAuditEventType.BROKER_CONFIG_UPDATED,
            after_json={"user_id": "user1", "api_key": "secret_key"},
        )
        assert log.after_json["api_key"] == "[REDACTED]"
        assert log.after_json["user_id"] == "user1"

    @pytest.mark.asyncio
    async def test_write_user_initiated_without_ip_logs_warning(self, db_session, caplog):
        """USER_INITIATED_EVENTS で ip_address=None でも記録は継続する"""
        import logging
        svc = UiAuditLogService(db_session)
        with caplog.at_level(logging.WARNING, logger="trade_app.admin.services.audit_log_service"):
            log = await svc.write(
                AdminAuditEventType.SYMBOL_CREATED,
                ip_address=None,
            )
        assert log is not None
        assert "ip_address" in caplog.text

    @pytest.mark.asyncio
    async def test_write_system_event_without_ip_no_warning(self, db_session, caplog):
        """SYSTEM_INITIATED_EVENTS では ip_address=None でも警告なし"""
        import logging
        svc = UiAuditLogService(db_session)
        with caplog.at_level(logging.WARNING, logger="trade_app.admin.services.audit_log_service"):
            log = await svc.write(
                AdminAuditEventType.HALT_TRIGGERED_AUTO,
                ip_address=None,
            )
        assert log is not None
        assert len(caplog.records) == 0

    @pytest.mark.asyncio
    async def test_write_null_before_and_after(self, db_session):
        svc = UiAuditLogService(db_session)
        log = await svc.write(
            AdminAuditEventType.LOGOUT,
            before_json=None,
            after_json=None,
        )
        assert log.before_json is None
        assert log.after_json is None


# ─── UiAuditLogService.query() のテスト ─────────────────────────────────────


class TestAuditLogQuery:
    @pytest.mark.asyncio
    async def test_query_returns_all_records(self, db_session):
        svc = UiAuditLogService(db_session)
        for i in range(3):
            await svc.write(
                AdminAuditEventType.SYMBOL_CREATED,
                user_email=f"user{i}@example.com",
            )
        await db_session.flush()

        filters = AuditLogFilter()
        logs, total = await svc.query(filters)
        assert total == 3
        assert len(logs) == 3

    @pytest.mark.asyncio
    async def test_query_filters_by_event_type(self, db_session):
        svc = UiAuditLogService(db_session)
        await svc.write(AdminAuditEventType.SYMBOL_CREATED)
        await svc.write(AdminAuditEventType.SYMBOL_DELETED)
        await db_session.flush()

        filters = AuditLogFilter(event_type=AdminAuditEventType.SYMBOL_CREATED)
        logs, total = await svc.query(filters)
        assert total == 1
        assert logs[0].event_type == AdminAuditEventType.SYMBOL_CREATED

    @pytest.mark.asyncio
    async def test_query_filters_by_user_email(self, db_session):
        svc = UiAuditLogService(db_session)
        await svc.write(AdminAuditEventType.SYMBOL_CREATED, user_email="alice@example.com")
        await svc.write(AdminAuditEventType.SYMBOL_CREATED, user_email="bob@example.com")
        await db_session.flush()

        filters = AuditLogFilter(user_email="alice")
        logs, total = await svc.query(filters)
        assert total == 1
        assert "alice" in logs[0].user_email

    @pytest.mark.asyncio
    async def test_query_pagination(self, db_session):
        svc = UiAuditLogService(db_session)
        for _ in range(5):
            await svc.write(AdminAuditEventType.SYMBOL_CREATED)
        await db_session.flush()

        filters = AuditLogFilter()
        logs, total = await svc.query(filters, offset=0, limit=2)
        assert total == 5
        assert len(logs) == 2

    @pytest.mark.asyncio
    async def test_query_orders_by_created_at_desc(self, db_session):
        svc = UiAuditLogService(db_session)
        await svc.write(AdminAuditEventType.SYMBOL_CREATED, description="first")
        await svc.write(AdminAuditEventType.SYMBOL_UPDATED, description="second")
        await db_session.flush()

        filters = AuditLogFilter()
        logs, _ = await svc.query(filters, limit=10)
        # 最新が先頭
        assert logs[0].description == "second"
        assert logs[1].description == "first"


# ─── UiAuditLogService.get_by_id() のテスト ─────────────────────────────────


class TestAuditLogGetById:
    @pytest.mark.asyncio
    async def test_get_existing_record(self, db_session):
        svc = UiAuditLogService(db_session)
        log = await svc.write(
            AdminAuditEventType.SYMBOL_CREATED,
            description="test log",
        )
        await db_session.flush()

        fetched = await svc.get_by_id(log.id)
        assert fetched is not None
        assert fetched.id == log.id
        assert fetched.description == "test log"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, db_session):
        svc = UiAuditLogService(db_session)
        result = await svc.get_by_id(str(uuid.uuid4()))
        assert result is None
