"""
管理画面バックエンドモジュール

仕様書: 管理画面仕様書 v0.3 (Phase 1 凍結版)

【実装状況】
- Phase 1 先行実装済み: 定数・モデル定義・スキーマ・サービス・ルート・DB 分離設計
- 以下は未確定事項により一部スタブ:
  - 認証 (I-4: フロントエンドスタック未確定 / Google OAuth 統合保留)
  - broker_connection_configs (T-1: 秘密情報項目種類未確定, I-3: 暗号化方式未確定)

【DB 分離設計 — 確定済み（2026-03-18）】
admin_db / trade_db の分離は確定済み。実装方針:
  - AdminBase: 管理画面専用 DeclarativeBase（trade_app.admin.database.AdminBase）
  - 別 Alembic チェーン: alembic_admin/ で管理（alembic_admin.ini で実行）
  - get_admin_db(): admin_db セッション（ui_users/ui_sessions/ui_audit_logs/symbol_configs/notification_configs）
  - get_trade_db(): trade_db セッション（trading_halts/orders/positions 等のトレードデータ）
  - 直接 JOIN 禁止: admin_db と trade_db のテーブルをまたぐ SQL JOIN は禁止

【Phase 1 単一コンテナ前提】
ADMIN_DATABASE_URL のデフォルト値は trade_db と同一 PostgreSQL を使用する（物理共有）。
論理分離（AdminBase / 別 migration チェーン）は実装済み。
物理配置（別サーバー / 別 DB 名への分離）は未確定。
ADMIN_DATABASE_URL を変更するだけでコード変更なしに対応可能。

【migration 実行方法】
  alembic -c alembic_admin.ini upgrade head
または
  docker compose exec trade_app alembic -c alembic_admin.ini upgrade head
"""
