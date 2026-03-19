"""
MockBrokerAdapter のユニットテスト
- 発注 → 約定シミュレーション
- キャンセル
- 残高取得
- always_reject モード
"""
import asyncio
import uuid

import pytest

from trade_app.brokers.base import CancelResult, OrderRequest
from trade_app.brokers.mock_broker import FillBehavior, MockBrokerAdapter
from trade_app.models.enums import OrderStatus, OrderType, Side


def _make_order_request(**kwargs) -> OrderRequest:
    defaults = {
        "client_order_id": str(uuid.uuid4()),
        "ticker": "7203",
        "order_type": OrderType.LIMIT,
        "side": Side.BUY,
        "quantity": 100,
        "limit_price": 2850.0,
    }
    defaults.update(kwargs)
    return OrderRequest(**defaults)


@pytest.mark.asyncio
async def test_place_order_returns_submitted():
    """発注直後は SUBMITTED 状態が返ること"""
    broker = MockBrokerAdapter(fill_delay_sec=0.0)
    request = _make_order_request()

    response = await broker.place_order(request)

    assert response.status == OrderStatus.SUBMITTED
    assert response.broker_order_id.startswith("MOCK-")
    assert len(response.broker_order_id) > 5


@pytest.mark.asyncio
async def test_place_order_fills_after_delay():
    """fill_delay_sec 後に FILLED 状態になること"""
    broker = MockBrokerAdapter(fill_delay_sec=0.05)
    request = _make_order_request(limit_price=3000.0)

    response = await broker.place_order(request)
    broker_id = response.broker_order_id

    # 発注直後は SUBMITTED
    status = await broker.get_order_status(broker_id)
    assert status.status == OrderStatus.SUBMITTED

    # 少し待って FILLED に
    await asyncio.sleep(0.2)
    status = await broker.get_order_status(broker_id)
    assert status.status == OrderStatus.FILLED
    assert status.filled_quantity == 100
    assert status.filled_price == 3000.0


@pytest.mark.asyncio
async def test_cancel_order_before_fill():
    """未約定の注文はキャンセルできること"""
    # fill_delay_sec を長くして約定前にキャンセル
    broker = MockBrokerAdapter(fill_delay_sec=60.0)
    request = _make_order_request()

    response = await broker.place_order(request)
    broker_id = response.broker_order_id

    result = await broker.cancel_order(broker_id)
    assert isinstance(result, CancelResult)
    assert result.success is True
    assert result.is_already_terminal is False

    status = await broker.get_order_status(broker_id)
    assert status.status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_filled_order_returns_already_terminal():
    """約定済み注文のキャンセルは success=True, is_already_terminal=True が返ること"""
    broker = MockBrokerAdapter(fill_delay_sec=0.0)
    request = _make_order_request()

    response = await broker.place_order(request)
    await asyncio.sleep(0.2)  # 約定を待つ

    result = await broker.cancel_order(response.broker_order_id)
    assert isinstance(result, CancelResult)
    assert result.success is True
    assert result.is_already_terminal is True


@pytest.mark.asyncio
async def test_cancel_unknown_order_returns_failure():
    """存在しない注文IDのキャンセルは success=False が返ること"""
    broker = MockBrokerAdapter()
    result = await broker.cancel_order("NON-EXISTENT-ID")
    assert isinstance(result, CancelResult)
    assert result.success is False
    assert result.is_already_terminal is False
    assert "NON-EXISTENT-ID" in result.reason


@pytest.mark.asyncio
async def test_cancel_already_cancelled_order_returns_already_terminal():
    """キャンセル済み注文を再キャンセルすると is_already_terminal=True が返ること"""
    broker = MockBrokerAdapter(fill_delay_sec=60.0)
    request = _make_order_request()

    response = await broker.place_order(request)
    broker_id = response.broker_order_id

    # 1回目キャンセル
    result1 = await broker.cancel_order(broker_id)
    assert result1.success is True
    assert result1.is_already_terminal is False

    # 2回目キャンセル（冪等性確認）
    result2 = await broker.cancel_order(broker_id)
    assert result2.success is True
    assert result2.is_already_terminal is True


@pytest.mark.asyncio
async def test_always_reject():
    """always_reject=True のとき発注が REJECTED で返ること"""
    broker = MockBrokerAdapter(always_reject=True)
    request = _make_order_request()

    response = await broker.place_order(request)
    assert response.status == OrderStatus.REJECTED
    assert response.broker_order_id == ""


@pytest.mark.asyncio
async def test_get_balance():
    """残高が設定値で返ること"""
    broker = MockBrokerAdapter(cash_balance=5_000_000.0)
    balance = await broker.get_balance()

    assert balance.cash_balance == 5_000_000.0
    assert balance.margin_available == 15_000_000.0  # 3倍
    assert balance.total_equity == 5_000_000.0


@pytest.mark.asyncio
async def test_market_order_fill_price():
    """成行注文は fill_price=1000（Mockデフォルト）で約定すること"""
    broker = MockBrokerAdapter(fill_delay_sec=0.0)
    request = _make_order_request(
        order_type=OrderType.MARKET,
        limit_price=None,
    )

    response = await broker.place_order(request)
    await asyncio.sleep(0.2)

    status = await broker.get_order_status(response.broker_order_id)
    assert status.status == OrderStatus.FILLED
    assert status.filled_price == 1000.0  # Mock デフォルト価格


@pytest.mark.asyncio
async def test_get_positions_after_fill():
    """約定後はポジション一覧に含まれること"""
    broker = MockBrokerAdapter(fill_delay_sec=0.0)
    request = _make_order_request(ticker="9432", quantity=200, limit_price=4000.0)

    response = await broker.place_order(request)
    await asyncio.sleep(0.2)

    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].ticker == "9432"
    assert positions[0].quantity == 200
    assert positions[0].average_price == 4000.0


# ─── OrderRequest 新フィールドテスト ────────────────────────────────────────

def test_order_request_new_fields_defaults():
    """OrderRequest の新フィールドがデフォルト値を持つこと"""
    req = _make_order_request()
    assert req.stop_price is None
    assert req.time_in_force == "day"
    assert req.account_type == "cash"


def test_order_request_new_fields_custom():
    """OrderRequest の新フィールドをカスタム値で設定できること"""
    req = _make_order_request(
        stop_price=2800.0,
        time_in_force="gtc",
        account_type="margin",
    )
    assert req.stop_price == 2800.0
    assert req.time_in_force == "gtc"
    assert req.account_type == "margin"


# ─── OrderStatusResponse 新フィールドテスト ─────────────────────────────────

def test_order_status_response_new_fields_defaults():
    """OrderStatusResponse の新フィールドがデフォルト値を持つこと"""
    from trade_app.brokers.base import OrderStatusResponse
    resp = OrderStatusResponse(
        broker_order_id="MOCK-001",
        status=OrderStatus.SUBMITTED,
    )
    assert resp.remaining_qty == 0
    assert resp.broker_execution_id is None
    assert resp.cancel_qty == 0


def test_order_status_response_new_fields_custom():
    """OrderStatusResponse の新フィールドをカスタム値で設定できること"""
    from trade_app.brokers.base import OrderStatusResponse
    resp = OrderStatusResponse(
        broker_order_id="MOCK-001",
        status=OrderStatus.PARTIAL,
        filled_quantity=50,
        remaining_qty=50,
        broker_execution_id="EXEC-XYZ",
        cancel_qty=0,
    )
    assert resp.remaining_qty == 50
    assert resp.broker_execution_id == "EXEC-XYZ"
    assert resp.cancel_qty == 0


# ─── 新例外クラステスト ──────────────────────────────────────────────────────

def test_exception_hierarchy():
    """新例外クラスが BrokerAPIError のサブクラスであること"""
    from trade_app.brokers.base import (
        BrokerAPIError,
        BrokerMaintenanceError,
        BrokerRateLimitError,
        BrokerTemporaryError,
    )
    assert issubclass(BrokerTemporaryError, BrokerAPIError)
    assert issubclass(BrokerRateLimitError, BrokerAPIError)
    assert issubclass(BrokerMaintenanceError, BrokerAPIError)


def test_temporary_error_is_caught_as_api_error():
    """BrokerTemporaryError が BrokerAPIError として捕捉できること"""
    from trade_app.brokers.base import BrokerAPIError, BrokerTemporaryError

    with pytest.raises(BrokerAPIError):
        raise BrokerTemporaryError("タイムアウト")


def test_cancel_result_defaults():
    """CancelResult のデフォルト値が正しいこと"""
    result = CancelResult(success=True)
    assert result.success is True
    assert result.reason == ""
    assert result.is_already_terminal is False
