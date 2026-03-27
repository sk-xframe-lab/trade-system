"""
Phase C+1 遷移ベース記録テスト

確認項目:
  1. 初回発火 — StateEvaluation が INSERT される（is_active=True, is_new_activation=True）
  2. 継続で INSERT なし — 同じ状態が 2 サイクル続いても DB 行は 1 行のまま
  3. 解除で soft-expire — 状態が消えたサイクルで既存行が is_active=False になる
  4. 再発火で再 INSERT — 解除後に再び同じ状態が発火すると新しい行が INSERT される

設計:
  - engine.run(ctx) が prev_active_states を snapshot から自動ロードする
  - SymbolStateEvaluator が prev_active と比較して is_new_activation を設定する
  - save_evaluations_transitioned が activated のみ INSERT / deactivated のみ soft-expire
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.services.market_state.engine import MarketStateEngine
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.market_state.schemas import EvaluationContext

_UTC = timezone.utc

# wide_spread が発火するデータ: spread_rate = 12/3000 = 0.4% >= 0.3%
_WIDE_SPREAD_DATA = {
    "current_price": 3000.0,
    "best_bid": 2994.0,
    "best_ask": 3006.0,
}

# wide_spread が発火しないデータ（他の状態も発火しない）
_NO_STATE_DATA = {
    "current_price": 1000.0,
    "best_bid": 999.0,
    "best_ask": 1001.0,  # spread_rate = 2/1000 = 0.2% < 0.3%
}


def _ctx(hour: int, symbol_data: dict) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=datetime(2024, 11, 6, hour, 0, 0, tzinfo=_UTC),
        symbol_data=symbol_data,
    )


# ─── テスト 1: 初回発火 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestInitialActivation:
    """初回発火時は StateEvaluation が INSERT される（is_active=True）"""

    async def test_first_fire_inserts_row(self, db_session: AsyncSession):
        """wide_spread が初めて発火 → DB に is_active=True の行が 1 件 INSERT される"""
        engine = MarketStateEngine(db_session)
        ctx = _ctx(1, {"7203": _WIDE_SPREAD_DATA})
        results = await engine.run(ctx)

        # evaluator が返した結果に wide_spread が含まれる
        state_codes = [r.state_code for r in results if r.target_code == "7203"]
        assert "wide_spread" in state_codes

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        active_codes = {r.state_code for r in active}
        assert "wide_spread" in active_codes, "初回発火で wide_spread が is_active=True になるべき"

    async def test_first_fire_is_new_activation_true(self, db_session: AsyncSession):
        """初回発火時は is_new_activation=True（snapshot が存在しないため）"""
        engine = MarketStateEngine(db_session)
        ctx = _ctx(1, {"7203": _WIDE_SPREAD_DATA})
        results = await engine.run(ctx)

        wide_spread_results = [r for r in results if r.state_code == "wide_spread" and r.target_code == "7203"]
        assert len(wide_spread_results) == 1
        assert wide_spread_results[0].is_new_activation is True


# ─── テスト 2: 継続で INSERT なし ─────────────────────────────────────────────

@pytest.mark.asyncio
class TestContinuationNoInsert:
    """同じ状態が 2 サイクル続いても DB 行は 1 行のみ"""

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """
        run1: wide_spread 発火 → 1 行 INSERT
        run2: wide_spread 継続 → INSERT なし（合計 1 行のまま）
        """
        engine = MarketStateEngine(db_session)

        ctx1 = _ctx(1, {"7203": _WIDE_SPREAD_DATA})
        ctx2 = _ctx(2, {"7203": _WIDE_SPREAD_DATA})

        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        wide_rows = [r for r in history if r.state_code == "wide_spread"]
        assert len(wide_rows) == 1, (
            f"継続サイクルで INSERT が発生している。期待1行、実際{len(wide_rows)}行"
        )
        assert wide_rows[0].is_active is True

    async def test_continuation_is_new_activation_false(self, db_session: AsyncSession):
        """
        run1 で発火後、run2 では is_new_activation=False が返される
        """
        engine = MarketStateEngine(db_session)

        ctx1 = _ctx(1, {"7203": _WIDE_SPREAD_DATA})
        await engine.run(ctx1)

        ctx2 = _ctx(2, {"7203": _WIDE_SPREAD_DATA})
        results2 = await engine.run(ctx2)

        wide = [r for r in results2 if r.state_code == "wide_spread" and r.target_code == "7203"]
        assert len(wide) == 1
        assert wide[0].is_new_activation is False, "継続状態は is_new_activation=False であるべき"

    async def test_snapshot_updated_at_refreshed_on_continuation(self, db_session: AsyncSession):
        """継続でも snapshot の updated_at が更新される（stale 検出のため）"""
        engine = MarketStateEngine(db_session)

        ctx1 = _ctx(1, {"7203": _WIDE_SPREAD_DATA})
        await engine.run(ctx1)

        repo = MarketStateRepository(db_session)
        snap1 = await repo.get_symbol_snapshot("7203")
        assert snap1 is not None
        updated_at_1 = snap1.updated_at

        ctx2 = _ctx(2, {"7203": _WIDE_SPREAD_DATA})
        await engine.run(ctx2)

        snap2 = await repo.get_symbol_snapshot("7203")
        assert snap2 is not None
        # updated_at が run2 で更新されている（同一行を更新）
        # SQLite はタイムゾーン情報を保持しないため naive で比較する
        t1 = updated_at_1.replace(tzinfo=None) if updated_at_1.tzinfo else updated_at_1
        t2 = snap2.updated_at.replace(tzinfo=None) if snap2.updated_at.tzinfo else snap2.updated_at
        assert t2 >= t1


# ─── テスト 3: 解除で soft-expire ─────────────────────────────────────────────

@pytest.mark.asyncio
class TestDeactivation:
    """状態が消えたサイクルで is_active=False になる"""

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """
        run1: wide_spread 発火 → is_active=True
        run2: wide_spread なし → is_active=False
        """
        engine = MarketStateEngine(db_session)

        ctx1 = _ctx(1, {"7203": _WIDE_SPREAD_DATA})
        ctx2 = _ctx(2, {"7203": _NO_STATE_DATA})

        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        wide_rows = [r for r in history if r.state_code == "wide_spread"]
        assert len(wide_rows) == 1
        assert wide_rows[0].is_active is False, "解除サイクルで wide_spread が is_active=False になるべき"

    async def test_deactivation_snapshot_has_empty_states(self, db_session: AsyncSession):
        """解除後 snapshot の active_states_json が空リストになる"""
        engine = MarketStateEngine(db_session)

        ctx1 = _ctx(1, {"7203": _WIDE_SPREAD_DATA})
        ctx2 = _ctx(2, {"7203": _NO_STATE_DATA})

        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        snapshot = await repo.get_symbol_snapshot("7203")
        assert snapshot is not None
        assert "wide_spread" not in snapshot.active_states_json


# ─── テスト 4: 再発火で再 INSERT ──────────────────────────────────────────────

@pytest.mark.asyncio
class TestReactivation:
    """解除後に再び同じ状態が発火すると新しい行が INSERT される"""

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """
        run1: wide_spread 発火 (行1, is_active=True)
        run2: wide_spread なし (行1, is_active=False)
        run3: wide_spread 発火 (行2, is_active=True)
        → DB に wide_spread 行が 2 件、最新が is_active=True
        """
        engine = MarketStateEngine(db_session)

        ctx1 = _ctx(1, {"7203": _WIDE_SPREAD_DATA})
        ctx2 = _ctx(2, {"7203": _NO_STATE_DATA})
        ctx3 = _ctx(3, {"7203": _WIDE_SPREAD_DATA})

        await engine.run(ctx1)
        await engine.run(ctx2)
        await engine.run(ctx3)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        wide_rows = [r for r in history if r.state_code == "wide_spread"]
        assert len(wide_rows) == 2, f"再発火で 2 行になるべき、実際 {len(wide_rows)} 行"

        active_rows = [r for r in wide_rows if r.is_active is True]
        inactive_rows = [r for r in wide_rows if r.is_active is False]
        assert len(active_rows) == 1, "最新行のみ is_active=True"
        assert len(inactive_rows) == 1, "古い行は is_active=False"

    async def test_reactivation_is_new_activation_true(self, db_session: AsyncSession):
        """再発火時は is_new_activation=True が返される"""
        engine = MarketStateEngine(db_session)

        ctx1 = _ctx(1, {"7203": _WIDE_SPREAD_DATA})
        ctx2 = _ctx(2, {"7203": _NO_STATE_DATA})
        ctx3 = _ctx(3, {"7203": _WIDE_SPREAD_DATA})

        await engine.run(ctx1)
        await engine.run(ctx2)
        results3 = await engine.run(ctx3)

        wide = [r for r in results3 if r.state_code == "wide_spread" and r.target_code == "7203"]
        assert len(wide) == 1
        assert wide[0].is_new_activation is True, "再発火は is_new_activation=True であるべき"
