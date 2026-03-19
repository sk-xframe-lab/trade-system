"""Test if Order.id is set at construction time."""
import asyncio
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from trade_app.models.database import Base
import trade_app.models.signal
import trade_app.models.order
import trade_app.models.position
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.enums import OrderStatus, PositionStatus


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
        print(f"order.id after init: {order.id!r}")
        session.add(order)
        print(f"order.id after add: {order.id!r}")
        await session.flush()
        print(f"order.id after flush: {order.id!r}")

        position = Position(
            order_id=order.id,
            ticker="7203",
            side="buy",
            quantity=100,
            entry_price=2500.0,
            status=PositionStatus.OPEN.value,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        print(f"position.order_id: {position.order_id!r}")
        session.add(position)
        await session.flush()
        print("Position flushed OK")

asyncio.run(test())
