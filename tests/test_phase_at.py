"""
Phase AT テスト — DailyMetricsComputer / DailyMetricsRepository / Runner integration

テスト構成:
  1. TestDailyMetricsComputerMA    — MA5 / MA20 計算・行数不足
  2. TestDailyMetricsComputerATR   — ATR14 計算・行数不足・high/low None
  3. TestDailyMetricsComputerRSI   — RSI14 計算・行数不足・all-gain/all-loss
  4. TestDailyMetricsComputerStale — stale 判定（全 None）
  5. TestDailyMetricsRepository    — DB 取得・件数制限・降順
  6. TestRunnerDailyMetricsEnrich  — runner._run_once() で symbol_data に ma5/ma20/atr/rsi が注入される
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text

from trade_app.models.daily_price_history import DailyPriceHistory
from trade_app.services.market_state.daily_metrics import (
    DailyMetricsComputer,
    DailyMetricsRepository,
    DailyPriceRow,
)


# ─── ユーティリティ ────────────────────────────────────────────────────────────

def _row(
    trading_date: date,
    close: float,
    high: float | None = None,
    low: float | None = None,
    open_: float | None = None,
) -> DailyPriceRow:
    """テスト用 DailyPriceRow を生成するヘルパー。"""
    return DailyPriceRow(
        trading_date=trading_date,
        open=open_,
        high=high if high is not None else close + 10.0,
        low=low if low is not None else close - 10.0,
        close=close,
        volume=None,
    )


def _rows_desc(n: int, base_close: float = 1000.0, base_date: date | None = None) -> list[DailyPriceRow]:
    """n 行の DailyPriceRow を DESC 順（最新が先頭）で生成する。close は連番で微増。"""
    today = base_date or date(2026, 3, 27)
    rows = []
    for i in range(n):
        d = today - timedelta(days=i)
        close = base_close + i   # 最古が最大（DESC なので先頭が最新）
        rows.append(_row(d, close=base_close + (n - 1 - i)))
    return rows


# ─── 1. MA計算 ────────────────────────────────────────────────────────────────

class TestDailyMetricsComputerMA:
    TODAY = date(2026, 3, 27)

    def _compute(self, rows_desc: list[DailyPriceRow]) -> dict:
        return DailyMetricsComputer.compute(rows_desc, self.TODAY, stale_threshold_days=4)

    def test_ma5_correct(self):
        """直近5行 close の平均が ma5 に入る"""
        rows = _rows_desc(20, base_close=1000.0, base_date=self.TODAY)
        result = self._compute(rows)
        # rows[0..4] の close が ma5 の対象（DESC 順の先頭5行）
        expected = sum(r.close for r in rows[:5]) / 5
        assert result["ma5"] == pytest.approx(expected)

    def test_ma20_correct(self):
        """直近20行 close の平均が ma20 に入る"""
        rows = _rows_desc(20, base_close=1000.0, base_date=self.TODAY)
        result = self._compute(rows)
        expected = sum(r.close for r in rows[:20]) / 20
        assert result["ma20"] == pytest.approx(expected)

    def test_ma5_insufficient_rows(self):
        """4行以下 → ma5=None"""
        rows = _rows_desc(4, base_date=self.TODAY)
        result = self._compute(rows)
        assert result["ma5"] is None

    def test_ma20_insufficient_rows(self):
        """19行以下 → ma20=None"""
        rows = _rows_desc(19, base_date=self.TODAY)
        result = self._compute(rows)
        assert result["ma20"] is None

    def test_ma5_exactly_5_rows(self):
        """ちょうど5行 → ma5 が計算される"""
        rows = _rows_desc(5, base_close=2000.0, base_date=self.TODAY)
        result = self._compute(rows)
        assert result["ma5"] is not None

    def test_ma20_exactly_20_rows(self):
        """ちょうど20行 → ma20 が計算される"""
        rows = _rows_desc(20, base_close=2000.0, base_date=self.TODAY)
        result = self._compute(rows)
        assert result["ma20"] is not None


# ─── 2. ATR計算 ───────────────────────────────────────────────────────────────

class TestDailyMetricsComputerATR:
    TODAY = date(2026, 3, 27)

    def _compute(self, rows_desc: list[DailyPriceRow]) -> dict:
        return DailyMetricsComputer.compute(rows_desc, self.TODAY, stale_threshold_days=4)

    def test_atr_insufficient_rows(self):
        """13行以下 → atr=None"""
        rows = _rows_desc(13, base_date=self.TODAY)
        result = self._compute(rows)
        assert result["atr"] is None

    def test_atr_exactly_14_rows(self):
        """ちょうど14行 → atr が計算される"""
        rows = _rows_desc(14, base_close=2000.0, base_date=self.TODAY)
        result = self._compute(rows)
        assert result["atr"] is not None
        assert result["atr"] > 0

    def test_atr_value_is_mean_of_trs(self):
        """ATR = 14 TR の平均であることを確認"""
        # high = close + 20, low = close - 20 で統一 → H-L = 40
        # 先頭行は prev_close なし → TR = H-L = 40
        # 2行目以降: prev_close が明らかに近い場合 → TR ≒ H-L = 40
        rows = []
        for i in range(14):
            d = self.TODAY - timedelta(days=i)
            rows.append(DailyPriceRow(
                trading_date=d, open=1000.0,
                high=1020.0, low=980.0, close=1000.0, volume=None
            ))
        result = self._compute(rows)
        # 全TR = 40（high-low）なので ATR = 40
        assert result["atr"] == pytest.approx(40.0)

    def test_atr_none_if_high_is_none(self):
        """high が None の行を含む場合 atr=None"""
        rows = _rows_desc(14, base_date=self.TODAY)
        # 最古行（最後）の high を None にする
        rows_asc = list(reversed(rows))
        rows_asc[-14] = DailyPriceRow(
            trading_date=rows_asc[-14].trading_date,
            open=None, high=None, low=1000.0, close=1000.0, volume=None,
        )
        rows_modified = list(reversed(rows_asc))
        result = self._compute(rows_modified)
        assert result["atr"] is None

    def test_atr_uses_prev_close(self):
        """ATR は prev_close との gap も考慮することを確認"""
        # prev_close=900, high=1010, low=990 → TR=max(20, 110, 90)=110
        rows = []
        for i in range(14):
            d = self.TODAY - timedelta(days=i)
            if i == 0:  # 最新（先頭）
                close = 1000.0
            else:
                close = 900.0  # 前日に大きく下落していた
            rows.append(DailyPriceRow(
                trading_date=d, open=None,
                high=1010.0, low=990.0, close=close, volume=None,
            ))
        result = self._compute(rows)
        # 直近行 (i=0) の prev_close は rows_asc[13] のclose=900 → TR=max(20,110,90)=110
        assert result["atr"] is not None
        assert result["atr"] > 20.0   # H-L のみの 20.0 より大きい


# ─── 3. RSI計算 ───────────────────────────────────────────────────────────────

class TestDailyMetricsComputerRSI:
    TODAY = date(2026, 3, 27)

    def _compute(self, rows_desc: list[DailyPriceRow]) -> dict:
        return DailyMetricsComputer.compute(rows_desc, self.TODAY, stale_threshold_days=4)

    def test_rsi_insufficient_rows(self):
        """14行以下 → rsi=None（15行必要）"""
        rows = _rows_desc(14, base_date=self.TODAY)
        result = self._compute(rows)
        assert result["rsi"] is None

    def test_rsi_exactly_15_rows(self):
        """ちょうど15行 → rsi が計算される"""
        rows = _rows_desc(15, base_close=2000.0, base_date=self.TODAY)
        result = self._compute(rows)
        assert result["rsi"] is not None
        assert 0.0 <= result["rsi"] <= 100.0

    def test_rsi_all_gains(self):
        """終値が連続上昇 → RSI = 100"""
        # 最古 → 最新 で close が増加し続ける（降順なので先頭が大きい）
        rows = []
        for i in range(15):
            d = self.TODAY - timedelta(days=i)
            rows.append(_row(d, close=1000.0 + (14 - i) * 10.0))
        result = self._compute(rows)
        assert result["rsi"] == pytest.approx(100.0)

    def test_rsi_all_losses(self):
        """終値が連続下落 → RSI ≈ 0"""
        # 最古 → 最新 で close が減少し続ける
        rows = []
        for i in range(15):
            d = self.TODAY - timedelta(days=i)
            rows.append(_row(d, close=1000.0 - (14 - i) * 10.0))
        result = self._compute(rows)
        assert result["rsi"] == pytest.approx(0.0)

    def test_rsi_range(self):
        """RSI は 0〜100 の範囲に収まる"""
        rows = _rows_desc(21, base_close=1000.0, base_date=self.TODAY)
        result = self._compute(rows)
        assert 0.0 <= result["rsi"] <= 100.0


# ─── 4. Stale判定 ─────────────────────────────────────────────────────────────

class TestDailyMetricsComputerStale:
    TODAY = date(2026, 3, 27)

    def test_stale_returns_all_none(self):
        """最新 trading_date が stale_threshold_days より古い → 全 None"""
        stale_date = self.TODAY - timedelta(days=5)
        rows = [_row(stale_date, close=1000.0)]
        result = DailyMetricsComputer.compute(rows, self.TODAY, stale_threshold_days=4)
        assert result == {"ma5": None, "ma20": None, "atr": None, "rsi": None}

    def test_not_stale_within_threshold(self):
        """最新 trading_date が閾値内 → stale にならない"""
        # 月曜日: 前取引日は金曜日 (3日前)、threshold=4 なので stale にならない
        friday = self.TODAY - timedelta(days=3)
        rows = _rows_desc(21, base_date=friday)
        result = DailyMetricsComputer.compute(rows, self.TODAY, stale_threshold_days=4)
        # stale でないので ma5 は行数次第（21行あるので計算される）
        assert result["ma5"] is not None

    def test_empty_rows_returns_all_none(self):
        """rows が空 → 全 None"""
        result = DailyMetricsComputer.compute([], self.TODAY)
        assert result == {"ma5": None, "ma20": None, "atr": None, "rsi": None}

    def test_stale_one_day_past_boundary(self):
        """cutoff より1日古い（today - 5日）→ stale"""
        # stale_cutoff = today - 4 days。5日前は cutoff より古い → stale
        stale_date = self.TODAY - timedelta(days=5)
        rows = _rows_desc(21, base_date=stale_date)
        result = DailyMetricsComputer.compute(rows, self.TODAY, stale_threshold_days=4)
        assert result == {"ma5": None, "ma20": None, "atr": None, "rsi": None}

    def test_not_stale_at_cutoff(self):
        """ちょうど cutoff（today - 4 日）→ stale にならない（< 判定なので == は通過）"""
        cutoff = self.TODAY - timedelta(days=4)
        rows = _rows_desc(21, base_date=cutoff)
        result = DailyMetricsComputer.compute(rows, self.TODAY, stale_threshold_days=4)
        assert result["ma5"] is not None

    def test_not_stale_one_day_within_threshold(self):
        """today - 3日（月曜の前取引日=金曜相当）→ stale にならない"""
        recent = self.TODAY - timedelta(days=3)
        rows = _rows_desc(21, base_date=recent)
        result = DailyMetricsComputer.compute(rows, self.TODAY, stale_threshold_days=4)
        assert result["ma5"] is not None


# ─── 5. Repository (DB) ───────────────────────────────────────────────────────

class TestDailyMetricsRepository:
    @pytest_asyncio.fixture
    async def session_with_data(self, db_session):
        """7203 の直近 25 日分を挿入した DB セッションを返す"""
        today = date(2026, 3, 27)
        for i in range(25):
            d = today - timedelta(days=i)
            row = DailyPriceHistory(
                id=str(uuid.uuid4()),
                ticker="7203",
                trading_date=d,
                open=1000.0 + i,
                high=1010.0 + i,
                low=990.0 + i,
                close=1000.0 + i,
                volume=10000,
                source="test",
            )
            db_session.add(row)
        await db_session.commit()
        return db_session

    @pytest.mark.asyncio
    async def test_returns_rows_desc_order(self, session_with_data):
        """取得結果が trading_date DESC 順（最新が先頭）"""
        repo = DailyMetricsRepository(session_with_data)
        rows = await repo.get_recent_rows("7203", n=10)
        dates = [r.trading_date for r in rows]
        assert dates == sorted(dates, reverse=True)

    @pytest.mark.asyncio
    async def test_respects_limit(self, session_with_data):
        """n=10 を指定すると最大10行"""
        repo = DailyMetricsRepository(session_with_data)
        rows = await repo.get_recent_rows("7203", n=10)
        assert len(rows) == 10

    @pytest.mark.asyncio
    async def test_returns_all_if_less_than_n(self, session_with_data):
        """DB に 25 行あり n=21 → 21 行返却"""
        repo = DailyMetricsRepository(session_with_data)
        rows = await repo.get_recent_rows("7203", n=21)
        assert len(rows) == 21

    @pytest.mark.asyncio
    async def test_empty_for_unknown_ticker(self, session_with_data):
        """存在しない ticker → 空リスト（例外なし）"""
        repo = DailyMetricsRepository(session_with_data)
        rows = await repo.get_recent_rows("9999", n=21)
        assert rows == []

    @pytest.mark.asyncio
    async def test_row_fields_converted(self, session_with_data):
        """ORM → DailyPriceRow 変換が正しいこと"""
        repo = DailyMetricsRepository(session_with_data)
        rows = await repo.get_recent_rows("7203", n=1)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row.close, float)
        assert isinstance(row.trading_date, date)


# ─── 6. Runner integration ────────────────────────────────────────────────────

class TestRunnerDailyMetricsEnrich:
    """
    MarketStateRunner._run_once() が symbol_data に daily metrics を注入することを確認する。
    DB・broker・engine はすべてモック化する。
    """

    def _make_runner(self):
        from trade_app.services.market_state.runner import MarketStateRunner
        from trade_app.services.market_state.symbol_data_fetcher import SymbolDataFetcher

        mock_fetcher = MagicMock(spec=SymbolDataFetcher)
        mock_fetcher.fetch = AsyncMock(return_value={
            "7203": {
                "current_price": 3414.0,
                "best_bid": 3413.0,
                "best_ask": 3415.0,
                "last_updated": datetime.now(timezone.utc),
                "bid_ask_updated": datetime.now(timezone.utc),
            }
        })
        runner = MarketStateRunner(symbol_fetcher=mock_fetcher)
        return runner

    @pytest.mark.asyncio
    async def test_symbol_data_enriched_with_daily_metrics(self):
        """
        DB に daily data があるとき symbol_data に ma5/ma20/atr/rsi が注入される。
        """
        today = date(2026, 3, 27)
        rows_21 = _rows_desc(21, base_close=3400.0, base_date=today)

        mock_repo = MagicMock()
        mock_repo.get_recent_rows = AsyncMock(return_value=rows_21)

        captured: dict = {}

        async def fake_engine_run(ctx):
            captured.update(ctx.symbol_data.get("7203", {}))
            return []

        runner = self._make_runner()

        with (
            patch("trade_app.services.market_state.runner.get_settings") as mock_settings,
            patch("trade_app.services.market_state.runner.AsyncSessionLocal") as mock_session_local,
            patch("trade_app.services.market_state.runner.DailyMetricsRepository", return_value=mock_repo),
            patch("trade_app.services.market_state.runner.MarketStateEngine") as mock_engine_cls,
        ):
            mock_settings.return_value.MARKET_STATE_INTERVAL_SEC = 60
            mock_settings.return_value.WATCHED_SYMBOLS = "7203"

            mock_engine = MagicMock()
            mock_engine.run = AsyncMock(side_effect=fake_engine_run)
            mock_engine_cls.return_value = mock_engine

            mock_db = AsyncMock()
            mock_session_local.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_local.return_value.__aexit__ = AsyncMock(return_value=None)

            await runner._run_once()

        assert "ma5" in captured
        assert "ma20" in captured
        assert "atr" in captured
        assert "rsi" in captured
        assert captured["ma5"] is not None

    @pytest.mark.asyncio
    async def test_symbol_data_has_all_none_when_no_daily_data(self):
        """
        DB に daily data がない（空リスト）とき ma5/ma20/atr/rsi がすべて None で注入される。
        """
        mock_repo = MagicMock()
        mock_repo.get_recent_rows = AsyncMock(return_value=[])  # データなし

        captured: dict = {}

        async def fake_engine_run(ctx):
            captured.update(ctx.symbol_data.get("7203", {}))
            return []

        runner = self._make_runner()

        with (
            patch("trade_app.services.market_state.runner.get_settings") as mock_settings,
            patch("trade_app.services.market_state.runner.AsyncSessionLocal") as mock_session_local,
            patch("trade_app.services.market_state.runner.DailyMetricsRepository", return_value=mock_repo),
            patch("trade_app.services.market_state.runner.MarketStateEngine") as mock_engine_cls,
        ):
            mock_settings.return_value.MARKET_STATE_INTERVAL_SEC = 60
            mock_settings.return_value.WATCHED_SYMBOLS = "7203"

            mock_engine = MagicMock()
            mock_engine.run = AsyncMock(side_effect=fake_engine_run)
            mock_engine_cls.return_value = mock_engine

            mock_db = AsyncMock()
            mock_session_local.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_local.return_value.__aexit__ = AsyncMock(return_value=None)

            await runner._run_once()

        assert captured["ma5"] is None
        assert captured["ma20"] is None
        assert captured["atr"] is None
        assert captured["rsi"] is None

    @pytest.mark.asyncio
    async def test_existing_keys_preserved(self):
        """
        daily metrics 注入後も current_price / best_bid / best_ask が残る。
        """
        mock_repo = MagicMock()
        mock_repo.get_recent_rows = AsyncMock(return_value=[])

        captured: dict = {}

        async def fake_engine_run(ctx):
            captured.update(ctx.symbol_data.get("7203", {}))
            return []

        runner = self._make_runner()

        with (
            patch("trade_app.services.market_state.runner.get_settings") as mock_settings,
            patch("trade_app.services.market_state.runner.AsyncSessionLocal") as mock_session_local,
            patch("trade_app.services.market_state.runner.DailyMetricsRepository", return_value=mock_repo),
            patch("trade_app.services.market_state.runner.MarketStateEngine") as mock_engine_cls,
        ):
            mock_settings.return_value.MARKET_STATE_INTERVAL_SEC = 60
            mock_settings.return_value.WATCHED_SYMBOLS = "7203"

            mock_engine = MagicMock()
            mock_engine.run = AsyncMock(side_effect=fake_engine_run)
            mock_engine_cls.return_value = mock_engine

            mock_db = AsyncMock()
            mock_session_local.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_local.return_value.__aexit__ = AsyncMock(return_value=None)

            await runner._run_once()

        # SymbolDataFetcher が取得したフィールドが残っていること
        assert "current_price" in captured
        assert "best_bid" in captured
        assert "best_ask" in captured
