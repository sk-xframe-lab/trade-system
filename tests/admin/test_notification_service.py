"""
NotificationConfigService のテスト

【検証内容】
- create(): 新規作成・無効なイベントコードで ValueError
- get(): ID で取得。存在しない場合は None。
- list_all(): 全件 created_at 昇順
- update(): 変更前後 JSON を返す。
- delete(): 物理削除
- send_test(): 有効設定は success=True（スタブ）。無効設定は success=False。
- _validate_events(): VALID_EVENT_CODES 外のコードで ValueError
"""
import uuid

import pytest

from trade_app.admin.schemas.notification_config import (
    NotificationConfigCreate,
    NotificationConfigUpdate,
)
from trade_app.admin.services.notification_service import NotificationConfigService


def _make_create_data(**kwargs) -> NotificationConfigCreate:
    defaults = dict(
        channel_type="email",
        destination="test@example.com",
        is_enabled=True,
        events_json=["ORDER_FILLED", "ORDER_ERROR"],
    )
    defaults.update(kwargs)
    return NotificationConfigCreate(**defaults)


class TestNotificationConfigCreate:
    @pytest.mark.asyncio
    async def test_create_success(self, db_session):
        svc = NotificationConfigService(db_session)
        config, after_json = await svc.create(_make_create_data())
        assert config.id is not None
        assert config.channel_type == "email"
        assert after_json["destination"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_create_invalid_event_code_raises(self, db_session):
        svc = NotificationConfigService(db_session)
        with pytest.raises(ValueError, match="無効なイベントコード"):
            await svc.create(_make_create_data(events_json=["INVALID_CODE"]))

    @pytest.mark.asyncio
    async def test_create_telegram(self, db_session):
        svc = NotificationConfigService(db_session)
        config, _ = await svc.create(
            _make_create_data(channel_type="telegram", destination="@mychannel")
        )
        assert config.channel_type == "telegram"

    @pytest.mark.asyncio
    async def test_create_disabled_by_default(self, db_session):
        """is_enabled=False で作成できる"""
        svc = NotificationConfigService(db_session)
        config, _ = await svc.create(_make_create_data(is_enabled=False))
        assert config.is_enabled is False


class TestNotificationConfigGet:
    @pytest.mark.asyncio
    async def test_get_existing(self, db_session):
        svc = NotificationConfigService(db_session)
        config, _ = await svc.create(_make_create_data())
        await db_session.flush()

        fetched = await svc.get(config.id)
        assert fetched is not None
        assert fetched.id == config.id

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, db_session):
        svc = NotificationConfigService(db_session)
        result = await svc.get(str(uuid.uuid4()))
        assert result is None


class TestNotificationConfigListAll:
    @pytest.mark.asyncio
    async def test_list_returns_all(self, db_session):
        svc = NotificationConfigService(db_session)
        for dest in ["a@example.com", "b@example.com", "c@example.com"]:
            await svc.create(_make_create_data(destination=dest))
        await db_session.flush()

        configs = await svc.list_all()
        assert len(configs) == 3

    @pytest.mark.asyncio
    async def test_list_empty(self, db_session):
        svc = NotificationConfigService(db_session)
        configs = await svc.list_all()
        assert configs == []


class TestNotificationConfigUpdate:
    @pytest.mark.asyncio
    async def test_update_destination(self, db_session):
        svc = NotificationConfigService(db_session)
        config, _ = await svc.create(_make_create_data(destination="old@example.com"))
        await db_session.flush()

        update = NotificationConfigUpdate(destination="new@example.com")
        updated, before, after = await svc.update(config.id, update)
        assert before["destination"] == "old@example.com"
        assert after["destination"] == "new@example.com"

    @pytest.mark.asyncio
    async def test_update_events_validates_codes(self, db_session):
        svc = NotificationConfigService(db_session)
        config, _ = await svc.create(_make_create_data())
        await db_session.flush()

        with pytest.raises(ValueError, match="無効なイベントコード"):
            await svc.update(config.id, NotificationConfigUpdate(events_json=["FAKE_EVENT"]))

    @pytest.mark.asyncio
    async def test_update_nonexistent_raises(self, db_session):
        svc = NotificationConfigService(db_session)
        with pytest.raises(ValueError, match="見つかりません"):
            await svc.update(str(uuid.uuid4()), NotificationConfigUpdate(is_enabled=False))

    @pytest.mark.asyncio
    async def test_update_is_enabled(self, db_session):
        svc = NotificationConfigService(db_session)
        config, _ = await svc.create(_make_create_data(is_enabled=True))
        await db_session.flush()

        updated, before, after = await svc.update(
            config.id, NotificationConfigUpdate(is_enabled=False)
        )
        assert before["is_enabled"] is True
        assert after["is_enabled"] is False


class TestNotificationConfigDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_record(self, db_session):
        svc = NotificationConfigService(db_session)
        config, _ = await svc.create(_make_create_data())
        await db_session.flush()

        await svc.delete(config.id)
        await db_session.flush()

        assert await svc.get(config.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_raises(self, db_session):
        svc = NotificationConfigService(db_session)
        with pytest.raises(ValueError, match="見つかりません"):
            await svc.delete(str(uuid.uuid4()))


class TestNotificationSendTest:
    @pytest.mark.asyncio
    async def test_send_test_enabled_config_succeeds(self, db_session):
        svc = NotificationConfigService(db_session)
        config, _ = await svc.create(_make_create_data(is_enabled=True))
        await db_session.flush()

        result = await svc.send_test(config.id)
        assert result.success is True
        assert result.config_id == config.id

    @pytest.mark.asyncio
    async def test_send_test_disabled_config_fails(self, db_session):
        svc = NotificationConfigService(db_session)
        config, _ = await svc.create(_make_create_data(is_enabled=False))
        await db_session.flush()

        result = await svc.send_test(config.id)
        assert result.success is False
        assert "無効" in result.message

    @pytest.mark.asyncio
    async def test_send_test_nonexistent_config_fails(self, db_session):
        svc = NotificationConfigService(db_session)
        result = await svc.send_test(str(uuid.uuid4()))
        assert result.success is False
        assert "見つかりません" in result.message


class TestValidateEvents:
    def test_valid_codes_pass(self):
        NotificationConfigService._validate_events(["ORDER_FILLED", "HALT_TRIGGERED"])

    def test_invalid_code_raises(self):
        with pytest.raises(ValueError, match="無効なイベントコード"):
            NotificationConfigService._validate_events(["ORDER_FILLED", "UNKNOWN_CODE"])

    def test_empty_list_passes(self):
        NotificationConfigService._validate_events([])

    def test_all_valid_codes_pass(self):
        from trade_app.admin.constants import VALID_EVENT_CODES
        NotificationConfigService._validate_events(list(VALID_EVENT_CODES))


class TestNotificationDestinationValidation:
    """NotificationConfigBase の destination バリデーション（model_validator）のテスト"""

    def _make(self, **kwargs) -> NotificationConfigCreate:
        defaults = dict(
            channel_type="email",
            destination="test@example.com",
            is_enabled=True,
            events_json=["ORDER_FILLED"],
        )
        defaults.update(kwargs)
        return NotificationConfigCreate(**defaults)

    # ─── email ──────────────────────────────────────────────────────────────

    def test_email_valid_passes(self):
        """有効なメールアドレスは通過する"""
        cfg = self._make(destination="user@example.com")
        assert cfg.destination == "user@example.com"

    def test_email_no_at_sign_raises(self):
        """'@' が含まれないメールアドレスは拒否"""
        with pytest.raises(ValueError, match="メールアドレス形式"):
            self._make(destination="userexample.com")

    def test_email_no_domain_dot_raises(self):
        """ドメイン部に '.' が含まれないメールアドレスは拒否"""
        with pytest.raises(ValueError, match="メールアドレス形式"):
            self._make(destination="user@nodot")

    def test_email_empty_local_part_raises(self):
        """ローカルパートが空のメールアドレスは拒否（@example.com）"""
        with pytest.raises(ValueError, match="メールアドレス形式"):
            self._make(destination="@example.com")

    # ─── telegram username ──────────────────────────────────────────────────

    def test_telegram_valid_username_passes(self):
        """有効な Telegram ユーザー名（@username 5文字以上）は通過"""
        cfg = self._make(channel_type="telegram", destination="@validuser")
        assert cfg.destination == "@validuser"

    def test_telegram_username_too_short_raises(self):
        """@username が5文字未満は拒否（@ab = 2文字）"""
        with pytest.raises(ValueError, match="telegram"):
            self._make(channel_type="telegram", destination="@ab")

    def test_telegram_username_no_at_sign_raises(self):
        """'@' なし文字列（数値でもない）は拒否"""
        with pytest.raises(ValueError, match="telegram"):
            self._make(channel_type="telegram", destination="justusername")

    # ─── telegram chat ID ───────────────────────────────────────────────────

    def test_telegram_positive_chat_id_passes(self):
        """正の数値チャットIDは通過"""
        cfg = self._make(channel_type="telegram", destination="123456789")
        assert cfg.destination == "123456789"

    def test_telegram_negative_group_id_passes(self):
        """負の数値グループIDは通過"""
        cfg = self._make(channel_type="telegram", destination="-1001234567890")
        assert cfg.destination == "-1001234567890"

    def test_telegram_float_like_string_raises(self):
        """小数点を含む文字列はチャットIDとして拒否"""
        with pytest.raises(ValueError, match="telegram"):
            self._make(channel_type="telegram", destination="123.456")
