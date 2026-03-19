import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from trade_app.models.database import Base
import trade_app.models.signal
import trade_app.models.order


async def test():
    engine1 = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine1.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine1.dispose()
    print("Engine1 done")

    engine2 = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine2.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine2.dispose()
    print("Engine2 done")


asyncio.run(test())
