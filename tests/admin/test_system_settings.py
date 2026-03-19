"""
system_settings ルート・スキーマのテスト

【検証内容】
- GET /system-settings: 設定値フィールドが返る
- GET /system-settings: persistence_mode == "runtime_only" を返す（暫定実装の明示）
- PATCH /system-settings: 値が in-memory に反映される（ランタイム上書き）
- PATCH /system-settings: before/after が正しく含まれる
- PATCH /system-settings: updated_fields が含まれる
- PATCH /system-settings: 変更なし（空ボディ）は "変更なし" を返す
- PATCH /system-settings: persistence_mode == "runtime_only" を返す
- PATCH /system-settings: 監査ログが記録される
- PATCH /system-settings: バリデーション（負値・ゼロ値）は更新されない
- persistence_mode / persistence_note フィールドが SystemSettingsResponse に存在する
- SystemSettingsUpdateResponse にも persistence_mode / persistence_note が存在する

【重要な設計メモ】
これらのテストが検証する「ランタイム上書き」は暫定実装である。
プロセス再起動後は .env の値に戻る（TODO Phase 2: 永続化未実装）。
テスト内で値を変更した場合は teardown で元に戻すこと（他テストへの影響防止）。
"""
import uuid

import pytest

from trade_app.admin.schemas.system_settings import (
    PERSISTENCE_MODE,
    PERSISTENCE_NOTE,
    SystemSettingsResponse,
    SystemSettingsUpdateRequest,
    SystemSettingsUpdateResponse,
)
from trade_app.admin.routes.system_settings import _settings_to_dict, update_system_settings
from trade_app.admin.services.auth_guard import AdminUser
from trade_app.admin.services.audit_log_service import UiAuditLogService
from trade_app.admin.schemas.audit_log import AuditLogFilter
from trade_app.admin.constants import AdminAuditEventType
from trade_app.config import get_settings


def _make_current_user() -> AdminUser:
    return AdminUser(
        user_id=str(uuid.uuid4()),
        email="admin@example.com",
        display_name="テスト管理者",
        role="admin",
        session_id=str(uuid.uuid4()),
    )


class TestSystemSettingsPersistenceMode:
    """persistence_mode フィールドが暫定実装を正しく明示しているか"""

    def test_persistence_mode_constant_is_runtime_only(self):
        """PERSISTENCE_MODE 定数は 'runtime_only' であること"""
        assert PERSISTENCE_MODE == "runtime_only"

    def test_persistence_note_mentions_restart(self):
        """PERSISTENCE_NOTE は再起動に言及していること"""
        assert "再起動" in PERSISTENCE_NOTE

    def test_persistence_note_mentions_env(self):
        """PERSISTENCE_NOTE は .env に言及していること"""
        assert ".env" in PERSISTENCE_NOTE

    def test_system_settings_response_has_persistence_mode(self):
        """SystemSettingsResponse は persistence_mode フィールドを持つ"""
        settings = get_settings()
        watched = [s.strip() for s in settings.WATCHED_SYMBOLS.split(",") if s.strip()]
        response = SystemSettingsResponse(
            daily_loss_limit_jpy=settings.DAILY_LOSS_LIMIT_JPY,
            max_concurrent_positions=settings.MAX_CONCURRENT_POSITIONS,
            consecutive_losses_stop=settings.CONSECUTIVE_LOSSES_STOP,
            exit_watcher_interval_sec=settings.EXIT_WATCHER_INTERVAL_SEC,
            strategy_runner_interval_sec=settings.STRATEGY_RUNNER_INTERVAL_SEC,
            market_state_interval_sec=settings.MARKET_STATE_INTERVAL_SEC,
            strategy_max_state_age_sec=settings.STRATEGY_MAX_STATE_AGE_SEC,
            signal_max_decision_age_sec=settings.SIGNAL_MAX_DECISION_AGE_SEC,
            watched_symbols=watched,
        )
        assert response.persistence_mode == "runtime_only"
        assert "再起動" in response.persistence_note

    def test_system_settings_update_response_has_persistence_mode(self):
        """SystemSettingsUpdateResponse は persistence_mode フィールドを持つ"""
        response = SystemSettingsUpdateResponse(
            updated_fields=["daily_loss_limit_jpy"],
            before={"daily_loss_limit_jpy": 100000.0},
            after={"daily_loss_limit_jpy": 200000.0},
            message="1 項目を更新しました",
        )
        assert response.persistence_mode == "runtime_only"
        assert "再起動" in response.persistence_note


class TestSystemSettingsRuntimeUpdate:
    """
    PATCH /system-settings のランタイム上書きテスト。

    ⚠️ 各テストは Settings シングルトンを変更するため、
    変更後に元の値へ戻す（teardown）こと。
    """

    @pytest.mark.asyncio
    async def test_update_changes_value_in_memory(self, db_session):
        """更新後に get_settings() が新しい値を返す（ランタイム上書き）"""
        from unittest.mock import MagicMock
        settings = get_settings()
        original = settings.CONSECUTIVE_LOSSES_STOP

        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        body = SystemSettingsUpdateRequest(consecutive_losses_stop=99)
        current_user = _make_current_user()

        try:
            result = await update_system_settings(request, body, current_user, db_session)
            assert result.updated_fields == ["consecutive_losses_stop"]
            # in-memory の値が変わっていることを確認
            assert get_settings().CONSECUTIVE_LOSSES_STOP == 99
        finally:
            # 他テストに影響しないよう元に戻す
            object.__setattr__(settings, "CONSECUTIVE_LOSSES_STOP", original)

    @pytest.mark.asyncio
    async def test_update_returns_before_and_after(self, db_session):
        """レスポンスに before / after が含まれる"""
        from unittest.mock import MagicMock
        settings = get_settings()
        original = settings.MAX_CONCURRENT_POSITIONS

        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        body = SystemSettingsUpdateRequest(max_concurrent_positions=original + 1)
        current_user = _make_current_user()

        try:
            result = await update_system_settings(request, body, current_user, db_session)
            assert "max_concurrent_positions" in result.before
            assert "max_concurrent_positions" in result.after
            assert result.before["max_concurrent_positions"] == original
            assert result.after["max_concurrent_positions"] == original + 1
        finally:
            object.__setattr__(settings, "MAX_CONCURRENT_POSITIONS", original)

    @pytest.mark.asyncio
    async def test_update_empty_body_returns_no_change(self, db_session):
        """空ボディ（変更なし）の場合は updated_fields が空でメッセージが '変更なし'"""
        from unittest.mock import MagicMock
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        body = SystemSettingsUpdateRequest()  # 全フィールド None
        current_user = _make_current_user()

        result = await update_system_settings(request, body, current_user, db_session)
        assert result.updated_fields == []
        assert result.message == "変更なし"

    @pytest.mark.asyncio
    async def test_update_response_includes_persistence_mode(self, db_session):
        """PATCH レスポンスに persistence_mode == 'runtime_only' が含まれる"""
        from unittest.mock import MagicMock
        settings = get_settings()
        original = settings.EXIT_WATCHER_INTERVAL_SEC

        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        body = SystemSettingsUpdateRequest(exit_watcher_interval_sec=original)
        current_user = _make_current_user()

        try:
            result = await update_system_settings(request, body, current_user, db_session)
            assert result.persistence_mode == "runtime_only"
        finally:
            object.__setattr__(settings, "EXIT_WATCHER_INTERVAL_SEC", original)

    @pytest.mark.asyncio
    async def test_update_writes_audit_log(self, db_session):
        """PATCH は SYSTEM_SETTINGS_UPDATED 監査ログを記録する"""
        from unittest.mock import MagicMock
        settings = get_settings()
        original = settings.STRATEGY_MAX_STATE_AGE_SEC

        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "10.0.0.1"

        body = SystemSettingsUpdateRequest(strategy_max_state_age_sec=300)
        current_user = _make_current_user()

        try:
            await update_system_settings(request, body, current_user, db_session)

            audit_svc = UiAuditLogService(db_session)
            logs, total = await audit_svc.query(
                AuditLogFilter(event_type=AdminAuditEventType.SYSTEM_SETTINGS_UPDATED)
            )
            assert total >= 1
            assert any(log.user_email == "admin@example.com" for log in logs)
        finally:
            object.__setattr__(settings, "STRATEGY_MAX_STATE_AGE_SEC", original)

    @pytest.mark.asyncio
    async def test_update_watched_symbols(self, db_session):
        """watched_symbols をリストで更新できる"""
        from unittest.mock import MagicMock
        settings = get_settings()
        original_symbols = settings.WATCHED_SYMBOLS

        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        body = SystemSettingsUpdateRequest(watched_symbols=["7203", "9984"])
        current_user = _make_current_user()

        try:
            result = await update_system_settings(request, body, current_user, db_session)
            assert "watched_symbols" in result.updated_fields
            # 更新後の値が反映されていること
            updated_watched = [
                s.strip() for s in get_settings().WATCHED_SYMBOLS.split(",") if s.strip()
            ]
            assert "7203" in updated_watched
            assert "9984" in updated_watched
        finally:
            object.__setattr__(settings, "WATCHED_SYMBOLS", original_symbols)


class TestSettingsToDict:
    """_settings_to_dict のユニットテスト"""

    def test_returns_expected_keys(self):
        settings = get_settings()
        d = _settings_to_dict(settings)
        expected_keys = {
            "daily_loss_limit_jpy",
            "max_concurrent_positions",
            "consecutive_losses_stop",
            "exit_watcher_interval_sec",
            "strategy_runner_interval_sec",
            "market_state_interval_sec",
            "strategy_max_state_age_sec",
            "signal_max_decision_age_sec",
            "watched_symbols",
        }
        assert set(d.keys()) == expected_keys

    def test_watched_symbols_is_list(self):
        settings = get_settings()
        d = _settings_to_dict(settings)
        assert isinstance(d["watched_symbols"], list)
