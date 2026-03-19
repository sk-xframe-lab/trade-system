"""
GET /api/orders      — 注文一覧
GET /api/orders/{id} — 注文詳細
"""
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.config import get_settings
from trade_app.models.database import get_db
from trade_app.models.order import Order
from trade_app.schemas.order import OrderResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/orders", tags=["orders"])
settings = get_settings()


def _verify_auth(authorization: str) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="認証エラー")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="無効なトークン")


@router.get("", response_model=list[OrderResponse], summary="注文一覧（新しい順）")
async def list_orders(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
    limit: int = 50,
):
    """直近の注文一覧を返す（最大 limit 件）"""
    _verify_auth(authorization)

    result = await db.execute(
        select(Order).order_by(Order.created_at.desc()).limit(limit)
    )
    return [OrderResponse.model_validate(o) for o in result.scalars().all()]


@router.get("/{order_id}", response_model=OrderResponse, summary="注文詳細")
async def get_order(
    order_id: str,
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(...),
):
    """指定した注文の詳細を返す"""
    _verify_auth(authorization)

    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()

    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"注文が見つかりません: {order_id}",
        )

    return OrderResponse.model_validate(order)
