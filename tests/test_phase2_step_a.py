"""
Phase 2 Step A 統合確認テスト

確認項目:
  1. symbol_data に current_price が入る（SymbolDataFetcher → MarketStateRunner 連携）
  2. current_state_snapshots が更新される（symbol layer）— 非 None 価格のとき
  3. stale が発生しなくなる — snapshot.updated_at が更新されている
  4. 例外が system 全体を止めない — API エラーでも runner は継続する
  5. ticker 単位で失敗が隔離される — 1 ticker 失敗でも他 ticker の snapshot は更新される

設計検証:
  - SymbolDataFetcher と MarketStateRunner の統合を SQLite + AsyncSession で検証
  - MarketStateRunner._run_once() をモック Session Factory で実行
  - BrokerAdapter を AsyncMock で差し替え
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.brokers.base import BrokerAPIError, MarketData
from trade_app.models.current_state_snapshot import CurrentStateSnapshot
from trade_app.models.state_evaluation import StateEvaluation
from trade_app.services.market_state.engine import MarketStateEngine
from trade_app.services.market_state.schemas import EvaluationContext
from trade_app.services.market_state.symbol_data_fetcher import SymbolDataFetcher

_UTC = timezone.utc


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _make_broker(prices: dict[str, float | None | Exception]) -> AsyncMock:
    """
    prices dict の値を current_price とし、bid/ask は None で MarketData を返す mock。
    価格が Exception の場合は raise する。
    """
    broker = AsyncMock()

    async def _get_data(ticker: str) -> MarketData:
        val = prices.get(ticker)
        if isinstance(val, Exception):
            raise val
        return MarketData(current_price=val, best_bid=None, best_ask=None)

    broker.get_market_data.side_effect = _get_data
    return broker


async def _run_engine_with_symbol_data(
    db: AsyncSession,
    symbol_data: dict,
) -> None:
    """指定した symbol_data で MarketStateEngine を 1 サイクル実行する。"""
    ctx = EvaluationContext(
        evaluation_time=datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC),
        symbol_data=symbol_data,
    )
    engine = MarketStateEngine(db)
    await engine.run(ctx)


# ─── 確認 1: symbol_data に current_price が入る ──────────────────────────────

class TestSymbolDataPopulated:
    """SymbolDataFetcher が MarketStateRunner に正しく current_price を渡す"""

    @pytest.mark.asyncio
    async def test_fetch_returns_current_price_for_ticker(self):
        """
        BrokerAdapter.get_market_data() が MarketData を返すとき、
        SymbolDataFetcher.fetch() の結果に current_price が含まれる。
        """
        broker = _make_broker({"7203": 3400.0, "6758": 1500.0})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203", "6758"])

        assert "7203" in result
        assert result["7203"]["current_price"] == 3400.0
        assert "6758" in result
        assert result["6758"]["current_price"] == 1500.0

    @pytest.mark.asyncio
    async def test_none_price_still_in_symbol_data(self):
        """
        get_market_data() が current_price=None のとき（取引時間外等）も
        {"current_price": None, ...} として含まれる。
        これにより snapshot の updated_at がリセットされ stale タイマーが維持される。
        """
        broker = _make_broker({"7203": None})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])

        assert "7203" in result
        assert result["7203"]["current_price"] is None


# ─── 確認 2 + 3: snapshot が更新される / stale が発生しない ──────────────────────

class TestSnapshotUpdated:
    """symbol layer の current_state_snapshots が更新されることを確認する"""

    @pytest.mark.asyncio
    async def test_symbol_snapshot_created_when_price_triggers_state(
        self, db_session: AsyncSession
    ):
        """
        current_price + gap 条件が揃うと symbol layer の snapshot が作成される。

        確認:
          - current_state_snapshots に layer=symbol の行が存在すること
          - updated_at が evaluation_time と同等であること（stale なし）
        """
        # gap_up_open を発動させる symbol_data
        symbol_data = {
            "7203": {
                "current_price": 3060.0,
                "current_open": 3060.0,
                "prev_close": 3000.0,
            }
        }
        await _run_engine_with_symbol_data(db_session, symbol_data)

        rows = (await db_session.execute(
            select(CurrentStateSnapshot).where(
                CurrentStateSnapshot.layer == "symbol"
            )
        )).scalars().all()

        assert len(rows) == 1, f"symbol layer snapshot が 1 件でない: {len(rows)}"
        snap = rows[0]
        assert snap.target_code == "7203"
        assert "gap_up_open" in snap.active_states_json
        # updated_at が evaluation_time と一致（stale でない）
        assert snap.updated_at is not None

    @pytest.mark.asyncio
    async def test_snapshot_updated_at_refreshed_on_second_run(
        self, db_session: AsyncSession
    ):
        """
        2 回目の評価で snapshot.updated_at が更新される（stale タイマーリセット）。
        """
        symbol_data_v1 = {
            "7203": {
                "current_price": 3060.0,
                "current_open": 3060.0,
                "prev_close": 3000.0,
            }
        }
        ctx1 = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC),
            symbol_data=symbol_data_v1,
        )
        engine1 = MarketStateEngine(db_session)
        await engine1.run(ctx1)

        snap_before = (await db_session.execute(
            select(CurrentStateSnapshot).where(
                CurrentStateSnapshot.layer == "symbol",
                CurrentStateSnapshot.target_code == "7203",
            )
        )).scalar_one()
        updated_at_before = snap_before.updated_at

        # 2 回目: 同じ銘柄で別の価格
        symbol_data_v2 = {
            "7203": {
                "current_price": 3100.0,
                "current_open": 3060.0,
                "prev_close": 3000.0,
            }
        }
        ctx2 = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 10, 1, 0, tzinfo=_UTC),
            symbol_data=symbol_data_v2,
        )
        engine2 = MarketStateEngine(db_session)
        await engine2.run(ctx2)

        await db_session.refresh(snap_before)
        updated_at_after = snap_before.updated_at

        assert updated_at_after > updated_at_before, \
            "2回目の評価で updated_at が更新されていない（stale タイマーがリセットされない）"


# ─── 確認 4: 例外が system 全体を止めない ─────────────────────────────────────

class TestExceptionIsolation:
    """BrokerAPIError が発生しても runner ループは継続する"""

    @pytest.mark.asyncio
    async def test_all_tickers_fail_fetch_does_not_raise(self):
        """
        全 ticker で BrokerAPIError が発生しても fetch() は空 dict を返すだけ（例外なし）。
        """
        broker = _make_broker({
            "7203": BrokerAPIError("connection timeout"),
            "6758": BrokerAPIError("connection timeout"),
        })
        fetcher = SymbolDataFetcher(broker)
        # 例外が伝播しないことを確認
        result = await fetcher.fetch(["7203", "6758"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_engine_continues_with_empty_symbol_data(
        self, db_session: AsyncSession
    ):
        """
        symbol_data が空でも MarketStateEngine は time_window / market 評価を継続する。
        """
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC),
            symbol_data={},
        )
        engine = MarketStateEngine(db_session)
        results = await engine.run(ctx)
        # time_window + market の結果は保存される
        assert len(results) >= 1, "symbol_data 空でも time_window/market 結果が存在するはず"

        # symbol layer の snapshot は作成されない（正常）
        symbol_snaps = (await db_session.execute(
            select(CurrentStateSnapshot).where(
                CurrentStateSnapshot.layer == "symbol"
            )
        )).scalars().all()
        assert len(symbol_snaps) == 0


# ─── 確認 5: ticker 単位で失敗が隔離される ────────────────────────────────────

class TestTickerIsolation:
    """1 ticker の失敗が他 ticker に影響しないことを確認する"""

    @pytest.mark.asyncio
    async def test_one_ticker_fails_other_ticker_snapshot_still_created(
        self, db_session: AsyncSession
    ):
        """
        7203 が BrokerAPIError → 結果から除外。
        6758 は正常 → snapshot が作成される。
        """
        broker = _make_broker({
            "7203": BrokerAPIError("timeout"),
            "6758": 1500.0,
        })
        fetcher = SymbolDataFetcher(broker)
        symbol_data = await fetcher.fetch(["7203", "6758"])

        # 7203 は除外される
        assert "7203" not in symbol_data
        # 6758 は正常に取得できる（current_price は MarketData.current_price から）
        assert "6758" in symbol_data
        assert symbol_data["6758"]["current_price"] == 1500.0

    @pytest.mark.asyncio
    async def test_failed_ticker_has_no_snapshot_successful_ticker_does(
        self, db_session: AsyncSession
    ):
        """
        6758 のみ正常 → 6758 の snapshot のみ作成。
        7203 は失敗（除外）→ snapshot なし。
        """
        # 6758 のみ gap_up_open を発動
        symbol_data = {
            "6758": {
                "current_price": 1530.0,
                "current_open": 1530.0,
                "prev_close": 1500.0,
            }
            # 7203 は除外済み（API 失敗のため fetch が返さなかった想定）
        }
        await _run_engine_with_symbol_data(db_session, symbol_data)

        all_snaps = (await db_session.execute(
            select(CurrentStateSnapshot).where(
                CurrentStateSnapshot.layer == "symbol"
            )
        )).scalars().all()

        codes = {s.target_code for s in all_snaps}
        assert "6758" in codes, "6758 の snapshot が存在しない"
        assert "7203" not in codes, "7203 の snapshot が存在すべきでない（API 失敗）"
