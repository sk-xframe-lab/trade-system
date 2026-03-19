"""
ExitPolicy テスト

TakeProfitPolicy / StopLossPolicy / TimeStopPolicy の境界値を網羅する。
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from trade_app.models.enums import ExitReason, PositionStatus
from trade_app.models.position import Position
from trade_app.services.exit_policies import (
    DEFAULT_EXIT_POLICIES,
    StopLossPolicy,
    TakeProfitPolicy,
    TimeStopPolicy,
)


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_position(
    side: str = "buy",
    tp_price: float | None = None,
    sl_price: float | None = None,
    exit_deadline: datetime | None = None,
    qty: int = 100,
) -> Position:
    """テスト用 Position（DB 不要・インメモリ）"""
    pos = Position(
        id=str(uuid.uuid4()),
        order_id=str(uuid.uuid4()),
        ticker="7203",
        side=side,
        quantity=qty,
        entry_price=2500.0,
        tp_price=tp_price,
        sl_price=sl_price,
        exit_deadline=exit_deadline,
        status=PositionStatus.OPEN.value,
        remaining_qty=None,
        opened_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    return pos


# ─── TakeProfitPolicy ─────────────────────────────────────────────────────────

class TestTakeProfitPolicy:

    def setup_method(self):
        self.policy = TakeProfitPolicy()

    def test_buy_price_equals_tp_fires(self):
        """BUY: current == tp_price ちょうどで発火する"""
        pos = _make_position(side="buy", tp_price=3000.0)
        assert self.policy.should_exit(pos, 3000.0) is True

    def test_buy_price_above_tp_fires(self):
        """BUY: current > tp_price でも発火する"""
        pos = _make_position(side="buy", tp_price=3000.0)
        assert self.policy.should_exit(pos, 3001.0) is True

    def test_buy_price_below_tp_no_fire(self):
        """BUY: current < tp_price では発火しない"""
        pos = _make_position(side="buy", tp_price=3000.0)
        assert self.policy.should_exit(pos, 2999.9) is False

    def test_sell_price_equals_tp_fires(self):
        """SELL: current == tp_price ちょうどで発火する（下方向 TP）"""
        pos = _make_position(side="sell", tp_price=2000.0)
        assert self.policy.should_exit(pos, 2000.0) is True

    def test_sell_price_below_tp_fires(self):
        """SELL: current < tp_price で発火する"""
        pos = _make_position(side="sell", tp_price=2000.0)
        assert self.policy.should_exit(pos, 1999.0) is True

    def test_sell_price_above_tp_no_fire(self):
        """SELL: current > tp_price では発火しない"""
        pos = _make_position(side="sell", tp_price=2000.0)
        assert self.policy.should_exit(pos, 2001.0) is False

    def test_no_tp_price_no_fire(self):
        """tp_price が None の場合は発火しない"""
        pos = _make_position(side="buy", tp_price=None)
        assert self.policy.should_exit(pos, 9999.0) is False

    def test_price_none_no_fire(self):
        """価格が None の場合は発火しない"""
        pos = _make_position(side="buy", tp_price=3000.0)
        assert self.policy.should_exit(pos, None) is False

    def test_exit_reason_is_tp_hit(self):
        assert self.policy.exit_reason == ExitReason.TP_HIT

    def test_name(self):
        assert self.policy.name == "TakeProfit"


# ─── StopLossPolicy ───────────────────────────────────────────────────────────

class TestStopLossPolicy:

    def setup_method(self):
        self.policy = StopLossPolicy()

    def test_buy_price_equals_sl_fires(self):
        """BUY: current == sl_price ちょうどで発火する"""
        pos = _make_position(side="buy", sl_price=2000.0)
        assert self.policy.should_exit(pos, 2000.0) is True

    def test_buy_price_below_sl_fires(self):
        """BUY: current < sl_price で発火する"""
        pos = _make_position(side="buy", sl_price=2000.0)
        assert self.policy.should_exit(pos, 1999.0) is True

    def test_buy_price_above_sl_no_fire(self):
        """BUY: current > sl_price では発火しない"""
        pos = _make_position(side="buy", sl_price=2000.0)
        assert self.policy.should_exit(pos, 2001.0) is False

    def test_sell_price_equals_sl_fires(self):
        """SELL: current == sl_price ちょうどで発火する（上方向 SL）"""
        pos = _make_position(side="sell", sl_price=3000.0)
        assert self.policy.should_exit(pos, 3000.0) is True

    def test_sell_price_above_sl_fires(self):
        """SELL: current > sl_price で発火する"""
        pos = _make_position(side="sell", sl_price=3000.0)
        assert self.policy.should_exit(pos, 3001.0) is True

    def test_sell_price_below_sl_no_fire(self):
        """SELL: current < sl_price では発火しない"""
        pos = _make_position(side="sell", sl_price=3000.0)
        assert self.policy.should_exit(pos, 2999.0) is False

    def test_no_sl_price_no_fire(self):
        """sl_price が None の場合は発火しない"""
        pos = _make_position(side="buy", sl_price=None)
        assert self.policy.should_exit(pos, 0.0) is False

    def test_price_none_no_fire(self):
        """価格が None の場合は発火しない"""
        pos = _make_position(side="buy", sl_price=2000.0)
        assert self.policy.should_exit(pos, None) is False

    def test_exit_reason_is_sl_hit(self):
        assert self.policy.exit_reason == ExitReason.SL_HIT

    def test_name(self):
        assert self.policy.name == "StopLoss"


# ─── TimeStopPolicy ───────────────────────────────────────────────────────────

class TestTimeStopPolicy:

    def setup_method(self):
        self.policy = TimeStopPolicy()

    def test_past_deadline_fires(self):
        """deadline が過去なら発火する"""
        deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        pos = _make_position(exit_deadline=deadline)
        assert self.policy.should_exit(pos, None) is True

    def test_past_deadline_by_1_hour_fires(self):
        """deadline が1時間前でも発火する"""
        deadline = datetime.now(timezone.utc) - timedelta(hours=1)
        pos = _make_position(exit_deadline=deadline)
        assert self.policy.should_exit(pos, 2500.0) is True

    def test_future_deadline_no_fire(self):
        """deadline が未来なら発火しない"""
        deadline = datetime.now(timezone.utc) + timedelta(hours=1)
        pos = _make_position(exit_deadline=deadline)
        assert self.policy.should_exit(pos, None) is False

    def test_no_deadline_no_fire(self):
        """exit_deadline が None の場合は発火しない"""
        pos = _make_position(exit_deadline=None)
        assert self.policy.should_exit(pos, None) is False

    def test_fires_even_when_price_none(self):
        """価格 None でも時間切れなら発火する"""
        deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        pos = _make_position(exit_deadline=deadline)
        assert self.policy.should_exit(pos, None) is True

    def test_naive_deadline_treated_as_utc(self):
        """tzinfo なし deadline は UTC として扱い、過去なら発火する"""
        deadline = datetime.utcnow() - timedelta(seconds=1)  # naive
        assert deadline.tzinfo is None
        pos = _make_position(exit_deadline=deadline)
        assert self.policy.should_exit(pos, None) is True

    def test_exit_reason_is_timeout(self):
        assert self.policy.exit_reason == ExitReason.TIMEOUT

    def test_name(self):
        assert self.policy.name == "TimeStop"


# ─── DEFAULT_EXIT_POLICIES ────────────────────────────────────────────────────

class TestDefaultExitPolicies:

    def test_default_has_three_policies(self):
        assert len(DEFAULT_EXIT_POLICIES) == 3

    def test_default_order_tp_sl_timestop(self):
        names = [p.name for p in DEFAULT_EXIT_POLICIES]
        assert names == ["TakeProfit", "StopLoss", "TimeStop"]

    def test_tp_fires_before_sl_check(self):
        """TP と SL が両方成立するケース: TP が先に評価されるため TP_HIT が返る"""
        pos = _make_position(side="buy", tp_price=2400.0, sl_price=2400.0)
        # current_price = 2400 → BUY: TP(>=2400)=True が先に評価される
        for policy in DEFAULT_EXIT_POLICIES:
            if policy.should_exit(pos, 2400.0):
                assert policy.exit_reason == ExitReason.TP_HIT
                break
        else:
            pytest.fail("どのポリシーも発火しなかった")

    def test_timestop_fires_regardless_of_price_none(self):
        """価格が None でも TimeStop は発火する"""
        deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        pos = _make_position(tp_price=3000.0, sl_price=2000.0, exit_deadline=deadline)
        fired = [p for p in DEFAULT_EXIT_POLICIES if p.should_exit(pos, None)]
        assert len(fired) == 1
        assert fired[0].exit_reason == ExitReason.TIMEOUT
