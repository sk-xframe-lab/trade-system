"""Test if Python callable defaults apply at init time."""
import uuid
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TestModel1(Base):
    """With String PK."""
    __tablename__ = "test1"
    id: Mapped[str] = mapped_column(
        sa.String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(sa.String(64))


class TestModel2(Base):
    """With PostgreSQL UUID PK."""
    __tablename__ = "test2"
    from sqlalchemy.dialects.postgresql import UUID
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(sa.String(64))


obj1 = TestModel1(name="test")
print(f"TestModel1.id after init: {obj1.id!r}")

obj2 = TestModel2(name="test")
print(f"TestModel2.id after init: {obj2.id!r}")
