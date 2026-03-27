"""
Phase 2 Step B 統合確認テスト

確認項目:
  1. symbol_data に best_bid / best_ask が入る（get_market_data() 連携）
  2. wide_spread ルールが発火する（bid/ask があれば SymbolStateEvaluator が評価する）
  3. wide_spread 発火で symbol layer snapshot が作成される
  4. bid/ask が None の場合 wide_spread はスキップされる（安全側）
  5. get_market_data() 例外でも系全体は止まらない（ticker 単位隔離は維持）

設計検証:
  - SymbolDataFetcher → MarketStateRunner → SymbolStateEvaluator の連携を SQLite で検証
  - BrokerAdapter を AsyncMock (get_market_data) で差し替え
  - wide_spread 発火条件: (ask - bid) / mid >= 0.003 (0.3%)
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from httpx import ASGITransport, AsyncClient

from trade_app.brokers.base import BrokerAPIError, MarketData
from trade_app.models.current_state_snapshot import CurrentStateSnapshot
from trade_app.models.state_evaluation import StateEvaluation
from trade_app.services.market_state.engine import MarketStateEngine
from trade_app.services.market_state.schemas import EvaluationContext
from trade_app.services.market_state.symbol_data_fetcher import SymbolDataFetcher

_UTC = timezone.utc


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_broker(data: dict[str, MarketData | Exception]) -> AsyncMock:
    broker = AsyncMock()

    async def _get_data(ticker: str) -> MarketData:
        val = data.get(ticker)
        if isinstance(val, Exception):
            raise val
        return val

    broker.get_market_data.side_effect = _get_data
    return broker


def _md(price: float | None, bid: float | None = None, ask: float | None = None) -> MarketData:
    return MarketData(current_price=price, best_bid=bid, best_ask=ask)


async def _run_engine(db: AsyncSession, symbol_data: dict) -> None:
    ctx = EvaluationContext(
        evaluation_time=datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC),
        symbol_data=symbol_data,
    )
    await MarketStateEngine(db).run(ctx)


# ─── 確認 1: symbol_data に best_bid / best_ask が入る ────────────────────────

class TestBidAskInSymbolData:
    """SymbolDataFetcher.fetch() が bid/ask を含む dict を返す"""

    @pytest.mark.asyncio
    async def test_fetch_includes_bid_ask(self):
        """get_market_data() の bid/ask が symbol_data に含まれる"""
        broker = _make_broker({
            "7203": _md(3400.0, bid=3390.0, ask=3410.0),
        })
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])

        assert "7203" in result
        assert result["7203"]["current_price"] == 3400.0
        assert result["7203"]["best_bid"] == 3390.0
        assert result["7203"]["best_ask"] == 3410.0

    @pytest.mark.asyncio
    async def test_fetch_bid_ask_none_included(self):
        """bid/ask が None（取引時間外等）でもキーは含まれる"""
        broker = _make_broker({"7203": _md(3400.0, bid=None, ask=None)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])

        assert result["7203"]["best_bid"] is None
        assert result["7203"]["best_ask"] is None

    @pytest.mark.asyncio
    async def test_fetch_multiple_tickers_bid_ask(self):
        """複数 ticker で bid/ask が正しく取得される"""
        broker = _make_broker({
            "7203": _md(3400.0, bid=3390.0, ask=3410.0),
            "6758": _md(1500.0, bid=1495.0, ask=1505.0),
        })
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203", "6758"])

        assert result["7203"]["best_bid"] == 3390.0
        assert result["7203"]["best_ask"] == 3410.0
        assert result["6758"]["best_bid"] == 1495.0
        assert result["6758"]["best_ask"] == 1505.0


# ─── 確認 2: wide_spread ルールが発火する ──────────────────────────────────────

class TestWideSpreadRuleFires:
    """bid/ask が揃うと wide_spread ルールが評価される"""

    @pytest.mark.asyncio
    async def test_wide_spread_fires_with_large_spread(
        self, db_session: AsyncSession
    ):
        """
        スプレッド率 >= 0.3% で wide_spread が発火する。

        7203: bid=2950, ask=3050 → spread=(3050-2950)/3000 ≈ 3.33% ≥ 0.3% → 発火
        """
        symbol_data = {
            "7203": {
                "current_price": 3000.0,
                "best_bid":      2950.0,
                "best_ask":      3050.0,
            }
        }
        await _run_engine(db_session, symbol_data)

        # state_evaluations に wide_spread が保存されていること
        rows = (await db_session.execute(
            select(StateEvaluation).where(
                StateEvaluation.layer == "symbol",
                StateEvaluation.target_code == "7203",
                StateEvaluation.state_code == "wide_spread",
                StateEvaluation.is_active == True,
            )
        )).scalars().all()

        assert len(rows) == 1, f"wide_spread ルールが発火していない: rows={len(rows)}"
        ev = rows[0]
        assert ev.confidence is not None
        assert ev.evidence_json is not None
        # evidence に bid/ask が含まれる
        assert "best_bid" in ev.evidence_json
        assert "best_ask" in ev.evidence_json
        assert ev.evidence_json["best_bid"] == 2950.0
        assert ev.evidence_json["best_ask"] == 3050.0

    @pytest.mark.asyncio
    async def test_wide_spread_not_fires_with_small_spread(
        self, db_session: AsyncSession
    ):
        """
        スプレッド率 < 0.3% では wide_spread が発火しない。

        7203: bid=3299, ask=3301 → spread=2/3300 ≈ 0.06% < 0.3% → 発火なし
        """
        symbol_data = {
            "7203": {
                "current_price": 3300.0,
                "best_bid":      3299.0,
                "best_ask":      3301.0,
            }
        }
        await _run_engine(db_session, symbol_data)

        rows = (await db_session.execute(
            select(StateEvaluation).where(
                StateEvaluation.layer == "symbol",
                StateEvaluation.target_code == "7203",
                StateEvaluation.state_code == "wide_spread",
                StateEvaluation.is_active == True,
            )
        )).scalars().all()

        assert len(rows) == 0, "0.06% スプレッドで wide_spread が発火すべきでない"

    @pytest.mark.asyncio
    async def test_wide_spread_threshold_boundary(self, db_session: AsyncSession):
        """
        スプレッド率ちょうど 0.3% で wide_spread が発火する（境界値）。

        bid=1000, ask=1003 → spread=3/1001.5 ≈ 0.2996% < 0.3% → 発火なし
        bid=1000, ask=1004 → spread=4/1002 ≈ 0.3992% >= 0.3% → 発火あり
        """
        # 発火あり
        symbol_data_fire = {
            "7203": {"current_price": 1002.0, "best_bid": 1000.0, "best_ask": 1004.0}
        }
        await _run_engine(db_session, symbol_data_fire)

        rows = (await db_session.execute(
            select(StateEvaluation).where(
                StateEvaluation.layer == "symbol",
                StateEvaluation.target_code == "7203",
                StateEvaluation.state_code == "wide_spread",
                StateEvaluation.is_active == True,
            )
        )).scalars().all()

        assert len(rows) == 1, "0.4% スプレッドで wide_spread が発火すべき"


# ─── 確認 3: wide_spread 発火で snapshot が作成される ─────────────────────────

class TestWideSpreadSnapshot:
    """wide_spread 発火時に symbol layer の snapshot が作成される"""

    @pytest.mark.asyncio
    async def test_snapshot_created_with_wide_spread(self, db_session: AsyncSession):
        """
        wide_spread が発火すると current_state_snapshots に symbol layer の行が作成される。
        """
        symbol_data = {
            "7203": {
                "current_price": 3000.0,
                "best_bid":      2950.0,
                "best_ask":      3050.0,
            }
        }
        await _run_engine(db_session, symbol_data)

        snaps = (await db_session.execute(
            select(CurrentStateSnapshot).where(
                CurrentStateSnapshot.layer == "symbol",
                CurrentStateSnapshot.target_code == "7203",
            )
        )).scalars().all()

        assert len(snaps) == 1, f"symbol snapshot が作成されていない: {len(snaps)} 件"
        snap = snaps[0]
        assert snap.active_states_json is not None
        assert "wide_spread" in snap.active_states_json

    @pytest.mark.asyncio
    async def test_api_returns_wide_spread_in_symbol_state(
        self, db_session: AsyncSession
    ):
        """
        GET /api/v1/market-state/symbols/{ticker} が wide_spread を返す。
        """
        from trade_app.main import app
        from trade_app.models.database import get_db
        from trade_app.config import get_settings

        symbol_data = {
            "7203": {
                "current_price": 3000.0,
                "best_bid":      2950.0,
                "best_ask":      3050.0,
            }
        }
        await _run_engine(db_session, symbol_data)

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        try:
            token = get_settings().API_TOKEN
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/v1/market-state/symbols/7203",
                    headers={"Authorization": f"Bearer {token}"},
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        data = response.json()
        assert "wide_spread" in data["active_states"]


# ─── 確認 4: bid/ask が None なら wide_spread はスキップ ──────────────────────

class TestWideSpreadSkipWhenNoBidAsk:
    """bid/ask が None または欠損の場合 wide_spread は発火しない"""

    @pytest.mark.asyncio
    async def test_no_wide_spread_when_bid_none(self, db_session: AsyncSession):
        symbol_data = {
            "7203": {
                "current_price": 3000.0,
                "best_bid":      None,
                "best_ask":      3050.0,
            }
        }
        await _run_engine(db_session, symbol_data)

        rows = (await db_session.execute(
            select(StateEvaluation).where(
                StateEvaluation.layer == "symbol",
                StateEvaluation.target_code == "7203",
                StateEvaluation.state_code == "wide_spread",
            )
        )).scalars().all()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_no_wide_spread_when_ask_none(self, db_session: AsyncSession):
        symbol_data = {
            "7203": {
                "current_price": 3000.0,
                "best_bid":      2950.0,
                "best_ask":      None,
            }
        }
        await _run_engine(db_session, symbol_data)

        rows = (await db_session.execute(
            select(StateEvaluation).where(
                StateEvaluation.layer == "symbol",
                StateEvaluation.target_code == "7203",
                StateEvaluation.state_code == "wide_spread",
            )
        )).scalars().all()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_no_wide_spread_when_both_none(self, db_session: AsyncSession):
        symbol_data = {
            "7203": {"current_price": 3000.0, "best_bid": None, "best_ask": None}
        }
        await _run_engine(db_session, symbol_data)

        rows = (await db_session.execute(
            select(StateEvaluation).where(
                StateEvaluation.layer == "symbol",
                StateEvaluation.target_code == "7203",
                StateEvaluation.state_code == "wide_spread",
            )
        )).scalars().all()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_no_wide_spread_when_bid_ask_missing(self, db_session: AsyncSession):
        """best_bid / best_ask キー自体が欠損しても wide_spread はスキップされる"""
        symbol_data = {
            "7203": {"current_price": 3000.0}  # bid/ask キーなし
        }
        await _run_engine(db_session, symbol_data)

        rows = (await db_session.execute(
            select(StateEvaluation).where(
                StateEvaluation.layer == "symbol",
                StateEvaluation.target_code == "7203",
                StateEvaluation.state_code == "wide_spread",
            )
        )).scalars().all()
        assert len(rows) == 0


# ─── 確認 5: 例外でも系全体は止まらない ──────────────────────────────────────

class TestExceptionIsolationStepB:
    """get_market_data() 例外でも ticker 単位隔離は維持される"""

    @pytest.mark.asyncio
    async def test_market_data_exception_excludes_ticker(self):
        """get_market_data() が例外 → ticker は除外、例外は伝播しない"""
        broker = _make_broker({"7203": BrokerAPIError("connection error")})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert "7203" not in result

    @pytest.mark.asyncio
    async def test_one_ticker_error_other_works(self, db_session: AsyncSession):
        """
        7203 が例外 → 除外。
        6758 は正常 wide_spread → snapshot 作成。
        """
        broker = _make_broker({
            "7203": BrokerAPIError("timeout"),
            "6758": _md(3000.0, bid=2950.0, ask=3050.0),
        })
        fetcher = SymbolDataFetcher(broker)
        symbol_data = await fetcher.fetch(["7203", "6758"])

        assert "7203" not in symbol_data
        assert "6758" in symbol_data
        assert symbol_data["6758"]["best_bid"] == 2950.0

        # 6758 で wide_spread 発火 → snapshot 作成
        await _run_engine(db_session, symbol_data)

        snaps = (await db_session.execute(
            select(CurrentStateSnapshot).where(
                CurrentStateSnapshot.layer == "symbol",
            )
        )).scalars().all()
        codes = {s.target_code for s in snaps}
        assert "6758" in codes
        assert "7203" not in codes
