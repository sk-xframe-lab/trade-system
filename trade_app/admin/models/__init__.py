"""
管理画面 DB モデル

【DB 分離設計 — 確定済み（2026-03-18）】
このパッケージ内の全モデルは AdminBase（trade_app.admin.database.AdminBase）を継承する。
trade_db の Base（trade_app.models.database.Base）とは分離されている。

migration 管理:
  - 対象チェーン: alembic_admin/（alembic/ とは独立）
  - 実行コマンド: alembic -c alembic_admin.ini upgrade head
  - 初回 migration: alembic_admin/versions/001_admin_initial.py

テスト環境:
  - tests/conftest.py で AdminBase.metadata.create_all() を SQLite インメモリ DB に実行
  - admin_db と trade_db を同一 SQLite DB で代用（テスト環境のみ）
"""
from trade_app.admin.models.ui_user import UiUser
from trade_app.admin.models.ui_session import UiSession
from trade_app.admin.models.ui_audit_log import UiAuditLog
from trade_app.admin.models.symbol_config import SymbolConfig
from trade_app.admin.models.notification_config import NotificationConfig

__all__ = [
    "UiUser",
    "UiSession",
    "UiAuditLog",
    "SymbolConfig",
    "NotificationConfig",
]
