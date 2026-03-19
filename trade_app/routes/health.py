"""GET /health — ヘルスチェックエンドポイント"""
import logging

import redis.asyncio as redis
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


async def get_redis() -> redis.Redis:
    """Redis クライアント（health チェック用）"""
    from trade_app.main import get_redis_client
    return get_redis_client()


@router.get("/health")
async def health_check(
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    システム全体のヘルスを返す。
    各コンポーネントの状態を確認し、問題があれば503を返す。
    """
    status = {"status": "ok", "components": {}}

    # PostgreSQL チェック
    try:
        await db.execute(text("SELECT 1"))
        status["components"]["postgres"] = "ok"
    except Exception as e:
        logger.error("PostgreSQL ヘルスチェック失敗: %s", e)
        status["components"]["postgres"] = f"error: {e}"
        status["status"] = "degraded"

    # Redis チェック
    try:
        await redis_client.ping()
        status["components"]["redis"] = "ok"
    except Exception as e:
        logger.error("Redis ヘルスチェック失敗: %s", e)
        status["components"]["redis"] = f"error: {e}"
        status["status"] = "degraded"

    from fastapi.responses import JSONResponse
    http_status = 200 if status["status"] == "ok" else 503
    return JSONResponse(content=status, status_code=http_status)
