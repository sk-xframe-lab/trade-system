"""Test Position - minimal imports only."""
import uuid
import trade_app.models.position  # Only position model

from trade_app.models.position import Position
from trade_app.models.enums import PositionStatus
from sqlalchemy.orm import configure_mappers

print(f"Position.id.impl before configure: {getattr(Position.id, 'impl', 'N/A')!r}")

try:
    configure_mappers()
    print("configure_mappers: OK")
except Exception as e:
    print(f"configure_mappers ERROR: {e}")

print(f"Position.id.impl after configure: {getattr(Position.id, 'impl', 'N/A')!r}")

pos = Position.__new__(Position)
try:
    pos.id = str(uuid.uuid4())
    print(f"pos.id set OK: {pos.id!r}")
except AttributeError as e:
    print(f"ERROR: {e}")
