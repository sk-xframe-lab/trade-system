"""
Strategy Engine Phase 6 テスト

カバー内容:
  - StrategyEvaluator 純粋判定ロジック（DB なし）
  - StrategyEngine DB 統合テスト
  - snapshot missing / stale の安全側挙動
  - API /current / /symbols/{ticker} / POST /recalculate
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.current_state_snapshot import CurrentStateSnapshot
from trade_app.models.strategy_condition import StrategyCondition
from trade_app.models.strategy_definition import StrategyDefinition
from trade_app.models.strategy_evaluation import StrategyEvaluation
from trade_app.services.strategy.engine import StrategyEngine
from trade_app.services.strategy.evaluator import StrategyEvaluator
from trade_app.services.strategy.repository import StrategyRepository
from trade_app.services.strategy.schemas import StrategyDecisionResult

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 9, 30, 0, tzinfo=_UTC)


# ─── ヘルパー: テスト用オブジェクト生成 ──────────────────────────────────────

def _strategy(
    code: str = "test_strategy",
    name: str = "テスト戦略",
    is_enabled: bool = True,
    direction: str = "both",
    priority: int = 0,
    max_size_ratio: float = 1.0,
) -> StrategyDefinition:
    now = datetime.now(_UTC)
    return StrategyDefinition(
        id=str(uuid.uuid4()),
        strategy_code=code,
        strategy_name=name,
        is_enabled=is_enabled,
        direction=direction,
        priority=priority,
        max_size_ratio=max_size_ratio,
        created_at=now,
        updated_at=now,
    )


def _condition(
    strategy_id: str,
    condition_type: str,
    layer: str,
    state_code: str,
    size_modifier: float | None = None,
) -> StrategyCondition:
    return StrategyCondition(
        id=str(uuid.uuid4()),
        strategy_id=strategy_id,
        condition_type=condition_type,
        layer=layer,
        state_code=state_code,
        operator="exists",
        size_modifier=size_modifier,
        created_at=datetime.now(_UTC),
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
# StrategyEvaluator 純粋テスト（DB なし）
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyEvaluatorRequired:
    """required_state 条件のテスト"""

    def test_required_state_present_entry_allowed(self):
        """required state が active にあれば entry_allowed=True"""
        ev = StrategyEvaluator()
        s = _strategy()
        conds = [_condition(s.id, "required_state", "market", "trend_up")]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"market": ["trend_up"]},
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is True
        assert result.is_active is True
        assert "market:trend_up" in result.matched_required_states
        assert result.missing_required_states == []

    def test_required_state_missing_blocked(self):
        """required state が active にない場合 entry_allowed=False"""
        ev = StrategyEvaluator()
        s = _strategy()
        conds = [_condition(s.id, "required_state", "market", "trend_up")]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"market": []},
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is False
        assert "market:trend_up" in result.missing_required_states
        assert any("missing_required_state" in r for r in result.blocking_reasons)

    def test_partial_required_missing_blocked(self):
        """required state が 2 件中 1 件しかない場合もブロック"""
        ev = StrategyEvaluator()
        s = _strategy()
        conds = [
            _condition(s.id, "required_state", "market", "trend_up"),
            _condition(s.id, "required_state", "time_window", "morning_trend_zone"),
        ]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={
                "market": ["trend_up"],
                "time_window": [],  # missing
            },
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is False
        assert "market:trend_up" in result.matched_required_states
        assert "time_window:morning_trend_zone" in result.missing_required_states

    def test_all_required_met_entry_allowed(self):
        """全 required state が揃えば entry_allowed=True"""
        ev = StrategyEvaluator()
        s = _strategy()
        conds = [
            _condition(s.id, "required_state", "market", "trend_up"),
            _condition(s.id, "required_state", "time_window", "morning_trend_zone"),
        ]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={
                "market": ["trend_up"],
                "time_window": ["morning_trend_zone"],
            },
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is True
        assert len(result.missing_required_states) == 0


class TestStrategyEvaluatorForbidden:
    """forbidden_state 条件のテスト"""

    def test_forbidden_state_blocks_entry(self):
        """forbidden state が active にある場合 entry_allowed=False"""
        ev = StrategyEvaluator()
        s = _strategy()
        conds = [_condition(s.id, "forbidden_state", "market", "risk_off")]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"market": ["risk_off"]},
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is False
        assert "market:risk_off" in result.matched_forbidden_states
        assert any("forbidden_state" in r for r in result.blocking_reasons)

    def test_forbidden_state_absent_does_not_block(self):
        """forbidden state が存在しなければブロックしない"""
        ev = StrategyEvaluator()
        s = _strategy()
        conds = [_condition(s.id, "forbidden_state", "market", "risk_off")]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"market": ["trend_up"]},
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is True
        assert result.matched_forbidden_states == []

    def test_size_ratio_zero_when_forbidden_blocks(self):
        """forbidden でブロックされた場合 size_ratio=0"""
        ev = StrategyEvaluator()
        s = _strategy(max_size_ratio=0.8)
        conds = [_condition(s.id, "forbidden_state", "market", "risk_off")]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"market": ["risk_off"]},
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.size_ratio == 0.0


class TestStrategyEvaluatorSizeModifier:
    """size_modifier 条件のテスト"""

    def test_size_modifier_applied_when_state_active(self):
        """size_modifier 条件の state が active なら size_ratio が縮小される"""
        ev = StrategyEvaluator()
        s = _strategy(max_size_ratio=1.0)
        conds = [_condition(s.id, "size_modifier", "symbol", "wide_spread", size_modifier=0.5)]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"symbol": ["wide_spread"]},
            ticker="7203",
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is True
        assert result.size_ratio == pytest.approx(0.5)
        assert result.applied_size_modifier == pytest.approx(0.5)

    def test_multiple_size_modifiers_min_adopted(self):
        """複数 size_modifier が成立する場合は最小値を採用（保守的）"""
        ev = StrategyEvaluator()
        s = _strategy(max_size_ratio=1.0)
        conds = [
            _condition(s.id, "size_modifier", "symbol", "wide_spread", size_modifier=0.5),
            _condition(s.id, "size_modifier", "symbol", "low_liquidity", size_modifier=0.8),
        ]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"symbol": ["wide_spread", "low_liquidity"]},
            ticker="7203",
            evaluation_time=_NOW,
        )
        assert result.size_ratio == pytest.approx(0.5)  # min(0.5, 0.8)
        assert result.applied_size_modifier == pytest.approx(0.5)

    def test_size_modifier_not_active_no_effect(self):
        """size_modifier 条件の state が inactive なら size_ratio に影響なし"""
        ev = StrategyEvaluator()
        s = _strategy(max_size_ratio=0.8)
        conds = [_condition(s.id, "size_modifier", "symbol", "wide_spread", size_modifier=0.3)]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"symbol": []},
            ticker="7203",
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is True
        assert result.size_ratio == pytest.approx(0.8)

    def test_max_size_ratio_applied_with_modifier(self):
        """max_size_ratio × size_modifier が size_ratio になる"""
        ev = StrategyEvaluator()
        s = _strategy(max_size_ratio=0.8)
        conds = [_condition(s.id, "size_modifier", "symbol", "wide_spread", size_modifier=0.5)]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"symbol": ["wide_spread"]},
            ticker="7203",
            evaluation_time=_NOW,
        )
        assert result.size_ratio == pytest.approx(0.4)  # 0.8 * 0.5


class TestStrategyEvaluatorControl:
    """strategy 制御（is_enabled / pre_blocking）のテスト"""

    def test_disabled_strategy_blocked(self):
        """is_enabled=False の strategy は entry_allowed=False"""
        ev = StrategyEvaluator()
        s = _strategy(is_enabled=False)
        result = ev.evaluate(
            strategy=s,
            conditions=[],
            active_states_by_layer={},
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is False
        assert "strategy_disabled" in result.blocking_reasons

    def test_pre_blocking_reasons_override_entry(self):
        """pre_blocking_reasons がある場合は条件が揃ってもブロック"""
        ev = StrategyEvaluator()
        s = _strategy()
        # 条件なし（通常は entry_allowed=True になるはず）
        result = ev.evaluate(
            strategy=s,
            conditions=[],
            active_states_by_layer={"market": ["trend_up"]},
            ticker=None,
            evaluation_time=_NOW,
            pre_blocking_reasons=["state_snapshot_missing:time_window"],
        )
        assert result.entry_allowed is False
        assert "state_snapshot_missing:time_window" in result.blocking_reasons

    def test_no_conditions_no_pre_block_entry_allowed(self):
        """条件なし・pre_block なし・is_enabled=True → entry_allowed=True"""
        ev = StrategyEvaluator()
        s = _strategy()
        result = ev.evaluate(
            strategy=s,
            conditions=[],
            active_states_by_layer={},
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is True

    def test_evidence_contains_active_layers(self):
        """evidence に active_states_by_layer の内容が含まれる"""
        ev = StrategyEvaluator()
        s = _strategy()
        result = ev.evaluate(
            strategy=s,
            conditions=[],
            active_states_by_layer={"market": ["trend_up"], "time_window": ["morning_trend_zone"]},
            ticker="7203",
            evaluation_time=_NOW,
        )
        assert "active_states_by_layer" in result.evidence
        assert result.evidence["active_states_by_layer"]["market"] == ["trend_up"]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6 補強: size_ratio=0 安全チェック テスト
# ─────────────────────────────────────────────────────────────────────────────

class TestStrategyEvaluatorSizeRatioZero:
    """size_ratio=0 のとき entry_allowed=False になることを保証するテスト"""

    def test_size_modifier_zero_blocks_entry(self):
        """
        size_modifier=0.0 の条件が成立すると size_ratio=0.0 になる。
        このとき entry_allowed=False で blocking_reasons に 'size_ratio_zero' が入る。
        （Signal Router との接続時に発注サイズ 0 で entry_allowed=True は危険）
        """
        ev = StrategyEvaluator()
        s = _strategy(max_size_ratio=1.0)
        conds = [_condition(s.id, "size_modifier", "symbol", "wide_spread", size_modifier=0.0)]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"symbol": ["wide_spread"]},
            ticker="7203",
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is False
        assert result.size_ratio == 0.0
        assert "size_ratio_zero" in result.blocking_reasons

    def test_max_size_ratio_zero_blocks_entry(self):
        """
        strategy.max_size_ratio=0.0 は size_ratio=0 になる → entry_allowed=False
        """
        ev = StrategyEvaluator()
        s = _strategy(max_size_ratio=0.0)
        result = ev.evaluate(
            strategy=s,
            conditions=[],
            active_states_by_layer={},
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is False
        assert "size_ratio_zero" in result.blocking_reasons

    def test_positive_size_ratio_allows_entry(self):
        """正の size_ratio なら size_ratio_zero によるブロックは発生しない"""
        ev = StrategyEvaluator()
        s = _strategy(max_size_ratio=0.5)
        result = ev.evaluate(
            strategy=s,
            conditions=[],
            active_states_by_layer={},
            ticker=None,
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is True
        assert result.size_ratio == pytest.approx(0.5)
        assert "size_ratio_zero" not in result.blocking_reasons

    def test_size_modifier_nonzero_no_spurious_block(self):
        """size_modifier=0.5（非ゼロ）なら size_ratio_zero は発生しない"""
        ev = StrategyEvaluator()
        s = _strategy(max_size_ratio=1.0)
        conds = [_condition(s.id, "size_modifier", "symbol", "wide_spread", size_modifier=0.5)]
        result = ev.evaluate(
            strategy=s,
            conditions=conds,
            active_states_by_layer={"symbol": ["wide_spread"]},
            ticker="7203",
            evaluation_time=_NOW,
        )
        assert result.entry_allowed is True
        assert result.size_ratio == pytest.approx(0.5)
        assert "size_ratio_zero" not in result.blocking_reasons


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6 補強: GET /current の symbol 条件挙動テスト
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestStrategyEngineCurrentSymbolBehavior:
    """
    GET /current（ticker=None）での symbol 条件挙動。

    設計仕様:
      ticker=None の評価では active_states_by_layer に "symbol" キーが存在しない。
      layer="symbol" の required_state 条件は常に missing_required_state として記録され、
      entry_allowed=False になる。これは意図した設計（symbol 条件は ticker 別評価で使う）。
    """

    async def test_symbol_required_missing_in_global_eval(self, db_session: AsyncSession):
        """
        ticker=None 評価で layer=symbol の required_state を持つ strategy は
        entry_allowed=False になり、missing_required_states に symbol 条件が記録される
        """
        now = datetime.now(_UTC)
        # market + time_window snapshot のみ（symbol なし）
        db_session.add(_snapshot("market", None, ["trend_up"]))
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"]))
        await db_session.flush()

        # symbol 条件を持つ strategy
        s = StrategyDefinition(
            strategy_code="symbol_cond_test",
            strategy_name="Symbol条件テスト",
            is_enabled=True, direction="long",
            priority=0, max_size_ratio=1.0,
            created_at=now, updated_at=now,
        )
        db_session.add(s)
        await db_session.flush()
        db_session.add(StrategyCondition(
            strategy_id=s.id,
            condition_type="required_state",
            layer="symbol",
            state_code="symbol_trend_up",
            operator="exists",
            created_at=now,
        ))
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)

        assert len(results) == 1
        assert results[0].entry_allowed is False
        assert "symbol:symbol_trend_up" in results[0].missing_required_states
        # pre_blocking ではなく条件 missing によるブロック
        assert any("missing_required_state:symbol" in r for r in results[0].blocking_reasons)
        assert not any("state_snapshot_missing" in r for r in results[0].blocking_reasons)

    async def test_ticker_eval_resolves_symbol_condition(self, db_session: AsyncSession):
        """
        同じ strategy を ticker="7203" で評価すると symbol 条件が解決され
        entry_allowed=True になる
        """
        now = datetime.now(_UTC)
        db_session.add(_snapshot("market", None, ["trend_up"]))
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"]))
        db_session.add(_snapshot("symbol", "7203", ["symbol_trend_up"]))
        await db_session.flush()

        s = StrategyDefinition(
            strategy_code="symbol_resolve_test",
            strategy_name="Symbol解決テスト",
            is_enabled=True, direction="long",
            priority=0, max_size_ratio=1.0,
            created_at=now, updated_at=now,
        )
        db_session.add(s)
        await db_session.flush()
        db_session.add(StrategyCondition(
            strategy_id=s.id,
            condition_type="required_state",
            layer="symbol",
            state_code="symbol_trend_up",
            operator="exists",
            created_at=now,
        ))
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker="7203", evaluation_time=_NOW)

        assert results[0].entry_allowed is True
        assert results[0].missing_required_states == []


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6 補強: stale 判定基準時刻テスト
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestStrategyEngineStaleBasetime:
    """
    stale 判定は StrategyEngine 実行時刻（evaluation_time）を基準に行う。
    API 呼び出し時刻ではなく、engine に渡す evaluation_time が基準になる。
    """

    async def _seed_simple_strategy(self, db: AsyncSession) -> None:
        now = datetime.now(_UTC)
        db.add(StrategyDefinition(
            strategy_code="stale_base_test",
            strategy_name="Stale基準テスト",
            is_enabled=True, direction="both",
            priority=0, max_size_ratio=1.0,
            created_at=now, updated_at=now,
        ))
        await db.flush()

    async def test_stale_relative_to_evaluation_time_not_wall_clock(
        self, db_session: AsyncSession
    ):
        """
        snapshot が 200 秒前に作成されていても、evaluation_time を
        snapshot.updated_at の直後（10秒後）に設定すれば stale にならない。
        これは「stale 判定基準 = engine evaluation_time」であることを証明する。
        """
        await self._seed_simple_strategy(db_session)
        snapshot_time = datetime(2024, 11, 6, 9, 0, 0, tzinfo=_UTC)
        # evaluation_time = snapshot_time + 10秒 → 180秒以内なのでフレッシュ
        eval_time = snapshot_time + timedelta(seconds=10)

        db_session.add(_snapshot("market", None, ["trend_up"], updated_at=snapshot_time))
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"], updated_at=snapshot_time))
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=eval_time)

        assert not any("stale" in r for r in results[0].blocking_reasons), (
            "evaluation_time から見て 10 秒前の snapshot は stale ではないはず"
        )

    async def test_stale_triggered_by_evaluation_time_far_future(
        self, db_session: AsyncSession
    ):
        """
        snapshot が同じでも、evaluation_time を 300 秒後に設定すると stale になる。
        API 呼び出し時刻でなく evaluation_time 基準であることを確認。
        """
        await self._seed_simple_strategy(db_session)
        snapshot_time = datetime(2024, 11, 6, 9, 0, 0, tzinfo=_UTC)
        # evaluation_time = snapshot_time + 300秒 → 180秒を超えるので stale
        eval_time = snapshot_time + timedelta(seconds=300)

        db_session.add(_snapshot("market", None, ["trend_up"], updated_at=snapshot_time))
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"], updated_at=snapshot_time))
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=eval_time)

        assert any("state_snapshot_stale:market" in r for r in results[0].blocking_reasons), (
            "evaluation_time から見て 300 秒前の snapshot は stale のはず"
        )


# ─────────────────────────────────────────────────────────────────────────────
# StrategyEngine DB 統合テスト
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestStrategyEngineSafety:
    """snapshot missing / stale の安全側挙動テスト"""

    async def _seed_strategy(self, db: AsyncSession) -> StrategyDefinition:
        """テスト用 strategy を DB に保存して返す"""
        now = datetime.now(_UTC)
        s = StrategyDefinition(
            strategy_code="test_safety",
            strategy_name="安全側テスト戦略",
            is_enabled=True,
            direction="both",
            priority=0,
            max_size_ratio=1.0,
            created_at=now,
            updated_at=now,
        )
        db.add(s)
        await db.flush()
        return s

    async def test_market_snapshot_missing_blocked(self, db_session: AsyncSession):
        """market snapshot 未存在 → entry_allowed=False"""
        await self._seed_strategy(db_session)
        # time_window snapshot のみ存在（market なし）
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"]))
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)
        assert len(results) == 1
        assert results[0].entry_allowed is False
        assert any("state_snapshot_missing:market" in r for r in results[0].blocking_reasons)

    async def test_time_window_snapshot_missing_blocked(self, db_session: AsyncSession):
        """time_window snapshot 未存在 → entry_allowed=False"""
        await self._seed_strategy(db_session)
        # market snapshot のみ存在（time_window なし）
        db_session.add(_snapshot("market", None, ["trend_up"]))
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)
        assert results[0].entry_allowed is False
        assert any("state_snapshot_missing:time_window" in r for r in results[0].blocking_reasons)

    async def test_symbol_snapshot_missing_blocked(self, db_session: AsyncSession):
        """ticker 評価時に symbol snapshot 未存在 → entry_allowed=False"""
        await self._seed_strategy(db_session)
        db_session.add(_snapshot("market", None, ["trend_up"]))
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"]))
        # symbol snapshot なし
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker="7203", evaluation_time=_NOW)
        assert results[0].entry_allowed is False
        assert any("state_snapshot_missing:symbol" in r for r in results[0].blocking_reasons)

    async def test_stale_snapshot_blocked(self, db_session: AsyncSession):
        """snapshot が stale（STRATEGY_MAX_STATE_AGE_SEC 超過）→ entry_allowed=False"""
        await self._seed_strategy(db_session)
        stale_time = _NOW - timedelta(seconds=300)  # 5分前（デフォルト 180s を超過）
        db_session.add(_snapshot("market", None, ["trend_up"], updated_at=stale_time))
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"]))
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)
        assert results[0].entry_allowed is False
        assert any("state_snapshot_stale:market" in r for r in results[0].blocking_reasons)

    async def test_fresh_snapshot_not_stale(self, db_session: AsyncSession):
        """snapshot が新鮮（60秒前）なら stale 扱いにならない"""
        await self._seed_strategy(db_session)
        fresh_time = _NOW - timedelta(seconds=60)
        db_session.add(_snapshot("market", None, ["trend_up"], updated_at=fresh_time))
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"], updated_at=fresh_time))
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)
        # safety reasons なし → stale ではない
        assert not any("stale" in r for r in results[0].blocking_reasons)


@pytest.mark.asyncio
class TestStrategyEngineEvaluation:
    """StrategyEngine の評価・保存テスト"""

    async def _setup_snapshots(self, db: AsyncSession, ticker: str | None = None) -> None:
        """market + time_window + オプション symbol snapshot を作成"""
        db.add(_snapshot("market", None, ["trend_up"]))
        db.add(_snapshot("time_window", None, ["morning_trend_zone"]))
        if ticker:
            db.add(_snapshot("symbol", ticker, ["symbol_trend_up"]))
        await db.flush()

    async def _seed_full_strategy(self, db: AsyncSession) -> StrategyDefinition:
        """conditions 付き strategy を DB に保存する"""
        now = datetime.now(_UTC)
        s = StrategyDefinition(
            strategy_code="full_test",
            strategy_name="フル条件テスト",
            is_enabled=True,
            direction="long",
            priority=10,
            max_size_ratio=1.0,
            created_at=now,
            updated_at=now,
        )
        db.add(s)
        await db.flush()

        conds = [
            StrategyCondition(
                strategy_id=s.id,
                condition_type="required_state",
                layer="market",
                state_code="trend_up",
                operator="exists",
                created_at=now,
            ),
            StrategyCondition(
                strategy_id=s.id,
                condition_type="required_state",
                layer="time_window",
                state_code="morning_trend_zone",
                operator="exists",
                created_at=now,
            ),
            StrategyCondition(
                strategy_id=s.id,
                condition_type="forbidden_state",
                layer="market",
                state_code="risk_off",
                operator="exists",
                created_at=now,
            ),
        ]
        for c in conds:
            db.add(c)
        await db.flush()
        return s

    async def test_all_conditions_met_entry_allowed(self, db_session: AsyncSession):
        """全条件成立 → entry_allowed=True"""
        await self._setup_snapshots(db_session)
        await self._seed_full_strategy(db_session)

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)
        assert len(results) == 1
        assert results[0].entry_allowed is True
        assert results[0].size_ratio > 0

    async def test_forbidden_state_active_blocked(self, db_session: AsyncSession):
        """forbidden state が active → entry_allowed=False"""
        db_session.add(_snapshot("market", None, ["trend_up", "risk_off"]))
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"]))
        await db_session.flush()
        await self._seed_full_strategy(db_session)

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)
        assert results[0].entry_allowed is False
        assert any("forbidden_state:market:risk_off" in r for r in results[0].blocking_reasons)

    async def test_ticker_specific_evaluation(self, db_session: AsyncSession):
        """ticker 指定の評価では symbol states が参照される"""
        await self._setup_snapshots(db_session, ticker="7203")

        now = datetime.now(_UTC)
        s = StrategyDefinition(
            strategy_code="symbol_test",
            strategy_name="銘柄テスト",
            is_enabled=True,
            direction="both",
            priority=0,
            max_size_ratio=1.0,
            created_at=now,
            updated_at=now,
        )
        db_session.add(s)
        await db_session.flush()
        cond = StrategyCondition(
            strategy_id=s.id,
            condition_type="required_state",
            layer="symbol",
            state_code="symbol_trend_up",
            operator="exists",
            created_at=now,
        )
        db_session.add(cond)
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker="7203", evaluation_time=_NOW)
        assert results[0].entry_allowed is True
        assert results[0].ticker == "7203"

    async def test_evaluation_saved_to_db(self, db_session: AsyncSession):
        """評価結果が strategy_evaluations テーブルに保存される"""
        await self._setup_snapshots(db_session)
        await self._seed_full_strategy(db_session)

        engine = StrategyEngine(db_session)
        await engine.run(ticker=None, evaluation_time=_NOW)

        repo = StrategyRepository(db_session)
        saved = await repo.get_latest_evaluations(ticker=None)
        assert len(saved) == 1
        assert saved[0].entry_allowed is True

    async def test_disabled_strategy_blocked_via_engine(self, db_session: AsyncSession):
        """is_enabled=False の strategy は engine 経由でもブロックされる"""
        await self._setup_snapshots(db_session)

        now = datetime.now(_UTC)
        s = StrategyDefinition(
            strategy_code="disabled_test",
            strategy_name="無効テスト",
            is_enabled=False,
            direction="both",
            priority=0,
            max_size_ratio=1.0,
            created_at=now,
            updated_at=now,
        )
        db_session.add(s)
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker=None, evaluation_time=_NOW)
        assert results[0].entry_allowed is False
        assert "strategy_disabled" in results[0].blocking_reasons

    async def test_size_modifier_via_engine(self, db_session: AsyncSession):
        """size_modifier 条件が engine 経由でも適用される"""
        db_session.add(_snapshot("market", None, ["trend_up"]))
        db_session.add(_snapshot("time_window", None, ["morning_trend_zone"]))
        db_session.add(_snapshot("symbol", "7203", ["wide_spread"]))
        await db_session.flush()

        now = datetime.now(_UTC)
        s = StrategyDefinition(
            strategy_code="size_test",
            strategy_name="サイズテスト",
            is_enabled=True,
            direction="both",
            priority=0,
            max_size_ratio=1.0,
            created_at=now,
            updated_at=now,
        )
        db_session.add(s)
        await db_session.flush()
        db_session.add(StrategyCondition(
            strategy_id=s.id,
            condition_type="size_modifier",
            layer="symbol",
            state_code="wide_spread",
            operator="exists",
            size_modifier=0.5,
            created_at=now,
        ))
        await db_session.flush()

        engine = StrategyEngine(db_session)
        results = await engine.run(ticker="7203", evaluation_time=_NOW)
        assert results[0].entry_allowed is True
        assert results[0].size_ratio == pytest.approx(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# Seed テスト
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestStrategySeed:
    """seed_strategies のテスト"""

    async def test_seed_creates_two_strategies(self, db_session: AsyncSession):
        """seed_strategies を実行すると 2 つの strategy が作成される"""
        from trade_app.services.strategy.seed import seed_strategies
        seeded = await seed_strategies(db_session)
        assert len(seeded) == 2
        codes = {s.strategy_code for s in seeded}
        assert "long_morning_trend" in codes
        assert "short_risk_off_rebound" in codes

    async def test_seed_idempotent(self, db_session: AsyncSession):
        """seed_strategies は 2 度実行しても重複しない"""
        from trade_app.services.strategy.seed import seed_strategies
        await seed_strategies(db_session)
        await seed_strategies(db_session)

        repo = StrategyRepository(db_session)
        all_s = await repo.get_all_strategies(enabled_only=False)
        codes = [s.strategy_code for s in all_s]
        assert codes.count("long_morning_trend") == 1

    async def test_seed_conditions_exist(self, db_session: AsyncSession):
        """seed された strategy に conditions が付いている"""
        from trade_app.services.strategy.seed import seed_strategies
        seeded = await seed_strategies(db_session)

        for s in seeded:
            repo = StrategyRepository(db_session)
            conds = await repo.get_conditions_for_strategy(s.id)
            assert len(conds) >= 2, f"{s.strategy_code} に条件が不足"


# ─────────────────────────────────────────────────────────────────────────────
# API テスト
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def test_client(db_engine):
    """Strategy API テスト用クライアント"""
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from trade_app.main import app
    from trade_app.models.database import get_db

    session_factory = async_sessionmaker(
        bind=db_engine, expire_on_commit=False, autoflush=False
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


_AUTH = {"Authorization": "Bearer changeme_before_production"}


class TestStrategyAPI:
    """Strategy API エンドポイントのテスト"""

    def test_get_current_empty(self, test_client):
        """GET /current — 評価結果がない場合は空リストを返す"""
        resp = test_client.get("/api/v1/strategies/current", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_current_unauthorized(self, test_client):
        """GET /current — 無効トークン → 403"""
        resp = test_client.get(
            "/api/v1/strategies/current",
            headers={"Authorization": "Bearer invalid_token"},
        )
        assert resp.status_code == 403

    def test_get_symbols_empty(self, test_client):
        """GET /symbols/{ticker} — 評価結果がない場合は空リスト"""
        resp = test_client.get("/api/v1/strategies/symbols/7203", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_recalculate_no_strategies(self, test_client):
        """POST /recalculate — strategy がない場合 evaluated=0"""
        resp = test_client.post(
            "/api/v1/strategies/recalculate",
            headers=_AUTH,
            json={},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["evaluated"] == 0
        assert data["results"] == []

    def test_recalculate_unauthorized(self, test_client):
        """POST /recalculate — 無効トークン → 403"""
        resp = test_client.post(
            "/api/v1/strategies/recalculate",
            headers={"Authorization": "Bearer invalid_token"},
            json={},
        )
        assert resp.status_code == 403

    def test_recalculate_with_ticker_missing_snapshots(self, test_client, db_engine):
        """POST /recalculate — ticker 指定・snapshot なし → blocked"""
        import asyncio
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from trade_app.services.strategy.seed import seed_strategies

        async def setup():
            factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
            async with factory() as session:
                await seed_strategies(session)
                await session.commit()

        asyncio.get_event_loop().run_until_complete(setup())

        resp = test_client.post(
            "/api/v1/strategies/recalculate",
            headers=_AUTH,
            json={"ticker": "7203"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["evaluated"] > 0
        for r in data["results"]:
            assert r["entry_allowed"] is False
            # blocking_reasons に snapshot_missing が含まれるはず
            assert any("state_snapshot_missing" in br for br in r["blocking_reasons"])

    def test_get_current_after_recalculate(self, test_client, db_engine):
        """POST /recalculate 後に GET /current でデータを取得できる"""
        import asyncio
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from trade_app.services.strategy.seed import seed_strategies
        from trade_app.models.current_state_snapshot import CurrentStateSnapshot

        async def setup():
            factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
            async with factory() as session:
                await seed_strategies(session)
                # market + time_window snapshot を追加
                session.add(CurrentStateSnapshot(
                    id=str(uuid.uuid4()),
                    layer="market", target_type="market", target_code=None,
                    active_states_json=["trend_up"],
                    state_summary_json={},
                    updated_at=datetime.now(_UTC),
                ))
                session.add(CurrentStateSnapshot(
                    id=str(uuid.uuid4()),
                    layer="time_window", target_type="time_window", target_code=None,
                    active_states_json=["morning_trend_zone"],
                    state_summary_json={},
                    updated_at=datetime.now(_UTC),
                ))
                await session.commit()

        asyncio.get_event_loop().run_until_complete(setup())

        # recalculate
        resp = test_client.post(
            "/api/v1/strategies/recalculate", headers=_AUTH, json={}
        )
        assert resp.status_code == 200

        # GET /current でデータ取得
        resp = test_client.get("/api/v1/strategies/current", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0
        for item in data:
            assert "strategy_code" in item
            assert "entry_allowed" in item
            assert "blocking_reasons" in item
            assert "evaluation_time" in item
