"""
Strategy Runner Phase 7 テスト

カバー内容:
  - DecisionRepository.upsert_decisions(): INSERT / UPDATE / ticker=None 対応
  - DecisionRepository.get_latest_decisions(): ticker 別 / 銘柄横断
  - DecisionRepository.get_history(): strategy_evaluations 履歴取得
  - StrategyEngine.run(): current_strategy_decisions への UPSERT を含む
  - StrategyRunner.failure_isolation: global / ticker 失敗分離
  - API /latest / /latest/{ticker} / /history
  - POST /api/admin/strategies/init
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.current_state_snapshot import CurrentStateSnapshot
from trade_app.models.current_strategy_decision import CurrentStrategyDecision
from trade_app.models.strategy_condition import StrategyCondition
from trade_app.models.strategy_definition import StrategyDefinition
from trade_app.models.strategy_evaluation import StrategyEvaluation
from trade_app.services.strategy.decision_repository import DecisionRepository
from trade_app.services.strategy.engine import StrategyEngine
from trade_app.services.strategy.repository import StrategyRepository
from trade_app.services.strategy.runner import StrategyRunner
from trade_app.services.strategy.schemas import StrategyDecisionResult

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 9, 30, 0, tzinfo=_UTC)


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _strategy(
    code: str = "test_strategy",
    name: str = "テスト戦略",
    is_enabled: bool = True,
    max_size_ratio: float = 1.0,
) -> StrategyDefinition:
    now = datetime.now(_UTC)
    return StrategyDefinition(
        id=str(uuid.uuid4()),
        strategy_code=code,
        strategy_name=name,
        is_enabled=is_enabled,
        direction="both",
        priority=0,
        max_size_ratio=max_size_ratio,
        created_at=now,
        updated_at=now,
    )


def _decision_result(
    strategy: StrategyDefinition,
    ticker: str | None = None,
    entry_allowed: bool = True,
    size_ratio: float = 1.0,
    evaluation_time: datetime | None = None,
) -> StrategyDecisionResult:
    return StrategyDecisionResult(
        strategy_id=strategy.id,
        strategy_code=strategy.strategy_code,
        strategy_name=strategy.strategy_name,
        ticker=ticker,
        evaluation_time=evaluation_time or _NOW,
        is_active=entry_allowed,
        entry_allowed=entry_allowed,
        size_ratio=size_ratio,
        matched_required_states=[],
        matched_forbidden_states=[],
        missing_required_states=[],
        blocking_reasons=[] if entry_allowed else ["state_snapshot_missing:market"],
        applied_size_modifier=1.0,
        evidence={"strategy_code": strategy.strategy_code},
    )


def _snapshot(
    layer: str,
    target_code: str | None,
    active_states: list[str],
    updated_at: datetime | None = None,
) -> CurrentStateSnapshot:
    return CurrentStateSnapshot(
        id=str(uuid.uuid4()),
        layer=layer,
        target_type=layer,
        target_code=target_code,
        active_states_json=active_states,
        state_summary_json={},
        updated_at=updated_at or _NOW,
    )


# ─────────────────────────────────────────────────────────────────────────────
# DecisionRepository テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestDecisionRepositoryUpsert:
    """upsert_decisions のテスト"""

    @pytest.mark.asyncio
    async def test_upsert_inserts_new_decision(self, db_session: AsyncSession):
        """新規 decision が INSERT される"""
        s = _strategy()
        db_session.add(s)
        await db_session.flush()

        repo = DecisionRepository(db_session)
        result = _decision_result(s, ticker=None, entry_allowed=True)
        await repo.upsert_decisions([result])
        await db_session.flush()

        decisions = await repo.get_latest_decisions(ticker=None)
        assert len(decisions) == 1
        assert decisions[0].strategy_code == s.strategy_code
        assert decisions[0].entry_allowed is True
        assert decisions[0].ticker is None

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_decision(self, db_session: AsyncSession):
        """同じ (strategy_id, ticker) で再度 upsert すると UPDATE される"""
        s = _strategy()
        db_session.add(s)
        await db_session.flush()

        repo = DecisionRepository(db_session)

        # 1回目: entry_allowed=True
        result1 = _decision_result(s, ticker=None, entry_allowed=True, size_ratio=1.0)
        await repo.upsert_decisions([result1])
        await db_session.flush()

        # 2回目: entry_allowed=False（更新）
        result2 = _decision_result(s, ticker=None, entry_allowed=False, size_ratio=0.0)
        await repo.upsert_decisions([result2])
        await db_session.flush()

        decisions = await repo.get_latest_decisions(ticker=None)
        # UPDATE により 1 件のまま
        assert len(decisions) == 1
        assert decisions[0].entry_allowed is False
        assert decisions[0].size_ratio == 0.0

    @pytest.mark.asyncio
    async def test_upsert_ticker_and_global_are_independent(self, db_session: AsyncSession):
        """ticker=None と ticker="7203" は別レコードとして管理される"""
        s = _strategy()
        db_session.add(s)
        await db_session.flush()

        repo = DecisionRepository(db_session)

        result_global = _decision_result(s, ticker=None, entry_allowed=False)
        result_ticker = _decision_result(s, ticker="7203", entry_allowed=True)
        await repo.upsert_decisions([result_global, result_ticker])
        await db_session.flush()

        global_decisions = await repo.get_latest_decisions(ticker=None)
        ticker_decisions = await repo.get_latest_decisions(ticker="7203")

        assert len(global_decisions) == 1
        assert global_decisions[0].entry_allowed is False
        assert global_decisions[0].ticker is None

        assert len(ticker_decisions) == 1
        assert ticker_decisions[0].entry_allowed is True
        assert ticker_decisions[0].ticker == "7203"

    @pytest.mark.asyncio
    async def test_upsert_multiple_strategies(self, db_session: AsyncSession):
        """複数 strategy を一度に upsert できる"""
        s1 = _strategy(code="strategy_a")
        s2 = _strategy(code="strategy_b")
        db_session.add(s1)
        db_session.add(s2)
        await db_session.flush()

        repo = DecisionRepository(db_session)
        results = [
            _decision_result(s1, ticker=None, entry_allowed=True),
            _decision_result(s2, ticker=None, entry_allowed=False),
        ]
        await repo.upsert_decisions(results)
        await db_session.flush()

        decisions = await repo.get_latest_decisions(ticker=None)
        assert len(decisions) == 2
        codes = {d.strategy_code for d in decisions}
        assert codes == {"strategy_a", "strategy_b"}


class TestDecisionRepositoryGetHistory:
    """get_history のテスト"""

    @pytest.mark.asyncio
    async def test_get_history_returns_evaluations(self, db_session: AsyncSession):
        """strategy_evaluations から履歴を取得できる"""
        s = _strategy()
        db_session.add(s)
        await db_session.flush()

        # strategy_evaluations を直接 INSERT
        eval1 = StrategyEvaluation(
            strategy_id=s.id,
            ticker=None,
            evaluation_time=_NOW - timedelta(minutes=5),
            is_active=False,
            entry_allowed=False,
            size_ratio=0.0,
            matched_required_states_json=[],
            matched_forbidden_states_json=[],
            missing_required_states_json=[],
            blocking_reasons_json=["state_snapshot_missing:market"],
            evidence_json={},
        )
        eval2 = StrategyEvaluation(
            strategy_id=s.id,
            ticker=None,
            evaluation_time=_NOW,
            is_active=True,
            entry_allowed=True,
            size_ratio=1.0,
            matched_required_states_json=[],
            matched_forbidden_states_json=[],
            missing_required_states_json=[],
            blocking_reasons_json=[],
            evidence_json={},
        )
        db_session.add(eval1)
        db_session.add(eval2)
        await db_session.flush()

        repo = DecisionRepository(db_session)
        history = await repo.get_history(ticker=None, limit=10)

        assert len(history) == 2
        # evaluation_time DESC 順
        assert history[0].evaluation_time > history[1].evaluation_time

    @pytest.mark.asyncio
    async def test_get_history_filters_by_ticker(self, db_session: AsyncSession):
        """ticker 指定で正しく絞り込まれる"""
        s = _strategy()
        db_session.add(s)
        await db_session.flush()

        eval_global = StrategyEvaluation(
            strategy_id=s.id, ticker=None, evaluation_time=_NOW,
            is_active=False, entry_allowed=False, size_ratio=0.0,
            matched_required_states_json=[], matched_forbidden_states_json=[],
            missing_required_states_json=[], blocking_reasons_json=[], evidence_json={},
        )
        eval_ticker = StrategyEvaluation(
            strategy_id=s.id, ticker="7203", evaluation_time=_NOW,
            is_active=True, entry_allowed=True, size_ratio=1.0,
            matched_required_states_json=[], matched_forbidden_states_json=[],
            missing_required_states_json=[], blocking_reasons_json=[], evidence_json={},
        )
        db_session.add(eval_global)
        db_session.add(eval_ticker)
        await db_session.flush()

        repo = DecisionRepository(db_session)

        history_global = await repo.get_history(ticker=None)
        history_ticker = await repo.get_history(ticker="7203")

        assert len(history_global) == 1
        assert history_global[0].ticker is None
        assert len(history_ticker) == 1
        assert history_ticker[0].ticker == "7203"

    @pytest.mark.asyncio
    async def test_get_history_respects_limit(self, db_session: AsyncSession):
        """limit パラメータで件数が制限される"""
        s = _strategy()
        db_session.add(s)
        await db_session.flush()

        for i in range(5):
            ev = StrategyEvaluation(
                strategy_id=s.id, ticker=None,
                evaluation_time=_NOW + timedelta(seconds=i),
                is_active=False, entry_allowed=False, size_ratio=0.0,
                matched_required_states_json=[], matched_forbidden_states_json=[],
                missing_required_states_json=[], blocking_reasons_json=[], evidence_json={},
            )
            db_session.add(ev)
        await db_session.flush()

        repo = DecisionRepository(db_session)
        history = await repo.get_history(ticker=None, limit=3)
        assert len(history) == 3


# ─────────────────────────────────────────────────────────────────────────────
# StrategyEngine + DecisionRepository 統合テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyEngineWithDecisionRepo:
    """StrategyEngine.run() が current_strategy_decisions を upsert することを確認"""

    @pytest.mark.asyncio
    async def test_engine_run_upserts_current_decisions(self, db_session: AsyncSession):
        """engine.run() 後に current_strategy_decisions に行が存在する"""
        from trade_app.services.strategy.seed import seed_strategies

        # seed
        await seed_strategies(db_session)
        await db_session.flush()

        # state snapshots（missing → entry_allowed=False のはず）
        # snapshots なしで実行

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)
        assert len(results) > 0

        # current_strategy_decisions に upsert されている
        repo = DecisionRepository(db_session)
        decisions = await repo.get_latest_decisions(ticker=None)
        assert len(decisions) == len(results)

    @pytest.mark.asyncio
    async def test_engine_run_updates_existing_decision(self, db_session: AsyncSession):
        """engine.run() を 2 回実行すると current_strategy_decisions は UPDATE される（重複しない）"""
        from trade_app.services.strategy.seed import seed_strategies

        await seed_strategies(db_session)
        await db_session.flush()

        engine = StrategyEngine(db_session)

        # 1回目
        await engine.run(ticker=None, evaluation_time=_NOW)
        # 2回目
        await engine.run(ticker=None, evaluation_time=_NOW + timedelta(minutes=1))

        repo = DecisionRepository(db_session)
        decisions = await repo.get_latest_decisions(ticker=None)

        # 重複なし（strategy ごとに 1 件）
        strategy_codes = [d.strategy_code for d in decisions]
        assert len(strategy_codes) == len(set(strategy_codes))

    @pytest.mark.asyncio
    async def test_engine_run_decision_reflects_entry_allowed(self, db_session: AsyncSession):
        """state snapshot が全て揃っていれば entry_allowed=True の decision が保存される"""
        s = _strategy(code="simple_strategy")
        db_session.add(s)

        # condition なし → 常に entry_allowed=True（pre_block がなければ）
        await db_session.flush()

        # state snapshots を用意（stale にならない更新時刻）
        snaps = [
            _snapshot("market", None, ["trend_up"], updated_at=_NOW),
            _snapshot("time_window", None, ["morning_trend_zone"], updated_at=_NOW),
        ]
        for snap in snaps:
            db_session.add(snap)
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)

        assert len(results) == 1
        assert results[0].entry_allowed is True

        repo = DecisionRepository(db_session)
        decisions = await repo.get_latest_decisions(ticker=None)
        assert len(decisions) == 1
        assert decisions[0].entry_allowed is True


# ─────────────────────────────────────────────────────────────────────────────
# StrategyRunner 失敗分離テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyRunnerFailureIsolation:
    """StrategyRunner の失敗分離（failure isolation）テスト"""

    @pytest.mark.asyncio
    async def test_global_failure_does_not_stop_ticker_evaluation(self):
        """global 評価の失敗が per-ticker 評価を止めない"""
        ticker_called = []

        class FakeSessionCtx:
            async def __aenter__(self):
                return AsyncMock(spec=AsyncSession)

            async def __aexit__(self, *args):
                pass

        def session_factory():
            return FakeSessionCtx()

        # StrategyEngine は _run_once 内で lazy import されるため engine モジュールをパッチ
        with patch(
            "trade_app.services.strategy.engine.StrategyEngine"
        ) as MockEngine:
            async def side_effect(*args, **kwargs):
                ticker_arg = kwargs.get("ticker", args[0] if args else None)
                if ticker_arg is None:
                    raise RuntimeError("global evaluation failed")
                ticker_called.append(ticker_arg)
                return []

            MockEngine.return_value.run.side_effect = side_effect

            runner = StrategyRunner(session_factory=session_factory)

            with patch(
                "trade_app.services.strategy.runner.get_settings"
            ) as mock_settings:
                mock_settings.return_value.WATCHED_SYMBOLS = "7203,9984"
                mock_settings.return_value.STRATEGY_RUNNER_INTERVAL_SEC = 60
                await runner._run_once()

        # ticker 評価は実行されている
        assert "7203" in ticker_called
        assert "9984" in ticker_called

    @pytest.mark.asyncio
    async def test_one_ticker_failure_does_not_stop_others(self):
        """1 ticker の失敗が他 ticker 評価を止めない"""
        ticker_called = []

        class FakeSessionCtx:
            async def __aenter__(self):
                return AsyncMock(spec=AsyncSession)

            async def __aexit__(self, *args):
                pass

        def session_factory():
            return FakeSessionCtx()

        with patch(
            "trade_app.services.strategy.engine.StrategyEngine"
        ) as MockEngine:
            async def side_effect(*args, **kwargs):
                ticker_arg = kwargs.get("ticker", args[0] if args else None)
                if ticker_arg is None:
                    return []  # global OK
                if ticker_arg == "7203":
                    raise RuntimeError("ticker 7203 failed")
                ticker_called.append(ticker_arg)
                return []

            MockEngine.return_value.run.side_effect = side_effect

            runner = StrategyRunner(session_factory=session_factory)

            with patch(
                "trade_app.services.strategy.runner.get_settings"
            ) as mock_settings:
                mock_settings.return_value.WATCHED_SYMBOLS = "7203,9984,6758"
                mock_settings.return_value.STRATEGY_RUNNER_INTERVAL_SEC = 60
                await runner._run_once()

        # 7203 は失敗したが 9984, 6758 は実行されている
        assert "9984" in ticker_called
        assert "6758" in ticker_called
        assert "7203" not in ticker_called

    @pytest.mark.asyncio
    async def test_runner_empty_symbols_global_only(self):
        """WATCHED_SYMBOLS が空の場合は global 評価のみ実行される"""
        global_called = []

        class FakeSessionCtx:
            async def __aenter__(self):
                return AsyncMock(spec=AsyncSession)

            async def __aexit__(self, *args):
                pass

        def session_factory():
            return FakeSessionCtx()

        with patch(
            "trade_app.services.strategy.engine.StrategyEngine"
        ) as MockEngine:
            async def side_effect(*args, **kwargs):
                ticker_arg = kwargs.get("ticker", args[0] if args else None)
                if ticker_arg is None:
                    global_called.append("global")
                return []

            MockEngine.return_value.run.side_effect = side_effect

            runner = StrategyRunner(session_factory=session_factory)

            with patch(
                "trade_app.services.strategy.runner.get_settings"
            ) as mock_settings:
                mock_settings.return_value.WATCHED_SYMBOLS = ""
                mock_settings.return_value.STRATEGY_RUNNER_INTERVAL_SEC = 60
                await runner._run_once()

        assert "global" in global_called


# ─────────────────────────────────────────────────────────────────────────────
# API テスト: /latest / /latest/{ticker} / /history
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def test_client(db_session: AsyncSession):
    """TestClient を返す（DB セッションを差し替え）"""
    from trade_app.main import app
    from trade_app.models.database import get_db
    from trade_app.routes.admin import _get_db as admin_get_db

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[admin_get_db] = override_db
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


# strategy.py は module-level で get_settings() を呼ぶため lru_cache の値を使用
# テスト環境ではデフォルト値 "changeme_before_production" が API_TOKEN となる
_AUTH = {"Authorization": "Bearer changeme_before_production"}
_BAD_AUTH = {"Authorization": "Bearer wrong"}


class TestLatestDecisionsAPI:
    """GET /api/v1/strategies/latest テスト"""

    def test_latest_returns_empty_when_no_decisions(self, test_client):
        """current_strategy_decisions が空のとき空リストを返す"""
        resp = test_client.get("/api/v1/strategies/latest", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_latest_requires_auth(self, test_client):
        """認証なしで 403"""
        resp = test_client.get(
            "/api/v1/strategies/latest",
            headers={"Authorization": "Bearer invalid"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_latest_returns_decisions(
        self, test_client, db_session: AsyncSession
    ):
        """upsert 済みの decision が返される"""
        s = _strategy()
        db_session.add(s)
        await db_session.flush()

        decision = CurrentStrategyDecision(
            strategy_id=s.id,
            strategy_code=s.strategy_code,
            ticker=None,
            is_active=False,
            entry_allowed=False,
            size_ratio=0.0,
            blocking_reasons_json=["state_snapshot_missing:market"],
            matched_required_states_json=[],
            missing_required_states_json=[],
            matched_forbidden_states_json=[],
            evidence_json={},
            evaluation_time=_NOW,
            updated_at=_NOW,
        )
        db_session.add(decision)
        await db_session.flush()

        resp = test_client.get("/api/v1/strategies/latest", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["strategy_code"] == s.strategy_code
        assert data[0]["entry_allowed"] is False


class TestLatestTickerDecisionsAPI:
    """GET /api/v1/strategies/latest/{ticker} テスト"""

    def test_latest_ticker_returns_empty(self, test_client):
        """該当 ticker がなければ空リスト"""
        resp = test_client.get("/api/v1/strategies/latest/7203", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_latest_ticker_returns_only_that_ticker(
        self, test_client, db_session: AsyncSession
    ):
        """ticker="7203" の decision のみ返される（ticker=None の decision は含まない）"""
        s = _strategy()
        db_session.add(s)
        await db_session.flush()

        dec_global = CurrentStrategyDecision(
            strategy_id=s.id, strategy_code=s.strategy_code, ticker=None,
            is_active=False, entry_allowed=False, size_ratio=0.0,
            blocking_reasons_json=[], matched_required_states_json=[],
            missing_required_states_json=[], matched_forbidden_states_json=[],
            evidence_json={}, evaluation_time=_NOW, updated_at=_NOW,
        )
        dec_ticker = CurrentStrategyDecision(
            strategy_id=s.id, strategy_code=s.strategy_code, ticker="7203",
            is_active=True, entry_allowed=True, size_ratio=1.0,
            blocking_reasons_json=[], matched_required_states_json=[],
            missing_required_states_json=[], matched_forbidden_states_json=[],
            evidence_json={}, evaluation_time=_NOW, updated_at=_NOW,
        )
        db_session.add(dec_global)
        db_session.add(dec_ticker)
        await db_session.flush()

        resp = test_client.get("/api/v1/strategies/latest/7203", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["ticker"] == "7203"
        assert data[0]["entry_allowed"] is True


class TestHistoryAPI:
    """GET /api/v1/strategies/history テスト"""

    def test_history_returns_empty(self, test_client):
        """評価履歴がなければ空リスト"""
        resp = test_client.get("/api/v1/strategies/history", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_history_requires_auth(self, test_client):
        """認証なしで 403"""
        resp = test_client.get(
            "/api/v1/strategies/history",
            headers={"Authorization": "Bearer bad"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_history_limit_parameter(
        self, test_client, db_session: AsyncSession
    ):
        """limit クエリパラメータで件数制限が機能する"""
        s = _strategy()
        db_session.add(s)
        await db_session.flush()

        for i in range(5):
            ev = StrategyEvaluation(
                strategy_id=s.id, ticker=None,
                evaluation_time=_NOW + timedelta(seconds=i),
                is_active=False, entry_allowed=False, size_ratio=0.0,
                matched_required_states_json=[], matched_forbidden_states_json=[],
                missing_required_states_json=[], blocking_reasons_json=[],
                evidence_json={},
            )
            db_session.add(ev)
        await db_session.flush()

        resp = test_client.get(
            "/api/v1/strategies/history?limit=3", headers=_AUTH
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 3


class TestStrategySeedAdminAPI:
    """POST /api/admin/strategies/init テスト"""

    def test_seed_returns_200(self, test_client):
        """初回 seed で 200 が返る"""
        resp = test_client.post(
            "/api/admin/strategies/init",
            headers={"Authorization": "Bearer changeme_before_production"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "seeded" in data
        assert data["seeded"] > 0

    def test_seed_is_idempotent(self, test_client):
        """2回呼んでも 200 が返り、重複しない"""
        resp1 = test_client.post(
            "/api/admin/strategies/init",
            headers={"Authorization": "Bearer changeme_before_production"},
        )
        resp2 = test_client.post(
            "/api/admin/strategies/init",
            headers={"Authorization": "Bearer changeme_before_production"},
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        # 2回目は seeded 件数が同じ（既存スキップ）
        assert resp1.json()["seeded"] == resp2.json()["seeded"]

    def test_seed_requires_auth(self, test_client):
        """認証なしで 403"""
        resp = test_client.post(
            "/api/admin/strategies/init",
            headers={"Authorization": "Bearer wrong_token"},
        )
        assert resp.status_code == 403
