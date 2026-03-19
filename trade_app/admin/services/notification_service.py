"""
NotificationConfigService — 通知設定 CRUD サービス

仕様書: 管理画面仕様書 v0.3 §3(SCR-09)

【events_json の保証】
NotificationEventCode で定義されたコードのみを保存する。
自由文字列は Pydantic スキーマ層で拒否されるが、サービス層でも二重チェックする。

【テスト通知】
send_test() は実際の通知送信処理のスタブ。
メール/Telegram の実装は別途通知サービスに委譲する。
TODO: 通知送信サービスの実装
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.constants import VALID_EVENT_CODES
from trade_app.admin.models.notification_config import NotificationConfig
from trade_app.admin.schemas.notification_config import (
    NotificationConfigCreate,
    NotificationConfigUpdate,
    NotificationTestResponse,
)

logger = logging.getLogger(__name__)


class NotificationConfigService:
    """通知設定の CRUD サービス"""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def create(
        self, data: NotificationConfigCreate, created_by: str | None = None
    ) -> tuple[NotificationConfig, dict]:
        """
        通知設定を新規作成する。
        Returns: (作成したNotificationConfig, after_json)
        """
        self._validate_events(data.events_json)

        config = NotificationConfig(
            channel_type=data.channel_type,
            destination=data.destination,
            is_enabled=data.is_enabled,
            events_json=data.events_json,
            created_by=created_by,
            updated_by=created_by,
        )
        self._db.add(config)
        await self._db.flush()

        after_json = self._to_dict(config)
        logger.info("通知設定作成: channel=%s dest=%s", data.channel_type, data.destination)
        return config, after_json

    async def get(self, config_id: str) -> NotificationConfig | None:
        result = await self._db.execute(
            select(NotificationConfig).where(NotificationConfig.id == config_id)
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[NotificationConfig]:
        result = await self._db.execute(
            select(NotificationConfig).order_by(NotificationConfig.created_at)
        )
        return list(result.scalars().all())

    async def update(
        self, config_id: str, data: NotificationConfigUpdate, updated_by: str | None = None
    ) -> tuple[NotificationConfig, dict, dict]:
        """
        通知設定を更新する。
        Returns: (更新したNotificationConfig, before_json, after_json)
        """
        config = await self.get(config_id)
        if config is None:
            raise ValueError(f"通知設定が見つかりません: {config_id}")

        before_json = self._to_dict(config)

        if data.destination is not None:
            config.destination = data.destination
        if data.is_enabled is not None:
            config.is_enabled = data.is_enabled
        if data.events_json is not None:
            self._validate_events(data.events_json)
            config.events_json = data.events_json

        config.updated_by = updated_by
        config.updated_at = datetime.now(timezone.utc)

        after_json = self._to_dict(config)
        logger.info("通知設定更新: id=%s", config_id[:8])
        return config, before_json, after_json

    async def delete(self, config_id: str) -> NotificationConfig:
        """通知設定を物理削除する"""
        config = await self.get(config_id)
        if config is None:
            raise ValueError(f"通知設定が見つかりません: {config_id}")
        await self._db.delete(config)
        return config

    async def send_test(self, config_id: str) -> NotificationTestResponse:
        """
        テスト通知を送信する。
        TODO: メール/Telegram の実際の送信処理を実装すること。
        現在はスタブ（常に成功を返す）。
        """
        config = await self.get(config_id)
        if config is None:
            return NotificationTestResponse(
                success=False,
                message=f"通知設定が見つかりません: {config_id}",
                config_id=config_id,
            )

        if not config.is_enabled:
            return NotificationTestResponse(
                success=False,
                message="この通知設定は無効です。有効化してからテスト送信してください。",
                config_id=config_id,
            )

        # TODO: 実際の送信処理
        # if config.channel_type == "email":
        #     await _send_email(config.destination, "[テスト] 管理画面からのテスト通知")
        # elif config.channel_type == "telegram":
        #     await _send_telegram(config.destination, "[テスト] 管理画面からのテスト通知")

        logger.info(
            "テスト通知送信（スタブ）: channel=%s dest=%s",
            config.channel_type,
            config.destination,
        )
        return NotificationTestResponse(
            success=True,
            message=f"テスト通知を送信しました: {config.channel_type} → {config.destination}",
            config_id=config_id,
        )

    @staticmethod
    def _validate_events(events: list[str]) -> None:
        """定義済みイベントコードのみ許可する（サービス層での二重チェック）"""
        invalid = set(events) - VALID_EVENT_CODES
        if invalid:
            raise ValueError(f"無効なイベントコードが含まれています: {invalid}")

    @staticmethod
    def _to_dict(config: NotificationConfig) -> dict:
        """監査ログ用スナップショット（destination はそのまま記録）"""
        return {
            "id": config.id,
            "channel_type": config.channel_type,
            "destination": config.destination,
            "is_enabled": config.is_enabled,
            "events_json": config.events_json,
        }
