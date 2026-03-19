"""
UiAuditLog スキーマ (SCR-12)

仕様書: 管理画面仕様書 v0.3 §3(SCR-12), §6(監査ログ要件)
"""
from datetime import datetime
from pydantic import BaseModel, Field


class AuditLogFilter(BaseModel):
    """監査ログ検索フィルタ"""
    date_from: datetime | None = Field(default=None, description="開始日時（JST表示、UTC保存）")
    date_to: datetime | None = Field(default=None, description="終了日時")
    user_email: str | None = Field(default=None, description="操作者メールアドレス（部分一致）")
    event_type: str | None = Field(default=None, description="イベント種別（完全一致）")
    resource_type: str | None = Field(default=None, description="対象リソース種別")
    resource_id: str | None = Field(default=None, description="対象リソースID（部分一致）")


class AuditLogListItem(BaseModel):
    """監査ログ一覧の1件"""
    id: str
    created_at: datetime
    user_email: str | None
    ip_address: str | None
    event_type: str
    resource_type: str | None
    resource_id: str | None
    resource_label: str | None
    # 変更サマリー（before/afterの主要フィールドの要約）
    change_summary: str | None

    model_config = {"from_attributes": True}


class AuditLogDetail(BaseModel):
    """監査ログ詳細（モーダル表示用）"""
    id: str
    created_at: datetime
    user_id: str | None
    user_email: str | None
    ip_address: str | None
    user_agent: str | None
    event_type: str
    resource_type: str | None
    resource_id: str | None
    resource_label: str | None
    before_json: dict | None
    after_json: dict | None
    description: str | None

    model_config = {"from_attributes": True}


class AuditLogWriteRequest(BaseModel):
    """
    監査ログ書き込みリクエスト（UiAuditLogService.write() の引数）。
    外部から直接呼び出さず、サービス層を経由すること。
    """
    event_type: str
    resource_type: str | None = None
    resource_id: str | None = None
    resource_label: str | None = None
    before_json: dict | None = None
    after_json: dict | None = None
    description: str | None = None
    # ユーザー起点イベントは必須、システム自動は None 可
    ip_address: str | None = None
    user_agent: str | None = None
