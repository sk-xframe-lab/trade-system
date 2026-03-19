"""Test Order.id at various stages - with all models."""
import asyncio
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from trade_app.models.database import Base
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
from trade_app.models.order import Order
from trade_app.models.enums import OrderStatus


async def test():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, checkfirst=True))

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as session:
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
        print(f"order.id after __init__: {order.id!r}")
        session.add(order)
        print(f"order.id after session.add: {order.id!r}")
        await session.flush()
        print(f"order.id after flush: {order.id!r}")

asyncio.run(test())
