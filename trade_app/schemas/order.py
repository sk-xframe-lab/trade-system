"""注文関連の Pydantic スキーマ"""
from datetime import datetime

from pydantic import BaseModel


class OrderResponse(BaseModel):
    """GET /api/orders, GET /api/orders/{order_id} のレスポンス"""

    id: str
    signal_id: str
    broker_order_id: str | None
    ticker: str
    order_type: str
    side: str
    quantity: int
    limit_price: float | None
    status: str
    filled_quantity: int
    filled_price: float | None
    created_at: datetime
    submitted_at: datetime | None
    filled_at: datetime | None

    model_config = {"from_attributes": True}
