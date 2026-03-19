"""
Market State Engine テスト

カバレッジ:
  1. TimeWindowStateEvaluator — 各時間帯の正確な判定（JST タイムゾーン）
  2. MarketStateEvaluator    — trend_up / trend_down / range の判定
  3. MarketStateEngine.run() — DB 保存・スナップショット更新
  4. GET /api/v1/market-state/current — 現在状態 API
  5. GET /api/v1/market-state/history — 履歴 API
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.services.market_state.schemas import EvaluationContext
from trade_app.services.market_state.time_window_evaluator import TimeWindowStateEvaluator
from trade_app.services.market_state.market_evaluator import MarketStateEvaluator
from trade_app.services.market_state.engine import MarketStateEngine
from trade_app.services.market_state.repository import MarketStateRepository

_JST = ZoneInfo("Asia/Tokyo")


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _jst_ctx(h: int, m: int, s: int = 0) -> EvaluationContext:
    """指定した JST 時刻で EvaluationContext を作成する（UTC に変換して渡す）"""
    jst_dt = datetime(2024, 11, 6, h, m, s, tzinfo=_JST)
    utc_dt = jst_dt.astimezone(timezone.utc)
    return EvaluationContext(evaluation_time=utc_dt)


# ─── TimeWindowStateEvaluator ─────────────────────────────────────────────────

class TestTimeWindowStateEvaluator:
    """時間帯判定の境界値テスト"""

    def setup_method(self):
        self.evaluator = TimeWindowStateEvaluator()

    def _state(self, h: int, m: int, s: int = 0) -> str:
        results = self.evaluator.evaluate(_jst_ctx(h, m, s))
        assert len(results) == 1
        return results[0].state_code

    def test_pre_open(self):
        assert self._state(8, 0) == "pre_open"
        assert self._state(8, 30) == "pre_open"
        assert self._state(8, 59, 59) == "pre_open"

    def test_opening_auction_risk(self):
        assert self._state(9, 0) == "opening_auction_risk"
        assert self._state(9, 7) == "opening_auction_risk"
        assert self._state(9, 14, 59) == "opening_auction_risk"

    def test_morning_trend_zone(self):
        assert self._state(9, 15) == "morning_trend_zone"
        assert self._state(10, 0) == "morning_trend_zone"
        assert self._state(11, 29, 59) == "morning_trend_zone"

    def test_midday_low_liquidity(self):
        assert self._state(11, 30) == "midday_low_liquidity"
        assert self._state(12, 0) == "midday_low_liquidity"
        assert self._state(12, 29, 59) == "midday_low_liquidity"

    def test_afternoon_repricing_zone(self):
        assert self._state(12, 30) == "afternoon_repricing_zone"
        assert self._state(12, 44, 59) == "afternoon_repricing_zone"

    def test_afternoon_main_returns_morning_trend_zone(self):
        # 12:45〜14:50 は morning_trend_zone (sub_zone=afternoon_main)
        assert self._state(12, 45) == "morning_trend_zone"
        assert self._state(13, 30) == "morning_trend_zone"
        assert self._state(14, 49, 59) == "morning_trend_zone"

    def test_closing_cleanup_zone(self):
        assert self._state(14, 50) == "closing_cleanup_zone"
        assert self._state(15, 15) == "closing_cleanup_zone"
        assert self._state(15, 30) == "closing_cleanup_zone"

    def test_after_hours_morning(self):
        assert self._state(7, 59) == "after_hours"
        assert self._state(0, 0) == "after_hours"

    def test_after_hours_evening(self):
        assert self._state(15, 31) == "after_hours"
        assert self._state(23, 59) == "after_hours"

    def test_result_fields(self):
        results = self.evaluator.evaluate(_jst_ctx(10, 0))
        r = results[0]
        assert r.layer == "time_window"
        assert r.target_type == "time_window"
        assert r.target_code is None
        assert r.score == 1.0
        assert r.confidence == 1.0
        assert "jst_time" in r.evidence
        assert "evaluation_time_utc" in r.evidence

    def test_naive_datetime_treated_as_utc(self):
        """naive datetime は UTC として扱われること"""
        # 00:00 UTC = 09:00 JST → opening_auction_risk
        ctx = EvaluationContext(evaluation_time=datetime(2024, 11, 6, 0, 0, 0))
        results = self.evaluator.evaluate(ctx)
        assert results[0].state_code == "opening_auction_risk"


# ─── MarketStateEvaluator ─────────────────────────────────────────────────────

class TestMarketStateEvaluator:
    """市場トレンド判定テスト"""

    def setup_method(self):
        self.evaluator = MarketStateEvaluator()

    def _ctx(self, change_pct: float | None, index_name: str = "TOPIX") -> EvaluationContext:
        market_data = {}
        if change_pct is not None:
            market_data["index_change_pct"] = change_pct
            market_data["index_name"] = index_name
        return EvaluationContext(
            evaluation_time=datetime.now(timezone.utc),
            market_data=market_data,
        )

    def _eval(self, change_pct: float | None):
        results = self.evaluator.evaluate(self._ctx(change_pct))
        assert len(results) == 1
        return results[0]

    def test_trend_up_above_threshold(self):
        r = self._eval(0.6)
        assert r.state_code == "trend_up"
        assert r.layer == "market"
        assert 0.0 < r.confidence <= 1.0

    def test_trend_up_boundary(self):
        # 0.5% は trend_up の閾値を超えないため range
        r = self._eval(0.5)
        assert r.state_code == "range"

    def test_trend_down_below_threshold(self):
        r = self._eval(-0.6)
        assert r.state_code == "trend_down"
        assert 0.0 < r.confidence <= 1.0

    def test_trend_down_boundary(self):
        r = self._eval(-0.5)
        assert r.state_code == "range"

    def test_range_within_thresholds(self):
        r = self._eval(0.0)
        assert r.state_code == "range"
        assert r.confidence == 0.7

    def test_no_data_defaults_to_range(self):
        r = self._eval(None)
        assert r.state_code == "range"
        assert r.confidence == 0.3
        assert "not provided" in r.evidence["reason"]

    def test_confidence_capped_at_1(self):
        r = self._eval(5.0)  # 5% → confidence would be 2.5 without cap
        assert r.confidence == 1.0

    def test_result_fields(self):
        r = self._eval(0.8)
        assert r.target_type == "market"
        assert r.target_code is None
        assert "index_change_pct" in r.evidence
        assert "thresholds" in r.evidence


# ─── MarketStateEngine ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestMarketStateEngine:
    """エンジンの DB 保存・スナップショット更新テスト"""

    async def test_run_saves_evaluations(self, db_session: AsyncSession):
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 0, 0, 0, tzinfo=timezone.utc),
            market_data={"index_change_pct": 0.8, "index_name": "TOPIX"},
        )
        results = await engine.run(ctx)

        # デフォルト Evaluator は 2つ (time_window + market)
        assert len(results) == 2
        layers = {r.layer for r in results}
        assert "time_window" in layers
        assert "market" in layers

    async def test_run_creates_snapshots(self, db_session: AsyncSession):
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        snapshots = await repo.get_current_states()
        assert len(snapshots) == 2  # time_window + market

    async def test_run_deactivates_previous_evaluations(self, db_session: AsyncSession):
        """2回実行したとき、古い評価が is_active=False になること"""
        engine = MarketStateEngine(db_session)
        ctx1 = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            market_data={"index_change_pct": 0.8},
        )
        ctx2 = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 2, 0, 0, tzinfo=timezone.utc),
            market_data={"index_change_pct": -0.8},
        )
        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="market", limit=10)
        assert len(history) == 2
        active = [r for r in history if r.is_active]
        inactive = [r for r in history if not r.is_active]
        assert len(active) == 1
        assert len(inactive) == 1
        assert active[0].state_code == "trend_down"

    async def test_run_updates_snapshot_on_second_run(self, db_session: AsyncSession):
        """2回実行したとき、スナップショットが更新されること"""
        engine = MarketStateEngine(db_session)
        ctx1 = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            market_data={"index_change_pct": 0.8},
        )
        ctx2 = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 2, 0, 0, tzinfo=timezone.utc),
            market_data={"index_change_pct": -0.8},
        )
        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        snapshots = await repo.get_current_states(layers=["market"])
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert "trend_down" in snap.active_states_json
        assert snap.state_summary_json["primary_state"] == "trend_down"

    async def test_run_evidence_always_saved(self, db_session: AsyncSession):
        """evidence_json が必ず保存されること"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 0, 30, 0, tzinfo=timezone.utc),
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history()
        for row in history:
            assert row.evidence_json is not None
            assert isinstance(row.evidence_json, dict)
            assert len(row.evidence_json) > 0


# ─── API テスト ────────────────────────────────────────────────────────────────

@pytest.fixture
def app_with_db(db_session: AsyncSession):
    """テスト用 FastAPI アプリを返す（DB をテスト用セッションで差し替え）"""
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
    token = get_settings().API_TOKEN
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
class TestMarketStateAPI:
    """市場状態 API エンドポイントテスト"""

    async def test_current_empty(self, app_with_db, auth_headers):
        """スナップショットがない場合は空リストを返す"""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/current", headers=auth_headers
            )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_current_after_run(self, app_with_db, auth_headers, db_session: AsyncSession):
        """エンジン実行後に current が状態を返す"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            market_data={"index_change_pct": 0.8, "index_name": "TOPIX"},
        )
        await engine.run(ctx)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/current", headers=auth_headers
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        layers = {item["layer"] for item in data}
        assert "market" in layers
        assert "time_window" in layers

    async def test_current_filter_by_layer(self, app_with_db, auth_headers, db_session: AsyncSession):
        """layer パラメータでフィルタできること"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
        )
        await engine.run(ctx)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/current?layer=market", headers=auth_headers
            )
        assert resp.status_code == 200
        data = resp.json()
        assert all(item["layer"] == "market" for item in data)

    async def test_current_unauthorized(self, app_with_db):
        """認証なしは 403 を返す"""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/current",
                headers={"Authorization": "Bearer invalid-token"},
            )
        assert resp.status_code == 403

    async def test_history_empty(self, app_with_db, auth_headers):
        """評価なしは空リストを返す"""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/history", headers=auth_headers
            )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_history_after_run(self, app_with_db, auth_headers, db_session: AsyncSession):
        """エンジン実行後に history が返される"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=timezone.utc),
            market_data={"index_change_pct": -0.6},
        )
        await engine.run(ctx)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/history?layer=market", headers=auth_headers
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["state_code"] == "trend_down"
        assert data[0]["layer"] == "market"
        assert isinstance(data[0]["evidence"], dict)

    async def test_history_limit(self, app_with_db, auth_headers, db_session: AsyncSession):
        """limit パラメータが機能すること"""
        engine = MarketStateEngine(db_session)
        for i in range(5):
            ctx = EvaluationContext(
                evaluation_time=datetime(2024, 11, 6, i + 1, 0, 0, tzinfo=timezone.utc),
            )
            await engine.run(ctx)

        async with AsyncClient(
            transport=ASGITransport(app=app_with_db), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/market-state/history?layer=market&limit=3", headers=auth_headers
            )
        assert resp.status_code == 200
        assert len(resp.json()) == 3
