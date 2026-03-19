"""
HaltManager テスト

halt 発動 / 解除 / 二重防止 / 日次損失 / 連続損失 / DB 正本性を検証する。
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from zoneinfo import ZoneInfo

from trade_app.models.enums import HaltType, OrderStatus, PositionStatus
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.trading_halt import TradingHalt
from trade_app.models.trade_result import TradeResult
from trade_app.services.halt_manager import HaltManager

_JST = ZoneInfo("Asia/Tokyo")


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_settings(daily_loss_limit: float = 50000.0, consecutive_losses: int = 3):
    cfg = MagicMock()
    cfg.DAILY_LOSS_LIMIT_JPY = daily_loss_limit
    cfg.CONSECUTIVE_LOSSES_STOP = consecutive_losses
    return cfg


async def _add_trade_result(db_session, pnl: float):
    """TradeResult を追加するヘルパー。FK のため Position/Order も作成する。"""
    # created_at は JST タイムゾーンを使用: SQLite の文字列比較で
    # HaltManager の today_jst_start（JST）との比較を正しく動作させるため
    now_jst = datetime.now(_JST)
    entry_order = Order(
        id=str(uuid.uuid4()),
        ticker="7203",
        order_type="market",
        side="buy",
        quantity=100,
        status=OrderStatus.FILLED.value,
        filled_quantity=100,
        filled_price=2500.0,
        created_at=now_jst,
        updated_at=now_jst,
    )
    db_session.add(entry_order)
    await db_session.flush()

    position = Position(
        id=str(uuid.uuid4()),
        order_id=entry_order.id,
        ticker="7203",
        side="buy",
        quantity=100,
        entry_price=2500.0,
        status=PositionStatus.CLOSED.value,
        opened_at=now_jst,
        updated_at=now_jst,
    )
    db_session.add(position)
    await db_session.flush()

    exit_price = 2500.0 + pnl / 100
    tr = TradeResult(
        position_id=position.id,
        ticker="7203",
        side="buy",
        entry_price=2500.0,
        exit_price=exit_price,
        quantity=100,
        pnl=pnl,
        pnl_pct=pnl / (2500.0 * 100) * 100,
        exit_reason="tp_hit",
        created_at=now_jst,
    )
    db_session.add(tr)
    await db_session.flush()
    return tr


# ─── manual halt ──────────────────────────────────────────────────────────────

class TestManualHalt:

    @pytest.mark.asyncio
    async def test_activate_manual_halt(self, db_session):
        """manual halt を発動できること"""
        mgr = HaltManager()
        halt = await mgr.activate_halt(
            db=db_session,
            halt_type=HaltType.MANUAL,
            reason="テスト手動停止",
            activated_by="admin",
        )
        assert halt.is_active is True
        assert halt.halt_type == HaltType.MANUAL.value
        assert halt.reason == "テスト手動停止"
        assert halt.activated_by == "admin"

    @pytest.mark.asyncio
    async def test_is_halted_returns_true_after_activate(self, db_session):
        """halt 発動後は is_halted() が True を返す"""
        mgr = HaltManager()
        await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="test")
        is_halted, reason = await mgr.is_halted(db_session)
        assert is_halted is True
        assert "manual" in reason

    @pytest.mark.asyncio
    async def test_is_halted_false_when_no_halt(self, db_session):
        """halt がない場合は is_halted() が False を返す"""
        mgr = HaltManager()
        is_halted, reason = await mgr.is_halted(db_session)
        assert is_halted is False
        assert reason == ""

    @pytest.mark.asyncio
    async def test_deactivate_halt(self, db_session):
        """halt を解除できること"""
        mgr = HaltManager()
        halt = await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="test")
        await db_session.flush()

        deactivated = await mgr.deactivate_halt(db=db_session, halt_id=halt.id)
        assert deactivated.is_active is False
        assert deactivated.deactivated_at is not None

        is_halted, _ = await mgr.is_halted(db_session)
        assert is_halted is False

    @pytest.mark.asyncio
    async def test_deactivate_nonexistent_halt_returns_none(self, db_session):
        """存在しない halt_id の解除は None を返す"""
        mgr = HaltManager()
        result = await mgr.deactivate_halt(db=db_session, halt_id=str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_deactivate_all_halts(self, db_session):
        """全アクティブ halt を一括解除できること"""
        mgr = HaltManager()
        await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="a")
        await mgr.activate_halt(db=db_session, halt_type=HaltType.DAILY_LOSS, reason="b")
        await db_session.flush()

        count = await mgr.deactivate_all_halts(db=db_session)
        assert count == 2

        is_halted, _ = await mgr.is_halted(db_session)
        assert is_halted is False

    @pytest.mark.asyncio
    async def test_get_active_halts(self, db_session):
        """アクティブな halt 一覧を取得できること"""
        mgr = HaltManager()
        await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="x")
        await db_session.flush()

        halts = await mgr.get_active_halts(db_session)
        assert len(halts) == 1
        assert halts[0].is_active is True


# ─── 二重発動防止 ──────────────────────────────────────────────────────────────

class TestHaltDuplicatePrevention:

    @pytest.mark.asyncio
    async def test_same_type_halt_not_duplicated(self, db_session):
        """同一種別の halt を2回発動しても DB に1件しか作成されないこと"""
        mgr = HaltManager()
        h1 = await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="first")
        h2 = await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="second")
        assert h1.id == h2.id  # 同一レコードが返る

        result = await db_session.execute(
            select(TradingHalt).where(
                TradingHalt.halt_type == HaltType.MANUAL.value,
                TradingHalt.is_active == True,  # noqa: E712
            )
        )
        assert len(result.scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_different_types_can_coexist(self, db_session):
        """異なる種別の halt は同時に複数存在できること"""
        mgr = HaltManager()
        await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="a")
        await mgr.activate_halt(db=db_session, halt_type=HaltType.DAILY_LOSS, reason="b")
        await db_session.flush()

        halts = await mgr.get_active_halts(db_session)
        assert len(halts) == 2


# ─── 日次損失 halt ────────────────────────────────────────────────────────────

class TestDailyLossHalt:

    @pytest.mark.asyncio
    async def test_daily_loss_triggers_halt(self, db_session):
        """日次損失が上限を超えると daily_loss halt が発動すること"""
        await _add_trade_result(db_session, pnl=-60000.0)  # 上限 50000 超過
        await db_session.flush()

        mgr = HaltManager()
        settings = _make_settings(daily_loss_limit=50000.0, consecutive_losses=0)
        new_halts = await mgr.check_and_halt_if_needed(db_session, settings=settings)

        assert len(new_halts) == 1
        assert new_halts[0].halt_type == HaltType.DAILY_LOSS.value

    @pytest.mark.asyncio
    async def test_daily_loss_below_limit_no_halt(self, db_session):
        """日次損失が上限以下の場合は halt が発動しないこと"""
        await _add_trade_result(db_session, pnl=-40000.0)
        await db_session.flush()

        mgr = HaltManager()
        settings = _make_settings(daily_loss_limit=50000.0, consecutive_losses=0)
        new_halts = await mgr.check_and_halt_if_needed(db_session, settings=settings)

        assert len(new_halts) == 0

    @pytest.mark.asyncio
    async def test_daily_loss_exact_limit_triggers_halt(self, db_session):
        """日次損失がちょうど上限に達した場合は halt が発動すること"""
        await _add_trade_result(db_session, pnl=-50000.0)
        await db_session.flush()

        mgr = HaltManager()
        settings = _make_settings(daily_loss_limit=50000.0, consecutive_losses=0)
        new_halts = await mgr.check_and_halt_if_needed(db_session, settings=settings)

        assert len(new_halts) == 1
        assert new_halts[0].halt_type == HaltType.DAILY_LOSS.value


# ─── 連続損失 halt ────────────────────────────────────────────────────────────

class TestConsecutiveLossesHalt:

    @pytest.mark.asyncio
    async def test_consecutive_losses_triggers_halt(self, db_session):
        """N連続損失で consecutive_losses halt が発動すること"""
        for pnl in [-1000.0, -2000.0, -3000.0]:
            await _add_trade_result(db_session, pnl=pnl)
        await db_session.flush()

        mgr = HaltManager()
        settings = _make_settings(daily_loss_limit=9999999.0, consecutive_losses=3)
        new_halts = await mgr.check_and_halt_if_needed(db_session, settings=settings)

        assert len(new_halts) == 1
        assert new_halts[0].halt_type == HaltType.CONSECUTIVE_LOSSES.value

    @pytest.mark.asyncio
    async def test_profit_in_streak_breaks_consecutive(self, db_session):
        """連続損失の途中に利益が入ると N連続にならず halt しないこと"""
        await _add_trade_result(db_session, pnl=+500.0)   # 利益
        await _add_trade_result(db_session, pnl=-1000.0)
        await _add_trade_result(db_session, pnl=-2000.0)
        await db_session.flush()

        mgr = HaltManager()
        settings = _make_settings(daily_loss_limit=9999999.0, consecutive_losses=3)
        new_halts = await mgr.check_and_halt_if_needed(db_session, settings=settings)

        # 3件取得して直近2件が損失・1件が利益 → 3連続損失にならない
        assert not any(
            h.halt_type == HaltType.CONSECUTIVE_LOSSES.value for h in new_halts
        )

    @pytest.mark.asyncio
    async def test_consecutive_losses_zero_setting_skips_check(self, db_session):
        """CONSECUTIVE_LOSSES_STOP=0 の場合は連続損失チェック自体をスキップ"""
        for pnl in [-1000.0, -2000.0, -3000.0]:
            await _add_trade_result(db_session, pnl=pnl)
        await db_session.flush()

        mgr = HaltManager()
        settings = _make_settings(daily_loss_limit=9999999.0, consecutive_losses=0)
        new_halts = await mgr.check_and_halt_if_needed(db_session, settings=settings)

        assert not any(
            h.halt_type == HaltType.CONSECUTIVE_LOSSES.value for h in new_halts
        )

    @pytest.mark.asyncio
    async def test_not_enough_trades_for_consecutive(self, db_session):
        """取引件数が N 未満では consecutive halt が発動しないこと"""
        await _add_trade_result(db_session, pnl=-1000.0)
        await _add_trade_result(db_session, pnl=-2000.0)
        await db_session.flush()

        mgr = HaltManager()
        settings = _make_settings(daily_loss_limit=9999999.0, consecutive_losses=3)
        new_halts = await mgr.check_and_halt_if_needed(db_session, settings=settings)

        assert not any(
            h.halt_type == HaltType.CONSECUTIVE_LOSSES.value for h in new_halts
        )


# ─── 非アクティブ halt の扱い ─────────────────────────────────────────────────

class TestInactiveHalt:

    @pytest.mark.asyncio
    async def test_deactivated_halt_not_counted(self, db_session):
        """解除済みの halt は is_halted() に影響しないこと"""
        mgr = HaltManager()
        halt = await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="x")
        await db_session.flush()
        await mgr.deactivate_halt(db=db_session, halt_id=halt.id)
        await db_session.flush()

        is_halted, _ = await mgr.is_halted(db_session)
        assert is_halted is False

    @pytest.mark.asyncio
    async def test_deactivate_already_inactive_halt_is_noop(self, db_session):
        """既に非アクティブな halt を解除しても is_active は変わらない"""
        mgr = HaltManager()
        halt = await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="x")
        await db_session.flush()
        await mgr.deactivate_halt(db=db_session, halt_id=halt.id)
        await db_session.flush()

        # 2回目の deactivate
        result = await mgr.deactivate_halt(db=db_session, halt_id=halt.id)
        assert result is not None
        assert result.is_active is False


# ─── DB 正本性 ────────────────────────────────────────────────────────────────

class TestHaltDBPersistence:

    @pytest.mark.asyncio
    async def test_halt_persisted_in_db(self, db_session):
        """halt が DB に永続化されていること（SELECT で確認）"""
        mgr = HaltManager()
        halt = await mgr.activate_halt(
            db=db_session,
            halt_type=HaltType.MANUAL,
            reason="DB 永続テスト",
        )
        await db_session.flush()

        result = await db_session.execute(
            select(TradingHalt).where(TradingHalt.id == halt.id)
        )
        db_halt = result.scalar_one()
        assert db_halt.reason == "DB 永続テスト"
        assert db_halt.is_active is True

    @pytest.mark.asyncio
    async def test_multiple_halt_types_independently_queryable(self, db_session):
        """種別ごとに独立して DB 検索できること"""
        mgr = HaltManager()
        await mgr.activate_halt(db=db_session, halt_type=HaltType.MANUAL, reason="m")
        await mgr.activate_halt(db=db_session, halt_type=HaltType.DAILY_LOSS, reason="d")
        await db_session.flush()

        manual = await db_session.execute(
            select(TradingHalt).where(
                TradingHalt.halt_type == HaltType.MANUAL.value,
                TradingHalt.is_active == True,  # noqa: E712
            )
        )
        assert len(manual.scalars().all()) == 1

        daily = await db_session.execute(
            select(TradingHalt).where(
                TradingHalt.halt_type == HaltType.DAILY_LOSS.value,
                TradingHalt.is_active == True,  # noqa: E712
            )
        )
        assert len(daily.scalars().all()) == 1
