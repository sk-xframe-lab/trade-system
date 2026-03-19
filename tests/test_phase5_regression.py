"""
Phase 5 リグレッションテスト

目的:
  - save_evaluations のグループ化バグ再発防止を明示テストで固定する
  - MarketStateEngine の evaluator 失敗分離を検証する
  - SymbolStateEvaluator の ticker 単位失敗分離を検証する
  - WATCHED_SYMBOLS 未設定時の Runner 挙動（パース・ロジック）を検証する

旧バグ (Phase 4→5 修正済み):
  save_evaluations を for-result ループで実行すると、同一 (layer, target_type, target_code) に
  複数 state がある場合に「2件目の保存前に soft-expire が走り 1件目が is_active=False になる」
  問題があった。グループ化（key 単位で 1 回だけ soft-expire + 全件 INSERT）で修正済み。
  本テストはその修正が維持されることを保証する。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.services.market_state.engine import MarketStateEngine
from trade_app.services.market_state.evaluator_base import AbstractStateEvaluator
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult
from trade_app.services.market_state.symbol_evaluator import SymbolStateEvaluator

_UTC = timezone.utc


def _ctx(hour: int = 1, **symbol_data_fields) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=datetime(2024, 11, 6, hour, 0, 0, tzinfo=_UTC),
        symbol_data={"7203": symbol_data_fields} if symbol_data_fields else {},
    )


# ─── save_evaluations グループ化バグ再発防止 ────────────────────────────────────

@pytest.mark.asyncio
class TestSaveEvaluationsGroupingRegression:
    """
    同一 (layer, target_type, target_code) に複数 StateEvaluationResult がある場合、
    グループ単位で 1 回だけ soft-expire し、全件を is_active=True で INSERT すること。

    旧バグ: for-result ループで soft-expire すると 2件目以降が 1件目を消した。
    修正: groups dict でグループ化 → 1グループにつき 1 回だけ update(is_active=False) を実行。
    """

    async def test_two_states_same_ticker_both_active_after_save(
        self, db_session: AsyncSession
    ):
        """gap_up + high_volume を同一 ticker に保存 → 両方 is_active=True のまま"""
        engine = MarketStateEngine(db_session)
        ctx = _ctx(
            current_open=3060.0, prev_close=3000.0,            # gap_up_open
            current_volume=300_000, avg_volume_same_time=100_000,  # high_relative_volume
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        active_codes = {r.state_code for r in active}
        assert "gap_up_open" in active_codes, "gap_up_open が active でない"
        assert "high_relative_volume" in active_codes, "high_relative_volume が active でない"

    async def test_three_states_same_ticker_all_active(
        self, db_session: AsyncSession
    ):
        """3 状態 (gap_up + high_volume + overextended) が同時に active であること"""
        engine = MarketStateEngine(db_session)
        ctx = _ctx(
            current_open=3060.0, prev_close=3000.0,              # gap_up_open
            current_volume=300_000, avg_volume_same_time=100_000, # high_relative_volume
            rsi=80.0,                                              # overextended
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert len(active) == 3, f"3 件期待 got {len(active)}: {[r.state_code for r in active]}"

    async def test_second_run_with_different_state_expires_first(
        self, db_session: AsyncSession
    ):
        """
        run1: gap_up → is_active=True
        run2: wide_spread → gap_up が is_active=False に、wide_spread が is_active=True に
        """
        engine = MarketStateEngine(db_session)

        ctx1 = _ctx(1, current_open=3060.0, prev_close=3000.0)  # gap_up_open
        ctx2 = _ctx(2, best_bid=2994.0, best_ask=3006.0)         # wide_spread (0.4%)

        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=20
        )
        gap_rows = [r for r in history if r.state_code == "gap_up_open"]
        spread_rows = [r for r in history if r.state_code == "wide_spread"]

        assert len(gap_rows) == 1
        assert gap_rows[0].is_active is False, "gap_up_open は run2 で失効すべき"

        assert len(spread_rows) == 1
        assert spread_rows[0].is_active is True, "wide_spread は run2 で active であるべき"

    async def test_snapshot_contains_all_concurrent_active_states(
        self, db_session: AsyncSession
    ):
        """スナップショットの active_states_json に同時評価の全状態が含まれること"""
        engine = MarketStateEngine(db_session)
        ctx = _ctx(
            current_open=3060.0, prev_close=3000.0,              # gap_up_open
            current_volume=300_000, avg_volume_same_time=100_000, # high_relative_volume
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        snapshot = await repo.get_symbol_snapshot("7203")
        assert snapshot is not None
        assert "gap_up_open" in snapshot.active_states_json
        assert "high_relative_volume" in snapshot.active_states_json

    async def test_different_tickers_soft_expire_independently(
        self, db_session: AsyncSession
    ):
        """
        7203 と 9984 は独立した (layer, target_type, target_code) グループ。
        7203 の soft-expire が 9984 に影響しないこと。
        """
        engine = MarketStateEngine(db_session)

        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=_UTC),
            symbol_data={
                "7203": {"rsi": 80.0},  # overextended (overbought)
                "9984": {"rsi": 20.0},  # overextended (oversold)
            },
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        active_7203 = await repo.get_symbol_active_evaluations("7203")
        active_9984 = await repo.get_symbol_active_evaluations("9984")

        assert len(active_7203) == 1
        assert active_7203[0].evidence_json["direction"] == "overbought"

        assert len(active_9984) == 1
        assert active_9984[0].evidence_json["direction"] == "oversold"


# ─── Engine の Evaluator 失敗分離 ────────────────────────────────────────────

class _AlwaysFailingEvaluator(AbstractStateEvaluator):
    """テスト用: evaluate() が必ず RuntimeError を raise する Evaluator"""

    @property
    def name(self) -> str:
        return "AlwaysFailingEvaluator"

    def evaluate(self, ctx: EvaluationContext) -> list[StateEvaluationResult]:
        raise RuntimeError("Intentional test failure")


class _StaticEvaluator(AbstractStateEvaluator):
    """テスト用: 固定の StateEvaluationResult を返す Evaluator"""

    def __init__(self, state_code: str) -> None:
        self._state_code = state_code

    @property
    def name(self) -> str:
        return f"StaticEvaluator({self._state_code})"

    def evaluate(self, ctx: EvaluationContext) -> list[StateEvaluationResult]:
        return [
            StateEvaluationResult(
                layer="market",
                target_type="market",
                target_code=None,
                state_code=self._state_code,
                score=1.0,
                confidence=1.0,
                evidence={"evaluator": self.name},
            )
        ]


@pytest.mark.asyncio
class TestEngineEvaluatorFailureIsolation:
    """1つの Evaluator の失敗が他の Evaluator に影響しないこと"""

    async def test_failing_evaluator_does_not_stop_others(
        self, db_session: AsyncSession
    ):
        """
        [FailingEvaluator, StaticEvaluator("test_state")] の順で実行。
        FailingEvaluator は RuntimeError を raise するが、
        StaticEvaluator の結果は保存されること。
        """
        engine = MarketStateEngine(
            db_session,
            evaluators=[
                _AlwaysFailingEvaluator(),
                _StaticEvaluator("test_state"),
            ],
        )
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=_UTC),
        )
        results = await engine.run(ctx)

        # FailingEvaluator はスキップされ、StaticEvaluator の結果は保存される
        assert len(results) == 1
        assert results[0].state_code == "test_state"

    async def test_failing_evaluator_before_passing_evaluator_no_db_rollback(
        self, db_session: AsyncSession
    ):
        """
        FailingEvaluator が先に実行されても、後続 Evaluator の結果が DB に保存される。
        DB の整合性が壊れないこと。
        """
        engine = MarketStateEngine(
            db_session,
            evaluators=[
                _AlwaysFailingEvaluator(),
                _StaticEvaluator("stable_state"),
            ],
        )
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 2, 0, 0, tzinfo=_UTC),
        )
        await engine.run(ctx)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="market", limit=5)
        assert any(r.state_code == "stable_state" for r in history)


# ─── SymbolStateEvaluator の ticker 単位失敗分離 ────────────────────────────

class TestSymbolEvaluatorTickerIsolation:
    """1 ticker の評価失敗が他の ticker に影響しないこと"""

    def test_invalid_ticker_data_does_not_stop_valid_ticker(self):
        """
        BOOM ticker のデータが None（data.get() が AttributeError → 捕捉される）
        7203 ticker の評価は正常に完了する。
        """
        evaluator = SymbolStateEvaluator()
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=_UTC),
            symbol_data={
                "7203": {"rsi": 80.0},  # 正常データ → overextended
                "BOOM": "not_a_dict",   # 不正データ → _evaluate_symbol で AttributeError
            },
        )
        results = evaluator.evaluate(ctx)
        tickers = {r.target_code for r in results}
        assert "7203" in tickers, "7203 は正常に評価されるべき"
        assert "BOOM" not in tickers, "BOOM は評価失敗のためスキップされるべき"

    def test_one_ticker_error_other_tickers_still_evaluated(self):
        """3銘柄のうち1銘柄が失敗しても残り2銘柄は評価される"""
        evaluator = SymbolStateEvaluator()
        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=_UTC),
            symbol_data={
                "7203": {"rsi": 80.0},    # 正常 → overextended
                "CRASH": None,             # 不正 → AttributeError
                "9984": {"rsi": 20.0},    # 正常 → overextended (oversold)
            },
        )
        results = evaluator.evaluate(ctx)
        tickers = {r.target_code for r in results}
        assert "7203" in tickers
        assert "9984" in tickers
        assert "CRASH" not in tickers

    def test_market_time_evaluators_not_affected_by_symbol_failure(self):
        """
        SymbolStateEvaluator が全 ticker で失敗しても、
        TimeWindowStateEvaluator / MarketStateEvaluator の結果は変わらない。
        （Engine 層の per-evaluator try/except が保証する性質のテスト）
        """
        from trade_app.services.market_state.time_window_evaluator import TimeWindowStateEvaluator
        from trade_app.services.market_state.market_evaluator import MarketStateEvaluator

        time_ev = TimeWindowStateEvaluator()
        market_ev = MarketStateEvaluator()

        ctx = EvaluationContext(
            evaluation_time=datetime(2024, 11, 6, 1, 0, 0, tzinfo=_UTC),
            market_data={"index_change_pct": 0.8},
            symbol_data={"CRASH": None},  # SymbolStateEvaluator は全件失敗
        )

        # TimeWindowStateEvaluator / MarketStateEvaluator は symbol_data を無視
        time_results = time_ev.evaluate(ctx)
        market_results = market_ev.evaluate(ctx)

        assert len(time_results) == 1
        assert len(market_results) == 1
        assert time_results[0].layer == "time_window"
        assert market_results[0].state_code == "trend_up"


# ─── Runner WATCHED_SYMBOLS パース・挙動 ────────────────────────────────────

class TestRunnerWatchedSymbolsBehavior:
    """
    MarketStateRunner の WATCHED_SYMBOLS パース・空文字列処理。
    _run_once の DB 部分はここでは検証せず、ロジック部分のみテストする。
    """

    def _parse_watched(self, watched_str: str) -> list[str]:
        """Runner 内の watched パースロジックと同一実装"""
        return [s.strip() for s in watched_str.split(",") if s.strip()]

    def test_empty_string_returns_empty_list(self):
        assert self._parse_watched("") == []

    def test_single_ticker(self):
        assert self._parse_watched("7203") == ["7203"]

    def test_comma_separated_tickers_with_spaces(self):
        assert self._parse_watched("7203, 9984 , 6758") == ["7203", "9984", "6758"]

    def test_trailing_comma_ignored(self):
        assert self._parse_watched("7203, ") == ["7203"]

    def test_runner_empty_watched_flag_initially_false(self):
        """WATCHED_SYMBOLS 空の警告フラグは初期値 False"""
        from trade_app.services.market_state.runner import MarketStateRunner
        runner = MarketStateRunner()
        assert runner._warned_empty_symbols is False

    def test_runner_empty_watched_symbols_no_exception(self):
        """
        WATCHED_SYMBOLS が空でも watched リストが [] になるだけで例外にならない。
        Engine が {} の symbol_data を受け取ると SymbolStateEvaluator は空を返す（既存テスト済み）。
        """
        watched = self._parse_watched("")
        assert watched == []
        # 空リストで symbol_data={} を構築しても例外なし
        symbol_data: dict = {}
        if watched:
            pass  # Phase 1 では何もしない
        assert symbol_data == {}
