"""
管理画面 DB 接続設定 (admin_db)

【DB 分離設計】
管理画面専用テーブル（ui_users, ui_sessions, ui_audit_logs, symbol_configs,
notification_configs）は AdminBase を使用し、trade_db の Base とは分離する。

分離の実現方法:
  - AdminBase: 管理画面専用の DeclarativeBase（trade_db の Base とは別インスタンス）
  - AdminAsyncSessionLocal: admin_db への接続セッションファクトリ
  - 別 Alembic チェーン: alembic_admin/ で独立して migration を管理する
  - 直接 JOIN 禁止: admin_db と trade_db のテーブルを同一クエリで JOIN しない

【Phase 1 単一コンテナ前提】
ADMIN_DATABASE_URL のデフォルト値は trade_db と同一の PostgreSQL サーバー/DB を使用する。

  ┌──────────────────────────────────────────────────┐
  │  Phase 1 (現在): 同一 PostgreSQL インスタンス     │
  │    ADMIN_DATABASE_URL == DATABASE_URL             │
  │    → admin_db テーブルと trade_db テーブルが      │
  │      物理的に同一 DB ファイルに共存               │
  │    → 論理分離（Base 分離・migration 分離）のみ    │
  └──────────────────────────────────────────────────┘
  ┌──────────────────────────────────────────────────┐
  │  将来の物理分離（物理配置は未確定）               │
  │    ADMIN_DATABASE_URL を別ホスト/DB に変更するだけで │
  │    コード変更なしに物理分離が完了する             │
  └──────────────────────────────────────────────────┘

【複数コンテナ非対応（Phase 1 制約）】
system_settings の in-memory 書き換えと同様に、DB 接続も単一コンテナを前提とする。
複数コンテナ環境では各コンテナが独立した admin_db 接続を持つ。
Phase 2 以降でコネクションプーリングやセッション管理を強化する予定。

【get_admin_db vs get_trade_db】
  - get_admin_db(): 管理画面専用テーブルへのアクセスに使用
      認証 (ui_sessions/ui_users), 監査ログ (ui_audit_logs),
      銘柄設定 (symbol_configs), 通知設定 (notification_configs)
  - get_trade_db(): トレードエンジンテーブルへのアクセスに使用
      halt 操作 (trading_halts), ダッシュボード集計 (orders/positions/etc.)
      admin UI がトレードデータを読み取る必要がある場合に使用する
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from trade_app.models.database import AsyncSessionLocal as _TradeSessionLocal


class AdminBase(DeclarativeBase):
    """
    管理画面専用テーブルの DeclarativeBase。
    trade_db の Base（trade_app.models.database.Base）とは分離する。

    全管理画面モデルはこのクラスを継承すること:
        class UiUser(AdminBase): ...
        class UiSession(AdminBase): ...
    """


def _create_admin_engine():
    """admin_db エンジンを生成する（設定ロード後に呼ぶ）"""
    from trade_app.config import get_settings
    settings = get_settings()
    return create_async_engine(
        settings.ADMIN_DATABASE_URL,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=5,
    )


# モジュールロード時にエンジンを生成
_admin_engine = _create_admin_engine()

AdminAsyncSessionLocal = async_sessionmaker(
    bind=_admin_engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_admin_db() -> AsyncSession:  # type: ignore[override]
    """
    管理画面専用 DB (admin_db) セッションの依存関数。
    FastAPI の Depends() で使用する。

    対象テーブル: ui_users, ui_sessions, ui_audit_logs,
                  symbol_configs, notification_configs
    """
    async with AdminAsyncSessionLocal() as db:
        yield db


async def get_trade_db() -> AsyncSession:  # type: ignore[override]
    """
    トレード DB (trade_db) セッションの依存関数。
    管理画面から trade_db テーブルへのアクセスが必要な routes で使用する。

    使用箇所:
      - routes/halt.py: trading_halts テーブルへの読み書き（HaltManager 経由）
      - routes/dashboard.py: orders/positions/halts/trade_results の集計

    直接 JOIN 禁止: admin_db セッションと trade_db セッションをまたいだ
    JOIN クエリは禁止。データ連携は API 経由または非正規化カラムで行う。
    """
    async with _TradeSessionLocal() as db:
        yield db
