"""
Phase D+1 — price_stale rule 追加テスト

確認項目:
  A. rule 直テスト (_rule_price_stale 直接呼び出し)
     1. current_price=None → missing_price
     2. current_price あり / last_updated=None → missing_timestamp
     3. current_price あり / last_updated が 60秒以上前 → stale_price
     4. current_price あり / last_updated が 60秒未満 → 非発火
     5. 境界値: age_sec ちょうど 60秒 → 発火
     6. stale_price evidence に age_sec / threshold_sec が含まれる
     7. "last_updated" キーがない data → 評価しない (gate)

  B. orchestrator 経由テスト
     8. _evaluate_symbol() 経由でも price_stale が返る
     9. wide_spread と price_stale が同時 active になれる

  C. 遷移テスト (engine.run() 経由)
     10. 初回 stale → INSERT
     11. stale 継続 → INSERT なし
     12. stale 解消 → soft-expire
     13. stale 再発火 → 再 INSERT

設計:
  - price_stale の gate: "last_updated" キーが data に存在する場合のみ評価
  - threshold_sec = 60 (PRICE_STALE_THRESHOLD_SEC)
  - 既存の wide_spread テストへのリグレッションがないこと
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.services.market_state import symbol_evaluator as _mod
from trade_app.services.market_state.engine import MarketStateEngine
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult
from trade_app.services.market_state.symbol_evaluator import SymbolStateEvaluator

_UTC = timezone.utc
THRESHOLD = SymbolStateEvaluator.PRICE_STALE_THRESHOLD_SEC  # 60.0

# evaluation_time 基準
_EVAL_TIME = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

# stale: last_updated が 120秒前
_LAST_UPDATED_STALE = datetime(2024, 11, 6, 9, 58, 0, tzinfo=_UTC)
# fresh: last_updated が 30秒前
_LAST_UPDATED_FRESH = datetime(2024, 11, 6, 9, 59, 30, tzinfo=_UTC)
# boundary: ちょうど 60秒前
_LAST_UPDATED_BOUNDARY = datetime(2024, 11, 6, 9, 59, 0, tzinfo=_UTC)


# ─── テスト用 make ヘルパー ────────────────────────────────────────────────────

def _make(
    ticker: str,
    state_code: str,
    score: float,
    confidence: float,
    evidence: dict[str, Any],
) -> StateEvaluationResult:
    return StateEvaluationResult(
        layer="symbol",
        target_type="symbol",
        target_code=ticker,
        state_code=state_code,
        score=max(0.0, min(1.0, score)),
        confidence=max(0.0, min(1.0, confidence)),
        evidence=evidence,
    )


def _call_rule(data: dict[str, Any], eval_time: datetime = _EVAL_TIME):
    result, _diag = _mod._rule_price_stale(
        "7203", data,
        evaluation_time=eval_time,
        threshold_sec=THRESHOLD,
        make=_make,
    )
    return result


def _ctx(eval_time: datetime = _EVAL_TIME, **fields) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=eval_time,
        symbol_data={"7203": fields},
    )


def _engine_ctx(hour: int, symbol_data: dict) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=datetime(2024, 11, 6, hour, 0, 0, tzinfo=_UTC),
        symbol_data=symbol_data,
    )


# ─── A. rule 直テスト ─────────────────────────────────────────────────────────

class TestRulePriceStaleDirect:
    """_rule_price_stale() を直接呼び出す単体テスト"""

    # ── 発火ケース ──

    def test_missing_price_fires(self):
        """current_price=None → missing_price 発火"""
        result = _call_rule({"current_price": None, "last_updated": _LAST_UPDATED_STALE})
        assert result is not None
        assert result.state_code == "price_stale"
        assert result.evidence["reason"] == "missing_price"

    def test_missing_price_evidence(self):
        """missing_price の evidence フィールドが正しい"""
        result = _call_rule({"current_price": None, "last_updated": None})
        assert result is not None
        ev = result.evidence
        assert ev["current_price"] is None
        assert ev["last_updated"] is None
        assert "evaluation_time" in ev
        assert ev["threshold_sec"] == THRESHOLD

    def test_missing_timestamp_fires(self):
        """current_price あり / last_updated=None → missing_timestamp 発火"""
        result = _call_rule({"current_price": 1000.0, "last_updated": None})
        assert result is not None
        assert result.state_code == "price_stale"
        assert result.evidence["reason"] == "missing_timestamp"

    def test_missing_timestamp_evidence(self):
        """missing_timestamp の evidence に current_price が含まれる"""
        result = _call_rule({"current_price": 1500.0, "last_updated": None})
        assert result is not None
        ev = result.evidence
        assert ev["current_price"] == 1500.0
        assert ev["last_updated"] is None

    def test_stale_price_fires_when_over_threshold(self):
        """age_sec = 120 >= 60 → stale_price 発火"""
        result = _call_rule({"current_price": 1000.0, "last_updated": _LAST_UPDATED_STALE})
        assert result is not None
        assert result.state_code == "price_stale"
        assert result.evidence["reason"] == "stale_price"

    def test_stale_price_fires_at_boundary(self):
        """age_sec = ちょうど 60秒 → 発火（>=）"""
        result = _call_rule({"current_price": 1000.0, "last_updated": _LAST_UPDATED_BOUNDARY})
        assert result is not None
        assert result.evidence["reason"] == "stale_price"

    def test_stale_price_evidence_contains_age_sec(self):
        """stale_price の evidence に age_sec が含まれる"""
        result = _call_rule({"current_price": 1000.0, "last_updated": _LAST_UPDATED_STALE})
        assert result is not None
        ev = result.evidence
        assert "age_sec" in ev
        assert ev["age_sec"] == pytest.approx(120.0, abs=1.0)
        assert ev["threshold_sec"] == THRESHOLD
        assert "evaluation_time" in ev
        assert "last_updated" in ev

    def test_stale_price_score_is_1(self):
        """price_stale は常に score=1.0（バイナリ）"""
        result = _call_rule({"current_price": 1000.0, "last_updated": _LAST_UPDATED_STALE})
        assert result is not None
        assert result.score == 1.0

    # ── 非発火ケース ──

    def test_fresh_price_does_not_fire(self):
        """age_sec = 30 < 60 → 非発火"""
        result = _call_rule({"current_price": 1000.0, "last_updated": _LAST_UPDATED_FRESH})
        assert result is None

    def test_gate_no_last_updated_key(self):
        """
        "last_updated" キーが data にない → 評価しない（gate）

        last_updated キーのない既存テストデータへの影響がゼロであることを確認する。
        """
        result = _call_rule({"current_price": 1000.0})  # last_updated キー自体がない
        assert result is None

    def test_gate_no_last_updated_key_even_with_none_price(self):
        """current_price=None でも last_updated キーがなければ評価しない"""
        result = _call_rule({"current_price": None})  # last_updated キー自体がない
        assert result is None

    def test_just_before_boundary_does_not_fire(self):
        """age_sec = 59秒 → 非発火（< 60）"""
        last_updated = _EVAL_TIME - timedelta(seconds=59)
        result = _call_rule({"current_price": 1000.0, "last_updated": last_updated})
        assert result is None


# ─── B. orchestrator 経由テスト ───────────────────────────────────────────────

class TestOrchestratorPriceStale:
    """SymbolStateEvaluator.evaluate() 経由で price_stale が返ること"""

    def test_price_stale_via_evaluate(self):
        """stale データを渡すと price_stale が evaluate() から返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=1000.0, last_updated=_LAST_UPDATED_STALE)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "price_stale" in codes

    def test_fresh_price_not_stale_via_evaluate(self):
        """fresh データでは price_stale が返らない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=1000.0, last_updated=_LAST_UPDATED_FRESH)
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "price_stale" for r in results)

    def test_wide_spread_and_price_stale_coexist(self):
        """wide_spread と price_stale が同時に active になる"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=3000.0,
            best_bid=2994.0, best_ask=3006.0,  # wide_spread: spread_rate=0.4% >= 0.3%
            last_updated=_LAST_UPDATED_STALE,   # price_stale: 120s stale
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "wide_spread" in codes, "wide_spread が発火していない"
        assert "price_stale" in codes, "price_stale が発火していない"

    def test_no_last_updated_key_does_not_fire_in_existing_data(self):
        """
        last_updated キーを含まない従来データでは price_stale が発火しない。
        既存テストのリグレッションが起きないことを確認する。
        """
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(  # last_updated キーなし（従来テストと同じ）
            current_open=3060.0, prev_close=3000.0,
            current_volume=300_000, avg_volume_same_time=100_000,
            rsi=80.0,
        )
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "price_stale" for r in results)


# ─── C. 遷移テスト ────────────────────────────────────────────────────────────

# stale/fresh データ（他の state が発火しないように最小構成）
_STALE = {"current_price": 1000.0, "last_updated": _LAST_UPDATED_STALE}
_FRESH = {"current_price": 1000.0, "last_updated": _LAST_UPDATED_FRESH}


@pytest.mark.asyncio
class TestPriceStaleTransitions:
    """price_stale の activated / continued / deactivated 遷移テスト"""

    async def test_initial_stale_inserts_row(self, db_session: AsyncSession):
        """初回 stale → StateEvaluation が INSERT される（is_active=True）"""
        engine = MarketStateEngine(db_session)
        ctx = _engine_ctx(1, {"7203": _STALE})
        ctx.evaluation_time = _EVAL_TIME  # 固定 eval_time で age_sec を確定させる
        results = await engine.run(ctx)

        stale_results = [r for r in results if r.state_code == "price_stale"]
        assert len(stale_results) == 1
        assert stale_results[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "price_stale" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """
        run1: price_stale 発火 → 1 行 INSERT
        run2: price_stale 継続 → INSERT なし（合計 1 行）
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _STALE},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _STALE},
        )

        await engine.run(ctx1)
        results2 = await engine.run(ctx2)

        # run2 の price_stale は継続（is_new_activation=False）
        stale2 = [r for r in results2 if r.state_code == "price_stale" and r.target_code == "7203"]
        assert len(stale2) == 1
        assert stale2[0].is_new_activation is False

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        stale_rows = [r for r in history if r.state_code == "price_stale"]
        assert len(stale_rows) == 1, f"継続で INSERT が発生した。期待1行、実際{len(stale_rows)}行"

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """
        run1: price_stale 発火 → is_active=True
        run2: fresh data → is_active=False
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _STALE},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _FRESH},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        stale_rows = [r for r in history if r.state_code == "price_stale"]
        assert len(stale_rows) == 1
        assert stale_rows[0].is_active is False, "解消後 price_stale は is_active=False になるべき"

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """
        run1: stale → 行1 (is_active=True)
        run2: fresh → 行1 (is_active=False)
        run3: stale → 行2 (is_active=True)
        → DB に price_stale が 2 行
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _STALE},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _FRESH},
        )
        ctx3 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=20),
            symbol_data={"7203": _STALE},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)
        results3 = await engine.run(ctx3)

        # run3 の price_stale は再活性化
        stale3 = [r for r in results3 if r.state_code == "price_stale" and r.target_code == "7203"]
        assert len(stale3) == 1
        assert stale3[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        stale_rows = [r for r in history if r.state_code == "price_stale"]
        assert len(stale_rows) == 2, f"再発火で 2 行になるべき、実際 {len(stale_rows)} 行"
        active_rows = [r for r in stale_rows if r.is_active is True]
        assert len(active_rows) == 1
