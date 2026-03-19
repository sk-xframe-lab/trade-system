"""
RiskManager のユニットテスト

RiskManager の責務は「各リスク条件を受けて check() が適切に通過・拒否すること」。

テスト方針:
  - HaltManager.is_halted() は patch して RiskManager 単体テストに集約する
    （halt DB 依存の検証は test_halt_manager.py に委譲）
  - _check_market_hours は市場時間依存のため patch でバイパスする
  - DB に依存するチェック（ポジション数・損失額）のみ実 DB を使用する

チェック項目:
  0. 取引停止チェック（halt が最優先）
  1. 市場時間チェック
  2. 残高チェック
  3. 同時保有ポジション上限チェック
  4. 日次損失上限チェック（TradeResult ベース）
  5. 銘柄集中チェック
  6. 未解決注文チェック（UNKNOWN / PENDING 状態）
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trade_app.brokers.base import BalanceInfo
from trade_app.brokers.mock_broker import MockBrokerAdapter
from trade_app.config import Settings
from trade_app.models.enums import OrderStatus, PositionStatus, SignalStatus
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.signal import TradeSignal
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.risk_manager import RiskManager, RiskRejectedError


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_signal(ticker="7203", quantity=100, limit_price=2850.0, order_type="limit"):
    """テスト用シグナルのファクトリ"""
    return TradeSignal(
        id=str(uuid.uuid4()),
        idempotency_key=str(uuid.uuid4()),
        source_system="test",
        ticker=ticker,
        signal_type="entry",
        order_type=order_type,
        side="buy",
        quantity=quantity,
        limit_price=limit_price,
        generated_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        status=SignalStatus.RECEIVED.value,
    )


def _make_settings(**kwargs):
    """テスト用設定のファクトリ"""
    defaults = {
        "MAX_POSITION_SIZE_PCT": 10.0,
        "MAX_CONCURRENT_POSITIONS": 5,
        "DAILY_LOSS_LIMIT_JPY": 50000.0,
        "CONSECUTIVE_LOSSES_STOP": 3,
        "EXIT_WATCHER_INTERVAL_SEC": 10,
        "BROKER_TYPE": "mock",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "DATABASE_URL_SYNC": "sqlite:///:memory:",
        "REDIS_URL": "redis://localhost",
        "API_TOKEN": "test",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


# _check_market_hours と _check_trading_halt を両方バイパスするデコレータ群
_patch_market_hours = patch(
    "trade_app.services.risk_manager.RiskManager._check_market_hours",
    return_value=None,
)
_patch_halt_not_halted = patch(
    "trade_app.services.halt_manager.HaltManager.is_halted",
    new_callable=AsyncMock,
    return_value=(False, ""),
)


# ─── 0. 取引停止チェック（halt が最優先） ────────────────────────────────────

class TestHaltCheck:
    """halt=True の場合 check() が最優先で失敗すること"""

    @pytest.mark.asyncio
    async def test_halted_raises_immediately(self, db_session):
        """halt=True → check() が他のチェックより先に RiskRejectedError を送出する"""
        broker = MockBrokerAdapter(cash_balance=10_000_000.0)
        audit = AuditLogger(db_session)
        settings = _make_settings()
        rm = RiskManager(db=db_session, broker=broker, audit=audit, settings=settings)

        with patch(
            "trade_app.services.halt_manager.HaltManager.is_halted",
            new_callable=AsyncMock,
            return_value=(True, "manual: テスト停止"),
        ):
            with pytest.raises(RiskRejectedError) as exc_info:
                await rm.check(_make_signal())

        assert "取引停止中" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_halted_does_not_reach_balance_check(self, db_session):
        """halt=True → 残高取得（broker 呼び出し）が行われないこと"""
        broker = MockBrokerAdapter(cash_balance=10_000_000.0)
        # get_balance が呼ばれたら検出できるようにスパイする
        broker.get_balance = AsyncMock(wraps=broker.get_balance)
        audit = AuditLogger(db_session)
        settings = _make_settings()
        rm = RiskManager(db=db_session, broker=broker, audit=audit, settings=settings)

        with patch(
            "trade_app.services.halt_manager.HaltManager.is_halted",
            new_callable=AsyncMock,
            return_value=(True, "manual: テスト停止"),
        ):
            with pytest.raises(RiskRejectedError):
                await rm.check(_make_signal())

        broker.get_balance.assert_not_called()

    @pytest.mark.asyncio
    @_patch_market_hours
    async def test_not_halted_proceeds_to_balance_check(self, mock_hours, db_session):
        """halt=False → 残高チェックへ進む（正常経路）"""
        broker = MockBrokerAdapter(cash_balance=3_000_000.0)
        audit = AuditLogger(db_session)
        settings = _make_settings(MAX_POSITION_SIZE_PCT=10.0)
        rm = RiskManager(db=db_session, broker=broker, audit=audit, settings=settings)

        with _patch_halt_not_halted:
            balance = await rm.check(_make_signal(quantity=100, limit_price=2850.0))

        assert balance.cash_balance == 3_000_000.0

    @pytest.mark.asyncio
    async def test_halt_reason_included_in_error_message(self, db_session):
        """halt 理由が RiskRejectedError のメッセージに含まれること"""
        broker = MockBrokerAdapter(cash_balance=10_000_000.0)
        audit = AuditLogger(db_session)
        rm = RiskManager(db=db_session, broker=broker, audit=audit)

        halt_reason = "daily_loss: 日次損失上限到達"
        with patch(
            "trade_app.services.halt_manager.HaltManager.is_halted",
            new_callable=AsyncMock,
            return_value=(True, halt_reason),
        ):
            with pytest.raises(RiskRejectedError) as exc_info:
                await rm.check(_make_signal())

        assert halt_reason in exc_info.value.reason


# ─── 1. 市場時間チェック ──────────────────────────────────────────────────────

class TestMarketHoursCheck:

    @pytest.mark.asyncio
    async def test_market_hours_boundary_values(self):
        """市場時間のバウンダリ値が正しく設定されていること"""
        from trade_app.services.risk_manager import _MARKET_OPEN_JST, _MARKET_CLOSE_JST
        from datetime import time

        assert _MARKET_OPEN_JST == time(8, 0, 0)
        assert _MARKET_CLOSE_JST == time(15, 35, 0)


# ─── 2. 残高チェック ──────────────────────────────────────────────────────────

class TestPositionSizeCheck:

    @pytest.mark.asyncio
    @_patch_market_hours
    async def test_position_size_ok(self, mock_hours, db_session):
        """発注金額が残高の10%以内は通過すること"""
        broker = MockBrokerAdapter(cash_balance=3_000_000.0)
        audit = AuditLogger(db_session)
        settings = _make_settings(MAX_POSITION_SIZE_PCT=10.0)
        rm = RiskManager(db=db_session, broker=broker, audit=audit, settings=settings)

        with _patch_halt_not_halted:
            # 2850 × 100 = 285,000円 < 300,000円（10%）→ OK
            balance = await rm.check(_make_signal(quantity=100, limit_price=2850.0))

        assert balance.cash_balance == 3_000_000.0

    @pytest.mark.asyncio
    @_patch_market_hours
    async def test_position_size_exceeds_limit(self, mock_hours, db_session):
        """発注金額が残高の10%超は RiskRejectedError になること"""
        broker = MockBrokerAdapter(cash_balance=1_000_000.0)
        audit = AuditLogger(db_session)
        settings = _make_settings(MAX_POSITION_SIZE_PCT=10.0)
        rm = RiskManager(db=db_session, broker=broker, audit=audit, settings=settings)

        with _patch_halt_not_halted:
            # 2850 × 100 = 285,000円 > 100,000円（10%）→ NG
            with pytest.raises(RiskRejectedError) as exc_info:
                await rm.check(_make_signal(quantity=100, limit_price=2850.0))

        assert "発注金額超過" in exc_info.value.reason

    @pytest.mark.asyncio
    @_patch_market_hours
    async def test_position_size_exactly_at_limit_passes(self, mock_hours, db_session):
        """発注金額が残高の10%ちょうどは通過すること（境界値）"""
        # 10% = 300,000円 → limit_price=3000 × qty=100 = 300,000円 (ちょうど)
        broker = MockBrokerAdapter(cash_balance=3_000_000.0)
        audit = AuditLogger(db_session)
        settings = _make_settings(MAX_POSITION_SIZE_PCT=10.0)
        rm = RiskManager(db=db_session, broker=broker, audit=audit, settings=settings)

        with _patch_halt_not_halted:
            balance = await rm.check(_make_signal(quantity=100, limit_price=3000.0))

        assert balance is not None


# ─── 3. 同時保有ポジション上限チェック ──────────────────────────────────────

class TestMaxPositionsCheck:

    @pytest.mark.asyncio
    @_patch_market_hours
    async def test_max_positions_exceeded(self, mock_hours, db_session):
        """オープンポジションが上限以上の場合は RiskRejectedError になること"""
        broker = MockBrokerAdapter(cash_balance=10_000_000.0)
        audit = AuditLogger(db_session)
        settings = _make_settings(MAX_CONCURRENT_POSITIONS=3)

        for i in range(3):
            order = Order(
                id=str(uuid.uuid4()),
                ticker=f"000{i}",
                order_type="limit",
                side="buy",
                quantity=100,
                status=OrderStatus.FILLED.value,
                filled_quantity=100,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db_session.add(order)
            await db_session.flush()
            pos = Position(
                id=str(uuid.uuid4()),
                order_id=order.id,
                ticker=f"000{i}",
                side="buy",
                quantity=100,
                entry_price=1000.0,
                status=PositionStatus.OPEN.value,
                opened_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db_session.add(pos)
        await db_session.flush()

        rm = RiskManager(db=db_session, broker=broker, audit=audit, settings=settings)

        with _patch_halt_not_halted:
            with pytest.raises(RiskRejectedError) as exc_info:
                await rm.check(_make_signal(ticker="9999", quantity=100, limit_price=500.0))

        assert "同時保有ポジション上限超過" in exc_info.value.reason

    @pytest.mark.asyncio
    @_patch_market_hours
    async def test_positions_below_limit_passes(self, mock_hours, db_session):
        """オープンポジションが上限未満なら通過すること"""
        broker = MockBrokerAdapter(cash_balance=10_000_000.0)
        audit = AuditLogger(db_session)
        settings = _make_settings(MAX_CONCURRENT_POSITIONS=5)

        # 2件だけ（上限5件）
        for i in range(2):
            order = Order(
                id=str(uuid.uuid4()),
                ticker=f"111{i}",
                order_type="limit",
                side="buy",
                quantity=100,
                status=OrderStatus.FILLED.value,
                filled_quantity=100,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db_session.add(order)
            await db_session.flush()
            pos = Position(
                id=str(uuid.uuid4()),
                order_id=order.id,
                ticker=f"111{i}",
                side="buy",
                quantity=100,
                entry_price=1000.0,
                status=PositionStatus.OPEN.value,
                opened_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db_session.add(pos)
        await db_session.flush()

        rm = RiskManager(db=db_session, broker=broker, audit=audit, settings=settings)

        with _patch_halt_not_halted:
            balance = await rm.check(_make_signal(ticker="9999", quantity=1, limit_price=100.0))

        assert balance is not None


# ─── 5. 銘柄集中チェック ──────────────────────────────────────────────────────

class TestTickerConcentrationCheck:

    @pytest.mark.asyncio
    @_patch_market_hours
    async def test_ticker_concentration(self, mock_hours, db_session):
        """同一銘柄のオープンポジションが既にある場合は拒否されること"""
        broker = MockBrokerAdapter(cash_balance=10_000_000.0)
        audit = AuditLogger(db_session)

        order = Order(
            id=str(uuid.uuid4()),
            ticker="7203",
            order_type="limit",
            side="buy",
            quantity=100,
            status=OrderStatus.FILLED.value,
            filled_quantity=100,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(order)
        await db_session.flush()
        pos = Position(
            id=str(uuid.uuid4()),
            order_id=order.id,
            ticker="7203",
            side="buy",
            quantity=100,
            entry_price=2850.0,
            status=PositionStatus.OPEN.value,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(pos)
        await db_session.flush()

        rm = RiskManager(db=db_session, broker=broker, audit=audit)

        with _patch_halt_not_halted:
            with pytest.raises(RiskRejectedError) as exc_info:
                await rm.check(_make_signal(ticker="7203"))

        assert "銘柄集中" in exc_info.value.reason

    @pytest.mark.asyncio
    @_patch_market_hours
    async def test_different_ticker_passes_concentration_check(self, mock_hours, db_session):
        """異なる銘柄ならポジションがあっても通過すること"""
        broker = MockBrokerAdapter(cash_balance=10_000_000.0)
        audit = AuditLogger(db_session)

        order = Order(
            id=str(uuid.uuid4()),
            ticker="7203",
            order_type="limit",
            side="buy",
            quantity=100,
            status=OrderStatus.FILLED.value,
            filled_quantity=100,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(order)
        await db_session.flush()
        pos = Position(
            id=str(uuid.uuid4()),
            order_id=order.id,
            ticker="7203",
            side="buy",
            quantity=100,
            entry_price=2850.0,
            status=PositionStatus.OPEN.value,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(pos)
        await db_session.flush()

        rm = RiskManager(db=db_session, broker=broker, audit=audit)

        with _patch_halt_not_halted:
            # 別銘柄 6501 → 通過するはず
            balance = await rm.check(_make_signal(ticker="6501", quantity=1, limit_price=100.0))

        assert balance is not None
