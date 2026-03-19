"""Test Position.__new__ after ALL models imported (like conftest.py)."""
import uuid
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

from trade_app.models.position import Position
from sqlalchemy.orm import configure_mappers

try:
    configure_mappers()
    print("configure_mappers: OK")
except Exception as e:
    print(f"configure_mappers ERROR: {e}")

print(f"Position.id.impl: {getattr(Position.id, 'impl', 'N/A')!r}")

pos = Position.__new__(Position)
try:
    pos.id = str(uuid.uuid4())
    print(f"pos.id set OK: {pos.id!r}")
except AttributeError as e:
    print(f"ERROR: {e}")
