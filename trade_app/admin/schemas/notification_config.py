"""
NotificationConfig スキーマ (SCR-09)

仕様書: 管理画面仕様書 v0.3 §3(SCR-09)

【destination バリデーション】
channel_type に応じた形式チェックをモデルバリデータで実施する。
- email   : '@' を含み、'@' 以降に '.' が必要（最低限チェック）
- telegram: '@ユーザー名' (英数字・アンダースコア5文字以上) または
            数値チャットID（負値グループIDを含む）
"""
import re
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator

from trade_app.admin.constants import NotificationChannelType, VALID_EVENT_CODES

# Telegram ユーザー名: '@' + 英数字/アンダースコア、5文字以上32文字以下
_TELEGRAM_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
# Telegram チャットID: 整数（グループは負値）
_TELEGRAM_CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")


class NotificationConfigBase(BaseModel):
    channel_type: str = Field(..., description="'email' または 'telegram'")
    destination: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description=(
            "email: メールアドレス形式 / "
            "telegram: '@ユーザー名'（5文字以上）またはチャットID（数値）"
        ),
    )
    is_enabled: bool = Field(default=False)
    events_json: list[str] = Field(
        ...,
        description="通知対象イベントコードの配列。定義済みコードのみ有効。"
    )

    @field_validator("channel_type")
    @classmethod
    def validate_channel_type(cls, v: str) -> str:
        allowed = {e.value for e in NotificationChannelType}
        if v not in allowed:
            raise ValueError(f"channel_type は {allowed} のいずれかを指定してください")
        return v

    @field_validator("events_json")
    @classmethod
    def validate_event_codes(cls, v: list[str]) -> list[str]:
        """定義済みイベントコードのみ許可する"""
        invalid = set(v) - VALID_EVENT_CODES
        if invalid:
            raise ValueError(
                f"無効なイベントコードが含まれています: {invalid}。"
                f"有効なコード: {VALID_EVENT_CODES}"
            )
        # 重複除去
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def validate_destination_format(self) -> "NotificationConfigBase":
        """channel_type に応じた destination 形式チェック"""
        dest = self.destination.strip()
        if self.channel_type == NotificationChannelType.EMAIL.value:
            parts = dest.split("@")
            if len(parts) != 2 or not parts[0] or "." not in parts[1]:
                raise ValueError(
                    "email チャンネルの destination は "
                    "有効なメールアドレス形式（user@example.com）を指定してください"
                )
        elif self.channel_type == NotificationChannelType.TELEGRAM.value:
            if not (
                _TELEGRAM_USERNAME_RE.match(dest)
                or _TELEGRAM_CHAT_ID_RE.match(dest)
            ):
                raise ValueError(
                    "telegram チャンネルの destination は "
                    "'@ユーザー名'（英数字5文字以上）またはチャットID（整数）を指定してください"
                )
        return self


class NotificationConfigCreate(NotificationConfigBase):
    pass


class NotificationConfigUpdate(BaseModel):
    """更新リクエスト。全フィールド省略可能。"""
    destination: str | None = Field(default=None, max_length=256)
    is_enabled: bool | None = None
    events_json: list[str] | None = None

    @field_validator("events_json")
    @classmethod
    def validate_event_codes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        invalid = set(v) - VALID_EVENT_CODES
        if invalid:
            raise ValueError(f"無効なイベントコードが含まれています: {invalid}")
        return list(dict.fromkeys(v))


class NotificationConfigResponse(BaseModel):
    id: str
    channel_type: str
    destination: str
    is_enabled: bool
    events_json: list[str]
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NotificationTestRequest(BaseModel):
    """テスト通知送信リクエスト"""
    config_id: str = Field(..., description="対象 notification_configs.id")


class NotificationTestResponse(BaseModel):
    success: bool
    message: str
    config_id: str
