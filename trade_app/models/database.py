"""
SQLAlchemy 非同期エンジン・セッションファクトリ・共通Base定義
すべてのモデルはこのBaseを継承すること
"""
import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from trade_app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# ─── 非同期エンジン ────────────────────────────────────────────────────────
# pool_pre_ping=True: 接続切れを自動検知して再接続
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

# ─── セッションファクトリ ─────────────────────────────────────────────────
# expire_on_commit=False: コミット後もオブジェクトにアクセス可能にする
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """全モデル共通の基底クラス"""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI の Depends() で使用する DB セッションジェネレータ。
    リクエスト単位でセッションを生成し、終了時に自動クローズする。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
