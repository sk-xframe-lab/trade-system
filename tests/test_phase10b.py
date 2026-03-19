"""
Phase 10-B 追加テスト

1. BrokerCallLogger.after_cancel — CancelResult 対応
2. orders.cancel_requested_at — ORM カラム存在確認
"""
import uuid
from datetime import datetime, timezone

import pytest

from trade_app.brokers.base import CancelResult


# ─── BrokerCallLogger.after_cancel ────────────────────────────────────────────

class TestBrokerCallLoggerAfterCancel:
    """BrokerCallLogger.after_cancel が CancelResult を正しく記録すること"""

    async def _setup(self, db_session):
        """before_cancel で BrokerRequest を作成して返す"""
        from trade_app.services.broker_call_logger import BrokerCallLogger
        logger = BrokerCallLogger(db_session)
        order_id = str(uuid.uuid4())
        broker_request = await logger.before_cancel(
            order_id=order_id,
            broker_order_id="MOCK-001",
        )
        await db_session.flush()
        return logger, broker_request, order_id

    @pytest.mark.asyncio
    async def test_success_result_recorded(self, db_session):
        """成功 CancelResult が payload に記録されること"""
        from trade_app.models.broker_response import BrokerResponse
        from sqlalchemy import select

        logger, req, order_id = await self._setup(db_session)
        result = CancelResult(success=True)

        resp = await logger.after_cancel(
            broker_request=req,
            order_id=order_id,
            result=result,
        )
        await db_session.flush()

        db_resp = await db_session.execute(
            select(BrokerResponse).where(BrokerResponse.id == resp.id)
        )
        record = db_resp.scalar_one()
        assert record.payload["success"] is True
        assert record.payload["is_already_terminal"] is False
        assert record.payload["reason"] == ""
        assert record.is_error is False

    @pytest.mark.asyncio
    async def test_already_terminal_result_recorded(self, db_session):
        """is_already_terminal=True が payload に記録されること"""
        from trade_app.models.broker_response import BrokerResponse
        from sqlalchemy import select

        logger, req, order_id = await self._setup(db_session)
        result = CancelResult(
            success=True,
            reason="既に FILLED 状態",
            is_already_terminal=True,
        )

        resp = await logger.after_cancel(
            broker_request=req,
            order_id=order_id,
            result=result,
        )
        await db_session.flush()

        db_resp = await db_session.execute(
            select(BrokerResponse).where(BrokerResponse.id == resp.id)
        )
        record = db_resp.scalar_one()
        assert record.payload["success"] is True
        assert record.payload["is_already_terminal"] is True
        assert "FILLED" in record.payload["reason"]

    @pytest.mark.asyncio
    async def test_failure_result_recorded(self, db_session):
        """失敗 CancelResult が payload に記録されること"""
        from trade_app.models.broker_response import BrokerResponse
        from sqlalchemy import select

        logger, req, order_id = await self._setup(db_session)
        result = CancelResult(
            success=False,
            reason="注文が見つかりません: MOCK-999",
        )

        resp = await logger.after_cancel(
            broker_request=req,
            order_id=order_id,
            result=result,
        )
        await db_session.flush()

        db_resp = await db_session.execute(
            select(BrokerResponse).where(BrokerResponse.id == resp.id)
        )
        record = db_resp.scalar_one()
        assert record.payload["success"] is False
        assert record.payload["is_already_terminal"] is False
        assert "MOCK-999" in record.payload["reason"]


# ─── orders.cancel_requested_at ───────────────────────────────────────────────

class TestCancelRequestedAt:
    """orders.cancel_requested_at カラムが ORM と DB に正しく追加されていること"""

    def test_orm_has_cancel_requested_at_attribute(self):
        """Order モデルに cancel_requested_at 属性が存在すること"""
        from trade_app.models.order import Order
        assert hasattr(Order, "cancel_requested_at")

    @pytest.mark.asyncio
    async def test_cancel_requested_at_defaults_to_none(self, db_session):
        """新規 Order の cancel_requested_at は NULL であること"""
        from trade_app.models.order import Order
        from sqlalchemy import select

        order = Order(
            id=str(uuid.uuid4()),
            ticker="7203",
            order_type="limit",
            side="buy",
            quantity=100,
            limit_price=2500.0,
            status="submitted",
            broker_order_id=f"MOCK-{uuid.uuid4().hex[:12].upper()}",
            submitted_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(order)
        await db_session.flush()

        result = await db_session.execute(
            select(Order).where(Order.id == order.id)
        )
        saved = result.scalar_one()
        assert saved.cancel_requested_at is None

    @pytest.mark.asyncio
    async def test_cancel_requested_at_can_be_set(self, db_session):
        """cancel_requested_at に datetime をセットして保存できること"""
        from trade_app.models.order import Order
        from sqlalchemy import select

        now = datetime.now(timezone.utc)
        order = Order(
            id=str(uuid.uuid4()),
            ticker="7203",
            order_type="limit",
            side="buy",
            quantity=100,
            limit_price=2500.0,
            status="submitted",
            broker_order_id=f"MOCK-{uuid.uuid4().hex[:12].upper()}",
            submitted_at=now,
            created_at=now,
            updated_at=now,
            cancel_requested_at=now,
        )
        db_session.add(order)
        await db_session.flush()

        result = await db_session.execute(
            select(Order).where(Order.id == order.id)
        )
        saved = result.scalar_one()
        assert saved.cancel_requested_at is not None

    @pytest.mark.asyncio
    async def test_cancel_requested_at_can_be_updated(self, db_session):
        """cancel_requested_at を後から更新できること（キャンセル要求登録フロー）"""
        from trade_app.models.order import Order
        from sqlalchemy import select

        order = Order(
            id=str(uuid.uuid4()),
            ticker="7203",
            order_type="limit",
            side="buy",
            quantity=100,
            limit_price=2500.0,
            status="submitted",
            broker_order_id=f"MOCK-{uuid.uuid4().hex[:12].upper()}",
            submitted_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(order)
        await db_session.flush()

        # 初期値は NULL
        assert order.cancel_requested_at is None

        # キャンセル要求時に設定
        cancel_time = datetime.now(timezone.utc)
        order.cancel_requested_at = cancel_time
        await db_session.flush()

        result = await db_session.execute(
            select(Order).where(Order.id == order.id)
        )
        saved = result.scalar_one()
        assert saved.cancel_requested_at is not None
