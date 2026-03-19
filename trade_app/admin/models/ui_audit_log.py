"""
UiAuditLog モデル — 管理画面監査ログ (APPEND ONLY)

仕様書: 管理画面仕様書 v0.3 §6(監査ログ要件), §7(DBエンティティ案 > ui_audit_logs)

【APPEND ONLY 制約】
- UPDATE / DELETE は禁止。アプリケーション層と DB 層の両方で保証する。
- DB 層: migration 時に UPDATE/DELETE を禁止するトリガーを追加する予定（Phase 2）。
- アプリ層: UiAuditLogService.write() 以外からの書き込みを禁止する。

【IP/UA 記録ルール】
- ユーザー起点操作: ip_address / user_agent は必須（None を許可しない）
- システム自動イベント: ip_address / user_agent は null 可
- constants.USER_INITIATED_EVENTS / SYSTEM_INITIATED_EVENTS で分類する

【秘密情報除外】
- before_json / after_json にパスワード・APIキーを含めてはならない。
- UiAuditLogService がサニタイズを実施する。

【trade_db の audit_logs との違い】
- audit_logs: トレーディングエンジンの操作ログ（signal/order/position等）
- ui_audit_logs: 管理画面ユーザーの操作ログ（設定変更・認証等）
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.admin.database import AdminBase


class UiAuditLog(AdminBase):
    """管理画面監査ログテーブル（APPEND ONLY）"""

    __tablename__ = "ui_audit_logs"

    # ─── 主キー ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 操作者情報 ───────────────────────────────────────────────────────────
    # ui_users.id への参照。システム自動イベントは null 可。
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 非正規化: ユーザー削除後も追跡可能にするためメールアドレスを直接保存
    user_email: Mapped[str | None] = mapped_column(String(254), nullable=True)

    # ─── イベント分類 ─────────────────────────────────────────────────────────
    # AdminAuditEventType の値
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # ─── 対象リソース ─────────────────────────────────────────────────────────
    # "symbol" / "strategy" / "broker_config" / "notification" / "system_settings" 等
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 対象リソースのID
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 人間可読ラベル（銘柄コード・戦略名等）
    resource_label: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ─── クライアント情報 ─────────────────────────────────────────────────────
    # ユーザー起点: 必須。システム自動: null 可。
    # constants.USER_INITIATED_EVENTS を参照してバリデーションすること。
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ─── 変更内容 ─────────────────────────────────────────────────────────────
    # 変更前状態（新規作成時は null）。秘密情報を含めてはならない。
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 変更後状態（削除時は null）。秘密情報を含めてはならない。
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 補足説明（任意）
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ─── タイムスタンプ ───────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        Index("ix_ui_audit_logs_user_created", "user_id", "created_at"),
        Index("ix_ui_audit_logs_event_created", "event_type", "created_at"),
        Index("ix_ui_audit_logs_resource", "resource_type", "resource_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<UiAuditLog event={self.event_type} "
            f"resource={self.resource_type}:{self.resource_id} "
            f"user={self.user_email} at={self.created_at}>"
        )
