"""Test mapper configuration with all models."""
import trade_app.models.signal
import trade_app.models.order
import trade_app.models.position
import trade_app.models.execution
import trade_app.models.trade_result
import trade_app.models.audit_log
import trade_app.models.broker_request
import trade_app.models.broker_response
import trade_app.models.system_event
import trade_app.models.order_state_transition
import trade_app.models.trading_halt
import trade_app.models.position_exit_transition
import trade_app.models.state_definition
import trade_app.models.state_evaluation
import trade_app.models.current_state_snapshot

from sqlalchemy.orm import configure_mappers
try:
    configure_mappers()
    print("Mapper configuration: OK")
except Exception as e:
    print(f"Mapper configuration ERROR: {e}")

# Test creating an Order
import uuid
from datetime import datetime, timezone
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.enums import OrderStatus, PositionStatus

try:
    order = Order(
        id=str(uuid.uuid4()),
        ticker="7203",
        order_type="market",
        side="buy",
        quantity=100,
        status=OrderStatus.FILLED.value,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    print(f"Order created: id={order.id!r}")

    pos = Position(
        id=str(uuid.uuid4()),
        order_id=order.id,
        ticker="7203",
        side="buy",
        quantity=100,
        entry_price=2500.0,
        status=PositionStatus.OPEN.value,
        opened_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    print(f"Position created: id={pos.id!r}, order_id={pos.order_id!r}")
    pos.id = str(uuid.uuid4())
    print(f"Position id set: {pos.id!r}")
except Exception as e:
    import traceback
    print(f"ERROR: {e}")
    traceback.print_exc()
