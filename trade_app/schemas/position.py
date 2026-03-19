"""ポジション関連の Pydantic スキーマ"""
from datetime import datetime

from pydantic import BaseModel


class PositionResponse(BaseModel):
    """GET /api/positions, GET /api/positions/{position_id} のレスポンス"""

    id: str
    order_id: str
    ticker: str
    side: str
    quantity: int
    entry_price: float
    current_price: float | None
    tp_price: float | None
    sl_price: float | None
    exit_deadline: datetime | None
    unrealized_pnl: float | None
    status: str
    exit_price: float | None
    exit_reason: str | None
    realized_pnl: float | None
    opened_at: datetime
    closed_at: datetime | None

    model_config = {"from_attributes": True}
