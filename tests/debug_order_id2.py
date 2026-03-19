"""Test Order.id at init time, without relationships."""
from datetime import datetime, timezone
from trade_app.models.order import Order
from trade_app.models.enums import OrderStatus

# Test without any session
order = Order(
    signal_id=None,
    ticker="7203",
    order_type="market",
    side="buy",
    quantity=100,
    status=OrderStatus.FILLED.value,
    filled_quantity=100,
    filled_price=2500.0,
    created_at=datetime.now(timezone.utc),
    updated_at=datetime.now(timezone.utc),
)
print(f"order.id at construction: {order.id!r}")
