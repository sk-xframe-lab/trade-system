"""
constants.py のテスト

【検証内容】
- 各 Enum のコード値が期待通りか
- VALID_EVENT_CODES が NotificationEventCode と一致するか
- USER_INITIATED_EVENTS に秘密情報関連イベントが含まれないか
- SENSITIVE_KEYS が適切なキーを含むか
"""
import pytest

from trade_app.admin.constants import (
    AdminAuditEventType,
    ConnectionTestResultCode,
    NotificationEventCode,
    NOTIFICATION_EVENT_LABELS,
    SYSTEM_INITIATED_EVENTS,
    USER_INITIATED_EVENTS,
    VALID_EVENT_CODES,
)
from trade_app.admin.services.audit_log_service import SENSITIVE_KEYS


class TestNotificationEventCode:
    def test_all_codes_are_strings(self):
        for code in NotificationEventCode:
            assert isinstance(code.value, str)

    def test_valid_event_codes_matches_enum(self):
        expected = frozenset(e.value for e in NotificationEventCode)
        assert VALID_EVENT_CODES == expected

    def test_all_codes_have_labels(self):
        for code in NotificationEventCode:
            assert code in NOTIFICATION_EVENT_LABELS, f"{code} にラベルがありません"

    def test_seven_event_codes_defined(self):
        assert len(NotificationEventCode) == 7


class TestConnectionTestResultCode:
    def test_six_result_codes(self):
        assert len(ConnectionTestResultCode) == 6

    def test_not_configured_exists(self):
        assert ConnectionTestResultCode.NOT_CONFIGURED == "NOT_CONFIGURED"

    def test_auth_ok_is_success(self):
        from trade_app.admin.constants import CONNECTION_TEST_SUCCESS_CODES
        assert ConnectionTestResultCode.AUTH_OK.value in CONNECTION_TEST_SUCCESS_CODES
        assert ConnectionTestResultCode.NETWORK_OK.value not in CONNECTION_TEST_SUCCESS_CODES


class TestAdminAuditEventType:
    def test_symbol_events_defined(self):
        assert AdminAuditEventType.SYMBOL_CREATED
        assert AdminAuditEventType.SYMBOL_UPDATED
        assert AdminAuditEventType.SYMBOL_ENABLED
        assert AdminAuditEventType.SYMBOL_DISABLED
        assert AdminAuditEventType.SYMBOL_DELETED

    def test_notification_events_defined(self):
        assert AdminAuditEventType.NOTIFICATION_CONFIG_CREATED
        assert AdminAuditEventType.NOTIFICATION_CONFIG_UPDATED
        assert AdminAuditEventType.NOTIFICATION_CONFIG_DELETED
        assert AdminAuditEventType.NOTIFICATION_TEST_SENT
        assert AdminAuditEventType.NOTIFICATION_TEST_FAILED

    def test_system_events_defined(self):
        assert AdminAuditEventType.HALT_TRIGGERED_MANUAL
        assert AdminAuditEventType.HALT_RELEASED
        assert AdminAuditEventType.SYSTEM_SETTINGS_UPDATED

    def test_user_initiated_events_subset(self):
        """USER_INITIATED_EVENTS は AdminAuditEventType の値のみを含む"""
        all_values = frozenset(e.value for e in AdminAuditEventType)
        for event in USER_INITIATED_EVENTS:
            assert event in all_values, f"{event} は AdminAuditEventType に存在しません"

    def test_system_initiated_events_subset(self):
        all_values = frozenset(e.value for e in AdminAuditEventType)
        for event in SYSTEM_INITIATED_EVENTS:
            assert event in all_values

    def test_no_overlap_between_user_and_system_events(self):
        overlap = USER_INITIATED_EVENTS & SYSTEM_INITIATED_EVENTS
        assert len(overlap) == 0, f"重複イベント: {overlap}"

    def test_halt_auto_is_system_event(self):
        assert AdminAuditEventType.HALT_TRIGGERED_AUTO in SYSTEM_INITIATED_EVENTS
        assert AdminAuditEventType.HALT_TRIGGERED_AUTO not in USER_INITIATED_EVENTS

    def test_halt_manual_is_user_event(self):
        assert AdminAuditEventType.HALT_TRIGGERED_MANUAL in USER_INITIATED_EVENTS
        assert AdminAuditEventType.HALT_TRIGGERED_MANUAL not in SYSTEM_INITIATED_EVENTS


class TestSensitiveKeys:
    def test_password_is_sensitive(self):
        assert "password" in SENSITIVE_KEYS

    def test_totp_secret_is_sensitive(self):
        assert "totp_secret" in SENSITIVE_KEYS
        assert "totp_secret_encrypted" in SENSITIVE_KEYS

    def test_session_token_is_sensitive(self):
        assert "session_token" in SENSITIVE_KEYS
        assert "session_token_hash" in SENSITIVE_KEYS

    def test_api_key_is_sensitive(self):
        assert "api_key" in SENSITIVE_KEYS

    def test_access_token_is_sensitive(self):
        assert "access_token" in SENSITIVE_KEYS
        assert "refresh_token" in SENSITIVE_KEYS

    def test_frozenset_is_immutable(self):
        with pytest.raises((AttributeError, TypeError)):
            SENSITIVE_KEYS.add("new_key")  # type: ignore
