"""
Symbol State Engine テスト (Phase 5)

カバレッジ:
  1. SymbolStateEvaluator — gap_up / gap_down / volume / spread / trend / range / breakout / overextended
  2. 複数状態同時判定 / データ欠損 / 境界値
  3. MarketStateEngine との DB 統合（同一銘柄複数状態の正しい保存）
  4. GET /api/v1/market-state/symbols/{ticker} API
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.services.market_state.engine import MarketStateEngine
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.market_state.schemas import EvaluationContext
from trade_app.services.market_state.symbol_evaluator import SymbolStateEvaluator

# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _ctx(**symbol_data_fields) -> EvaluationContext:
    """ticker="7203" の symbol_data を持つ EvaluationContext を作成するヘルパー"""
    return EvaluationContext(
        evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
        symbol_data={"7203": symbol_data_fields} if symbol_data_fields else {},
    )


def _states(ctx: EvaluationContext) -> list[str]:
    """EvaluationContext から状態コードのリストを返す"""
    evaluator = SymbolStateEvaluator()
    return [r.state_code for r in evaluator.evaluate(ctx)]


def _results(ctx: EvaluationContext):
    """EvaluationContext から StateEvaluationResult のリストを返す"""
    return SymbolStateEvaluator().evaluate(ctx)


# ─── 1. ギャップアップ ────────────────────────────────────────────────────────

class TestGapUp:
    """gap_up_open: (open - prev_close) / prev_close >= 2%"""

    def test_gap_up_exceeds_threshold(self):
        # (3060 - 3000) / 3000 = 2.0% → gap_up_open
        ctx = _ctx(current_open=3060.0, prev_close=3000.0)
        assert "gap_up_open" in _states(ctx)

    def test_gap_up_boundary_exactly_2pct(self):
        # ちょうど2% → gap_up_open (>= なので含む)
        ctx = _ctx(current_open=3060.0, prev_close=3000.0)  # exactly 2%
        assert "gap_up_open" in _states(ctx)

    def test_gap_up_below_threshold(self):
        # 1.9% → gap_up_open なし
        ctx = _ctx(current_open=3057.0, prev_close=3000.0)
        assert "gap_up_open" not in _states(ctx)
        assert "gap_down_open" not in _states(ctx)

    def test_gap_up_evidence(self):
        ctx = _ctx(current_open=3120.0, prev_close=3000.0)
        results = _results(ctx)
        gap_result = next(r for r in results if r.state_code == "gap_up_open")
        assert gap_result.evidence["gap_pct"] == pytest.approx(4.0, abs=0.01)
        assert gap_result.evidence["current_open"] == 3120.0
        assert gap_result.evidence["prev_close"] == 3000.0
        assert gap_result.layer == "symbol"
        assert gap_result.target_type == "symbol"
        assert gap_result.target_code == "7203"

    def test_gap_up_score_scaled(self):
        # 4% gap → score 1.0, 2% gap → score 0.5
        ctx_4pct = _ctx(current_open=3120.0, prev_close=3000.0)
        ctx_2pct = _ctx(current_open=3060.0, prev_close=3000.0)
        r_4pct = next(r for r in _results(ctx_4pct) if r.state_code == "gap_up_open")
        r_2pct = next(r for r in _results(ctx_2pct) if r.state_code == "gap_up_open")
        assert r_4pct.score == pytest.approx(1.0)
        assert r_2pct.score == pytest.approx(0.5)


# ─── 2. ギャップダウン ───────────────────────────────────────────────────────

class TestGapDown:
    """gap_down_open: (open - prev_close) / prev_close <= -2%"""

    def test_gap_down_exceeds_threshold(self):
        # (2940 - 3000) / 3000 = -2.0% → gap_down_open
        ctx = _ctx(current_open=2940.0, prev_close=3000.0)
        assert "gap_down_open" in _states(ctx)

    def test_gap_down_boundary_exactly_minus_2pct(self):
        ctx = _ctx(current_open=2940.0, prev_close=3000.0)
        assert "gap_down_open" in _states(ctx)

    def test_gap_down_below_threshold(self):
        # -1.9% → なし
        ctx = _ctx(current_open=2943.0, prev_close=3000.0)
        assert "gap_down_open" not in _states(ctx)
        assert "gap_up_open" not in _states(ctx)

    def test_gap_down_evidence(self):
        ctx = _ctx(current_open=2880.0, prev_close=3000.0)
        results = _results(ctx)
        gap_result = next(r for r in results if r.state_code == "gap_down_open")
        assert gap_result.evidence["gap_pct"] == pytest.approx(-4.0, abs=0.01)
        assert "rule" in gap_result.evidence


# ─── 3. 相対出来高 ────────────────────────────────────────────────────────────

class TestRelativeVolume:
    """high_relative_volume / low_liquidity"""

    def test_high_volume_above_threshold(self):
        # 200% → high_relative_volume
        ctx = _ctx(current_volume=200_000, avg_volume_same_time=100_000)
        assert "high_relative_volume" in _states(ctx)

    def test_high_volume_exactly_2x(self):
        ctx = _ctx(current_volume=200_000, avg_volume_same_time=100_000)
        assert "high_relative_volume" in _states(ctx)

    def test_high_volume_below_threshold(self):
        # 1.9x → なし
        ctx = _ctx(current_volume=190_000, avg_volume_same_time=100_000)
        assert "high_relative_volume" not in _states(ctx)
        assert "low_liquidity" not in _states(ctx)

    def test_low_liquidity(self):
        # 25% → low_liquidity
        ctx = _ctx(current_volume=25_000, avg_volume_same_time=100_000)
        assert "low_liquidity" in _states(ctx)

    def test_normal_volume_no_state(self):
        # 1.5x → どちらもなし
        ctx = _ctx(current_volume=150_000, avg_volume_same_time=100_000)
        assert "high_relative_volume" not in _states(ctx)
        assert "low_liquidity" not in _states(ctx)

    def test_high_volume_evidence(self):
        ctx = _ctx(current_volume=300_000, avg_volume_same_time=100_000)
        results = _results(ctx)
        r = next(r for r in results if r.state_code == "high_relative_volume")
        assert r.evidence["volume_ratio"] == pytest.approx(3.0)
        assert r.evidence["threshold"] == 2.0


# ─── 4. スプレッド ────────────────────────────────────────────────────────────

class TestSpread:
    """wide_spread: (ask - bid) / mid >= 0.3%"""

    def test_wide_spread(self):
        # bid=2997, ask=3015, mid=3006, spread=18, ratio=18/3006=0.599% → wide_spread
        ctx = _ctx(best_bid=2997.0, best_ask=3015.0)
        assert "wide_spread" in _states(ctx)

    def test_normal_spread(self):
        # bid=2999, ask=3001, mid=3000, spread=2, ratio=2/3000=0.067% → なし
        ctx = _ctx(best_bid=2999.0, best_ask=3001.0)
        assert "wide_spread" not in _states(ctx)

    def test_spread_evidence(self):
        ctx = _ctx(best_bid=2997.0, best_ask=3015.0)
        results = _results(ctx)
        r = next(r for r in results if r.state_code == "wide_spread")
        assert r.evidence["best_bid"] == 2997.0
        assert r.evidence["best_ask"] == 3015.0
        assert r.evidence["threshold"] == 0.003


# ─── 5. トレンド ──────────────────────────────────────────────────────────────

class TestTrend:
    """symbol_trend_up / symbol_trend_down"""

    def test_trend_up(self):
        # price > vwap かつ ma5 > ma20 → symbol_trend_up
        ctx = _ctx(
            current_price=3050.0, vwap=3000.0, ma5=2980.0, ma20=2950.0
        )
        assert "symbol_trend_up" in _states(ctx)
        assert "symbol_trend_down" not in _states(ctx)

    def test_trend_down(self):
        # price < vwap かつ ma5 < ma20 → symbol_trend_down
        ctx = _ctx(
            current_price=2950.0, vwap=3000.0, ma5=2970.0, ma20=2990.0
        )
        assert "symbol_trend_down" in _states(ctx)
        assert "symbol_trend_up" not in _states(ctx)

    def test_mixed_no_trend_price_above_but_ma_inverted(self):
        # price > vwap だが ma5 < ma20 → トレンドなし
        ctx = _ctx(
            current_price=3050.0, vwap=3000.0, ma5=2960.0, ma20=2990.0
        )
        assert "symbol_trend_up" not in _states(ctx)
        assert "symbol_trend_down" not in _states(ctx)

    def test_mixed_no_trend_ma_up_but_price_below(self):
        # price < vwap だが ma5 > ma20 → トレンドなし
        ctx = _ctx(
            current_price=2950.0, vwap=3000.0, ma5=2980.0, ma20=2970.0
        )
        assert "symbol_trend_up" not in _states(ctx)
        assert "symbol_trend_down" not in _states(ctx)

    def test_trend_up_evidence(self):
        ctx = _ctx(
            current_price=3050.0, vwap=3000.0, ma5=2980.0, ma20=2950.0
        )
        results = _results(ctx)
        r = next(r for r in results if r.state_code == "symbol_trend_up")
        assert r.evidence["price_above_vwap"] is True
        assert r.evidence["ma5_above_ma20"] is True
        assert r.confidence == pytest.approx(0.75)


# ─── 6. レンジ ────────────────────────────────────────────────────────────────

class TestRange:
    """symbol_range: トレンドなし かつ ATR / price < 2%"""

    def test_range_when_no_trend_and_low_atr(self):
        # 混在条件（price > vwap だが ma5 < ma20）→ トレンドなし かつ ATR 低水準 → symbol_range
        ctx = _ctx(
            current_price=3010.0, vwap=3000.0,  # price > vwap → price_above_vwap=True
            ma5=2980.0, ma20=2990.0,            # ma5 < ma20  → ma5_above_ma20=False → 混在 → no trend
            atr=30.0,  # 30/3010 ≈ 1% < 2% → symbol_range
        )
        assert "symbol_range" in _states(ctx)

    def test_no_range_when_trending(self):
        # トレンドありなら range は出ない（ATR が低くても）
        ctx = _ctx(
            current_price=3050.0, vwap=3000.0, ma5=2980.0, ma20=2950.0,
            atr=10.0,  # 低 ATR
        )
        assert "symbol_range" not in _states(ctx)

    def test_no_range_when_high_atr(self):
        # トレンドなし でも ATR 高水準 → range なし (volatility_high になる)
        ctx = _ctx(
            current_price=3000.0, vwap=3000.0, ma5=2990.0, ma20=2995.0,
            atr=100.0,  # 100/3000 = 3.33% >= 2% → no range
        )
        assert "symbol_range" not in _states(ctx)


# ─── 7. ブレイクアウト候補 ───────────────────────────────────────────────────

class TestBreakout:
    """breakout_candidate: price > ma20 かつ high_volume かつ ギャップなし"""

    def test_breakout_candidate(self):
        ctx = _ctx(
            current_price=3050.0, ma20=3000.0,
            current_volume=300_000, avg_volume_same_time=100_000,  # 3x → high_volume
            current_open=3010.0, prev_close=3000.0,  # 0.33% gap → not gap
        )
        states = _states(ctx)
        assert "breakout_candidate" in states
        assert "high_relative_volume" in states

    def test_no_breakout_with_gap_up(self):
        # ギャップアップあり → breakout_candidate なし
        ctx = _ctx(
            current_price=3080.0, ma20=3000.0,
            current_volume=300_000, avg_volume_same_time=100_000,
            current_open=3070.0, prev_close=3000.0,  # 2.33% → gap_up
        )
        assert "gap_up_open" in _states(ctx)
        assert "breakout_candidate" not in _states(ctx)

    def test_no_breakout_without_high_volume(self):
        # 出来高が平均以下 → breakout_candidate なし
        ctx = _ctx(
            current_price=3050.0, ma20=3000.0,
            current_volume=100_000, avg_volume_same_time=100_000,  # 1x → not high
            current_open=3010.0, prev_close=3000.0,
        )
        assert "breakout_candidate" not in _states(ctx)

    def test_breakout_evidence(self):
        ctx = _ctx(
            current_price=3090.0, ma20=3000.0,
            current_volume=300_000, avg_volume_same_time=100_000,
            current_open=3010.0, prev_close=3000.0,
        )
        results = _results(ctx)
        r = next(r for r in results if r.state_code == "breakout_candidate")
        assert r.evidence["is_high_volume"] is True
        assert r.evidence["current_price"] == 3090.0
        assert r.evidence["ma20"] == 3000.0


# ─── 8. 過熱（RSI）────────────────────────────────────────────────────────────

class TestOverextended:
    """overextended: rsi >= 75 (overbought) または rsi <= 25 (oversold)"""

    def test_overbought_rsi_above_75(self):
        ctx = _ctx(rsi=80.0)
        states = _states(ctx)
        assert "overextended" in states
        r = next(r for r in _results(ctx) if r.state_code == "overextended")
        assert r.evidence["direction"] == "overbought"
        assert r.evidence["rsi"] == 80.0

    def test_oversold_rsi_below_25(self):
        ctx = _ctx(rsi=20.0)
        states = _states(ctx)
        assert "overextended" in states
        r = next(r for r in _results(ctx) if r.state_code == "overextended")
        assert r.evidence["direction"] == "oversold"

    def test_normal_rsi_no_overextended(self):
        ctx = _ctx(rsi=50.0)
        assert "overextended" not in _states(ctx)

    def test_rsi_boundary_75_exactly(self):
        # RSI == 75 → overextended (>=)
        ctx = _ctx(rsi=75.0)
        assert "overextended" in _states(ctx)

    def test_rsi_boundary_25_exactly(self):
        # RSI == 25 → overextended (<=)
        ctx = _ctx(rsi=25.0)
        assert "overextended" in _states(ctx)


# ─── 9. ボラティリティ ───────────────────────────────────────────────────────

class TestVolatility:
    """symbol_volatility_high: atr / price >= 2%"""

    def test_high_volatility(self):
        ctx = _ctx(current_price=3000.0, atr=90.0)  # 90/3000 = 3% >= 2%
        assert "symbol_volatility_high" in _states(ctx)

    def test_low_volatility(self):
        ctx = _ctx(current_price=3000.0, atr=30.0)  # 30/3000 = 1% < 2%
        assert "symbol_volatility_high" not in _states(ctx)

    def test_volatility_evidence(self):
        ctx = _ctx(current_price=3000.0, atr=90.0)
        results = _results(ctx)
        r = next(r for r in results if r.state_code == "symbol_volatility_high")
        assert r.evidence["atr_ratio"] == pytest.approx(0.03, abs=1e-5)
        assert r.evidence["threshold"] == 0.02


# ─── 10. データ欠損 ───────────────────────────────────────────────────────────

class TestMissingData:
    """データ欠損時のロバスト性"""

    def test_no_symbol_data_returns_empty(self):
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            symbol_data={},
        )
        assert _results(ctx) == []

    def test_partial_data_only_applicable_states(self):
        # RSI のみ → overextended のみ判定（ギャップ・出来高・トレンド等はなし）
        ctx = _ctx(rsi=80.0)
        states = _states(ctx)
        assert "overextended" in states
        assert "gap_up_open" not in states
        assert "gap_down_open" not in states
        assert "high_relative_volume" not in states

    def test_none_values_skipped(self):
        # None フィールドを持つデータ → エラーなし
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            symbol_data={"7203": {"current_price": None, "rsi": 80.0}},
        )
        states = _states(ctx)
        assert "overextended" in states


# ─── 11. 複数状態同時 ────────────────────────────────────────────────────────

class TestMultipleStates:
    """1銘柄に複数の状態が同時に有効"""

    def test_gap_up_and_high_volume_simultaneous(self):
        ctx = _ctx(
            current_open=3060.0, prev_close=3000.0,        # gap_up_open (2%)
            current_volume=300_000, avg_volume_same_time=100_000,  # high_relative_volume
            rsi=78.0,                                       # overextended
        )
        states = _states(ctx)
        assert "gap_up_open" in states
        assert "high_relative_volume" in states
        assert "overextended" in states
        # gap_up があっても breakout_candidate は出ない
        assert "breakout_candidate" not in states

    def test_target_fields_all_set_correctly(self):
        ctx = _ctx(current_open=3060.0, prev_close=3000.0)
        results = _results(ctx)
        for r in results:
            assert r.layer == "symbol"
            assert r.target_type == "symbol"
            assert r.target_code == "7203"

    def test_multiple_symbols_in_context(self):
        """複数銘柄を同一コンテキストで評価"""
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            symbol_data={
                "7203": {"rsi": 80.0},    # overextended
                "9984": {"rsi": 20.0},    # overextended (oversold)
                "6758": {"current_volume": 200_000, "avg_volume_same_time": 100_000},  # high_volume
            },
        )
        results = _results(ctx)
        tickers = {r.target_code for r in results}
        assert "7203" in tickers
        assert "9984" in tickers
        assert "6758" in tickers


# ─── 12. DB 統合テスト ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSymbolStateEngineDB:
    """MarketStateEngine との統合（同一銘柄複数状態の保存正確性）"""

    async def test_engine_saves_multiple_states_for_same_symbol(
        self, db_session: AsyncSession
    ):
        """同一銘柄に gap_up + high_volume の 2 状態が保存されること"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            symbol_data={
                "7203": {
                    "current_open": 3060.0, "prev_close": 3000.0,   # gap_up_open
                    "current_volume": 300_000, "avg_volume_same_time": 100_000,  # high_volume
                }
            },
        )
        results = await engine.run(ctx)

        symbol_results = [r for r in results if r.target_code == "7203"]
        state_codes = {r.state_code for r in symbol_results}
        assert "gap_up_open" in state_codes
        assert "high_relative_volume" in state_codes

    async def test_engine_deactivates_previous_symbol_states(
        self, db_session: AsyncSession
    ):
        """2回実行後、前回の状態が is_active=False になること。
        ctx2 で別の状態を生成することで soft-expiry が走り、ctx1 の overextended が失効する。"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            symbol_data={"7203": {"rsi": 80.0}},  # overextended
        )
        # ctx2: rsi=50 (overextended なし) + wide_spread → wide_spread が保存され、
        # 同 target の全 is_active=True (overextended) が soft-expire される
        ctx2 = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 2, 0, 0, tzinfo=timezone.utc),
            symbol_data={
                "7203": {
                    "rsi": 50.0,                   # overextended なし
                    "best_bid": 2994.0,             # wide_spread 生成
                    "best_ask": 3006.0,             # spread_ratio = 12/3000 = 0.4%
                }
            },
        )
        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=10
        )
        # overextended (ctx1) は is_active=False になっているはず
        overextended_rows = [r for r in history if r.state_code == "overextended"]
        assert len(overextended_rows) == 1
        assert overextended_rows[0].is_active is False
        # wide_spread (ctx2) は is_active=True
        spread_rows = [r for r in history if r.state_code == "wide_spread"]
        assert len(spread_rows) == 1
        assert spread_rows[0].is_active is True

    async def test_engine_creates_symbol_snapshot(
        self, db_session: AsyncSession
    ):
        """エンジン実行後に銘柄スナップショットが作成されること"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            symbol_data={
                "7203": {
                    "current_open": 3060.0, "prev_close": 3000.0,  # gap_up_open
                }
            },
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        snapshot = await repo.get_symbol_snapshot("7203")
        assert snapshot is not None
        assert "gap_up_open" in snapshot.active_states_json

    async def test_multiple_states_not_overwrite_each_other(
        self, db_session: AsyncSession
    ):
        """同一銘柄の複数状態が互いに上書きしないこと（旧バグ防止）"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            symbol_data={
                "7203": {
                    "current_open": 3060.0, "prev_close": 3000.0,    # gap_up_open
                    "current_volume": 300_000, "avg_volume_same_time": 100_000,  # high_volume
                    "rsi": 80.0,                                       # overextended
                }
            },
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        # is_active=True の銘柄評価が 3 件存在すること
        active_evals = await repo.get_symbol_active_evaluations("7203")
        active_codes = {r.state_code for r in active_evals}
        assert "gap_up_open" in active_codes
        assert "high_relative_volume" in active_codes
        assert "overextended" in active_codes


# ─── 13. Symbol API テスト ───────────────────────────────────────────────────

@pytest.fixture
def app_with_db(db_session: AsyncSession):
    from trade_app.main import app
    from trade_app.models.database import get_db

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    from trade_app.config import get_settings
    return {"Authorization": f"Bearer {get_settings().API_TOKEN}"}


@pytest.mark.asyncio
class TestSymbolStateAPI:
    """GET /api/v1/market-state/symbols/{ticker} API テスト"""

    async def test_get_symbol_state_404_when_no_data(self, app_with_db, auth_headers):
        """評価データなし → 404"""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/symbols/9999", headers=auth_headers
            )
        assert resp.status_code == 404

    async def test_get_symbol_state_with_data(
        self, app_with_db, auth_headers, db_session: AsyncSession
    ):
        """評価あり → 200 + active_states / evidence_list 含む"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            symbol_data={
                "7203": {
                    "current_open": 3060.0, "prev_close": 3000.0,  # gap_up_open
                }
            },
        )
        await engine.run(ctx)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/symbols/7203", headers=auth_headers
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticker"] == "7203"
        assert "gap_up_open" in data["active_states"]
        assert isinstance(data["evidence_list"], list)
        assert len(data["evidence_list"]) >= 1
        assert data["updated_at"] is not None

    async def test_get_symbol_state_unauthorized(self, app_with_db):
        """認証なし → 403"""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/symbols/7203",
                headers={"Authorization": "Bearer invalid-token"},
            )
        assert resp.status_code == 403

    async def test_get_symbol_state_multiple_active_states(
        self, app_with_db, auth_headers, db_session: AsyncSession
    ):
        """複数の状態が active_states に全て含まれること"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            symbol_data={
                "7203": {
                    "current_open": 3060.0, "prev_close": 3000.0,    # gap_up_open
                    "current_volume": 300_000, "avg_volume_same_time": 100_000,  # high_volume
                }
            },
        )
        await engine.run(ctx)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/symbols/7203", headers=auth_headers
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "gap_up_open" in data["active_states"]
        assert "high_relative_volume" in data["active_states"]
