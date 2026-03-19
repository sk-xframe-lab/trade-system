"""
SignalReceiver のユニットテスト
- 正常受信
- 重複シグナル（Redisヒット）
- 重複シグナル（DBフォールバック）
- バリデーションエラー
"""
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from trade_app.models.enums import SignalStatus
from trade_app.models.signal import TradeSignal
from trade_app.schemas.signal import SignalRequest
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.signal_receiver import DuplicateSignalError, SignalReceiver


def _make_signal_request(**kwargs) -> SignalRequest:
    """テスト用シグナルリクエストのファクトリ"""
    defaults = {
        "ticker": "7203",
        "signal_type": "entry",
        "order_type": "limit",
        "side": "buy",
        "quantity": 100,
        "limit_price": 2850.0,
        "strategy": "test_strategy",
        "score": 80.0,
        "generated_at": datetime(2026, 3, 12, 8, 30, 0, tzinfo=timezone.utc),
    }
    defaults.update(kwargs)
    return SignalRequest(**defaults)


@pytest.mark.asyncio
async def test_receive_success(db_session, mock_redis):
    """正常なシグナルが DB に保存され RECEIVED 状態になること"""
    audit = AuditLogger(db_session)
    receiver = SignalReceiver(db=db_session, redis_client=mock_redis, audit=audit)

    idempotency_key = str(uuid.uuid4())
    request = _make_signal_request()

    signal = await receiver.receive(
        request=request,
        idempotency_key=idempotency_key,
        source_system="stock-analysis-v1",
    )

    assert signal.id is not None
    assert signal.ticker == "7203"
    assert signal.side == "buy"
    assert signal.quantity == 100
    assert signal.limit_price == 2850.0
    assert signal.status == SignalStatus.RECEIVED.value
    assert signal.idempotency_key == idempotency_key
    assert signal.source_system == "stock-analysis-v1"


@pytest.mark.asyncio
async def test_receive_duplicate_via_redis(db_session, mock_redis):
    """同一 Idempotency-Key の2回目送信は DuplicateSignalError になること（Redisヒット）"""
    audit = AuditLogger(db_session)
    receiver = SignalReceiver(db=db_session, redis_client=mock_redis, audit=audit)

    idempotency_key = str(uuid.uuid4())
    request = _make_signal_request()

    # 1回目: 正常受信
    signal1 = await receiver.receive(
        request=request,
        idempotency_key=idempotency_key,
        source_system="stock-analysis-v1",
    )

    # 2回目: 重複エラー
    with pytest.raises(DuplicateSignalError) as exc_info:
        await receiver.receive(
            request=request,
            idempotency_key=idempotency_key,
            source_system="stock-analysis-v1",
        )

    assert exc_info.value.signal_id == signal1.id


@pytest.mark.asyncio
async def test_receive_sets_redis_key(db_session, mock_redis):
    """受信後に Redis に冪等性キーが登録されること"""
    audit = AuditLogger(db_session)
    receiver = SignalReceiver(db=db_session, redis_client=mock_redis, audit=audit)

    idempotency_key = str(uuid.uuid4())
    signal = await receiver.receive(
        request=_make_signal_request(),
        idempotency_key=idempotency_key,
        source_system="test",
    )

    # Redis にキーが登録されているか確認
    redis_value = await mock_redis.get(f"idem:{idempotency_key}")
    assert redis_value is not None
    assert redis_value.decode() == signal.id


@pytest.mark.asyncio
async def test_receive_market_order_no_limit_price(db_session, mock_redis):
    """成行注文では limit_price なしで受信できること"""
    audit = AuditLogger(db_session)
    receiver = SignalReceiver(db=db_session, redis_client=mock_redis, audit=audit)

    signal = await receiver.receive(
        request=_make_signal_request(order_type="market", limit_price=None),
        idempotency_key=str(uuid.uuid4()),
        source_system="test",
    )

    assert signal.order_type == "market"
    assert signal.limit_price is None


def test_signal_request_validation_limit_requires_price():
    """order_type=limit のとき limit_price がないとバリデーションエラーになること"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc_info:
        SignalRequest(
            ticker="7203",
            signal_type="entry",
            order_type="limit",
            side="buy",
            quantity=100,
            limit_price=None,  # ← 必須なのに None
            generated_at=datetime(2026, 3, 12, 8, 30, tzinfo=timezone.utc),
        )
    assert "limit_price" in str(exc_info.value)


def test_signal_request_ticker_must_be_digits():
    """ticker に .T を含む場合はバリデーションエラーになること"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SignalRequest(
            ticker="7203.T",  # ← .T は不可
            signal_type="entry",
            order_type="market",
            side="buy",
            quantity=100,
            generated_at=datetime(2026, 3, 12, 8, 30, tzinfo=timezone.utc),
        )


def test_signal_request_invalid_signal_type():
    """signal_type が 'entry' / 'exit' 以外はエラーになること"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SignalRequest(
            ticker="7203",
            signal_type="unknown",  # ← 不正
            order_type="market",
            side="buy",
            quantity=100,
            generated_at=datetime(2026, 3, 12, 8, 30, tzinfo=timezone.utc),
        )
