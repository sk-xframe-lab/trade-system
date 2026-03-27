"""
Phase F — symbol_volatility_high rule 化テスト

確認項目:
  A. _rule_symbol_volatility_high() 直テスト
     1. current_price=None → None（ガード）
     2. current_price=0 → None（ゼロ除算ガード）
     3. current_price<0 → None（無効値ガード）
     4. atr=None → None（ガード）
     5. atr_ratio < threshold → None（非発火）
     6. atr_ratio = threshold ちょうど → 発火（>=）
     7. atr_ratio > threshold → 発火
     8. score = min(1.0, atr_ratio / 0.05)
     9. score at threshold (0.02): 0.02/0.05 = 0.4
    10. score cap at 5% ATR: 0.05/0.05 = 1.0
    11. score > 1.0 にはならない（min クランプ）
    12. evidence フィールド（current_price / atr / atr_ratio / threshold / rule）
    13. evidence["atr_ratio"] は 6桁丸め

  B. orchestrator 経由テスト
    14. evaluate() 経由で symbol_volatility_high を検出
    15. atr_ratio < threshold では発火しない
    16. symbol_volatility_high と gap_up_open が共存する
    17. symbol_volatility_high と overextended が共存する（同一データで同時発火）
    18. current_price=None では発火しない

  C. 構造確認
    19. _rule_symbol_volatility_high が module レベルに存在する
    20. _evaluate_symbol() のソースに volatility_high 専用 score 式が残っていない
    21. _evaluate_symbol() のソースに _rule_symbol_volatility_high の呼び出しがある

  D. 遷移テスト（engine.run() 経由）
    22. 初回発火 → INSERT
    23. 継続 → INSERT なし
    24. 解消 → soft-expire
    25. 再発火 → 再 INSERT

設計:
  - _rule_symbol_volatility_high() は current_price / atr のみに依存する独立 rule
  - atr が None かつ current_price が有効でも発火しない（ガード）
  - symbol_range とは別: symbol_range は ATR < threshold / volatility_high は ATR >= threshold
  - current_price / atr は _evaluate_symbol() のトップ抽出に残る（symbol_range が使用）
"""
from __future__ import annotations

import inspect
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
_EVAL_TIME = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)

ATR_RATIO_HIGH = SymbolStateEvaluator.ATR_RATIO_HIGH  # 0.02


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


def _call_rule(data: dict[str, Any]) -> StateEvaluationResult | None:
    result, _diag = _mod._rule_symbol_volatility_high(
        "7203", data,
        atr_ratio_high=ATR_RATIO_HIGH,
        make=_make,
    )
    return result


def _ctx(**fields) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=_EVAL_TIME,
        symbol_data={"7203": fields},
    )


# ─── A. _rule_symbol_volatility_high() 直テスト ───────────────────────────────

class TestRuleVolatilityHighDirect:
    """_rule_symbol_volatility_high() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_returns_none_when_current_price_none(self):
        """current_price=None → None"""
        result = _call_rule({"current_price": None, "atr": 20.0})
        assert result is None

    def test_returns_none_when_current_price_zero(self):
        """current_price=0 → None（ゼロ除算ガード）"""
        result = _call_rule({"current_price": 0, "atr": 20.0})
        assert result is None

    def test_returns_none_when_current_price_negative(self):
        """current_price=-1 → None（無効値ガード）"""
        result = _call_rule({"current_price": -1.0, "atr": 20.0})
        assert result is None

    def test_returns_none_when_atr_none(self):
        """atr=None → None"""
        result = _call_rule({"current_price": 1000.0, "atr": None})
        assert result is None

    def test_returns_none_when_atr_key_missing(self):
        """atr キー自体がない → None"""
        result = _call_rule({"current_price": 1000.0})
        assert result is None

    # ── 非発火 ──

    def test_returns_none_when_atr_ratio_below_threshold(self):
        """atr_ratio = 0.01 < 0.02 → 非発火"""
        # atr=10, current_price=1000 → atr_ratio=0.01
        result = _call_rule({"current_price": 1000.0, "atr": 10.0})
        assert result is None

    def test_returns_none_just_below_threshold(self):
        """atr_ratio = 0.0199... < 0.02 → 非発火"""
        result = _call_rule({"current_price": 1000.0, "atr": 19.99})
        assert result is None

    # ── 発火 ──

    def test_fires_at_threshold(self):
        """atr_ratio = 0.02 ちょうど → 発火（>=）"""
        # atr=20, current_price=1000 → atr_ratio=0.02
        result = _call_rule({"current_price": 1000.0, "atr": 20.0})
        assert result is not None
        assert result.state_code == "symbol_volatility_high"

    def test_fires_above_threshold(self):
        """atr_ratio = 0.03 > 0.02 → 発火"""
        result = _call_rule({"current_price": 1000.0, "atr": 30.0})
        assert result is not None
        assert result.state_code == "symbol_volatility_high"

    # ── score ──

    def test_score_at_threshold(self):
        """
        atr_ratio = 0.02 (threshold): score = min(1.0, 0.02/0.05) = 0.4
        """
        result = _call_rule({"current_price": 1000.0, "atr": 20.0})
        assert result is not None
        assert result.score == pytest.approx(0.4, abs=1e-5)

    def test_score_scales_with_atr_ratio(self):
        """
        atr_ratio = 0.035: score = min(1.0, 0.035/0.05) = 0.7
        """
        result = _call_rule({"current_price": 1000.0, "atr": 35.0})
        assert result is not None
        assert result.score == pytest.approx(0.7, abs=1e-5)

    def test_score_caps_at_1(self):
        """atr_ratio = 0.05: score = min(1.0, 0.05/0.05) = 1.0"""
        result = _call_rule({"current_price": 1000.0, "atr": 50.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    def test_score_does_not_exceed_1(self):
        """atr_ratio = 0.10 (2x基準): score は 1.0 に丸められる"""
        result = _call_rule({"current_price": 1000.0, "atr": 100.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    # ── evidence ──

    def test_evidence_fields(self):
        """evidence に current_price / atr / atr_ratio / threshold / rule が含まれる"""
        result = _call_rule({"current_price": 1000.0, "atr": 30.0})
        assert result is not None
        ev = result.evidence
        assert ev["current_price"] == 1000.0
        assert ev["atr"] == 30.0
        assert ev["threshold"] == ATR_RATIO_HIGH
        assert "atr_ratio" in ev
        assert "rule" in ev

    def test_evidence_atr_ratio_value(self):
        """evidence["atr_ratio"] = atr / current_price（6桁丸め）"""
        # atr=30, current_price=1000 → 0.030000
        result = _call_rule({"current_price": 1000.0, "atr": 30.0})
        assert result is not None
        assert result.evidence["atr_ratio"] == pytest.approx(0.03, abs=1e-6)

    def test_evidence_atr_ratio_is_rounded(self):
        """evidence["atr_ratio"] は round(..., 6) で丸められる"""
        # atr=1, current_price=3 → ratio = 0.333333... → round 6桁
        result = _call_rule({"current_price": 3.0, "atr": 1.0})
        # atr_ratio = 0.333333 >= 0.02 → 発火
        assert result is not None
        assert result.evidence["atr_ratio"] == round(1.0 / 3.0, 6)

    def test_result_metadata(self):
        """layer / target_type / target_code / confidence が正しい"""
        result = _call_rule({"current_price": 1000.0, "atr": 30.0})
        assert result is not None
        assert result.layer == "symbol"
        assert result.target_type == "symbol"
        assert result.target_code == "7203"
        assert result.confidence == pytest.approx(0.8, abs=1e-5)


# ─── B. orchestrator 経由テスト ───────────────────────────────────────────────

class TestOrchestratorVolatilityHigh:
    """SymbolStateEvaluator.evaluate() 経由で symbol_volatility_high が返ること"""

    def test_volatility_high_via_evaluate(self):
        """high ATR データを渡すと symbol_volatility_high が evaluate() から返る"""
        evaluator = SymbolStateEvaluator()
        # atr=30, current_price=1000 → atr_ratio=0.03 >= 0.02
        ctx = _ctx(current_price=1000.0, atr=30.0)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_volatility_high" in codes

    def test_low_atr_not_fired(self):
        """atr_ratio < threshold → symbol_volatility_high が返らない"""
        evaluator = SymbolStateEvaluator()
        # atr=10, current_price=1000 → atr_ratio=0.01 < 0.02
        ctx = _ctx(current_price=1000.0, atr=10.0)
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "symbol_volatility_high" for r in results)

    def test_none_current_price_not_fired(self):
        """current_price=None → symbol_volatility_high が返らない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=None, atr=30.0)
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "symbol_volatility_high" for r in results)

    def test_volatility_high_and_gap_up_coexist(self):
        """symbol_volatility_high と gap_up_open が同時に発火する"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=3060.0,
            atr=100.0,                              # atr_ratio ≈ 0.033 >= 0.02
            current_open=3060.0, prev_close=3000.0, # gap_up_open: 2%
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_volatility_high" in codes
        assert "gap_up_open" in codes

    def test_volatility_high_and_overextended_coexist(self):
        """symbol_volatility_high と overextended が同時に発火する（独立 rule）"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=1000.0,
            atr=30.0,   # atr_ratio=0.03 >= 0.02
            rsi=80.0,   # overbought
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_volatility_high" in codes
        assert "overextended" in codes

    def test_evidence_via_evaluate(self):
        """evaluate() 経由でも evidence フィールドが正しい"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=1000.0, atr=30.0)
        results = evaluator.evaluate(ctx)
        r = next(r for r in results if r.state_code == "symbol_volatility_high")
        assert r.evidence["current_price"] == 1000.0
        assert r.evidence["atr"] == 30.0
        assert r.evidence["atr_ratio"] == pytest.approx(0.03, abs=1e-6)
        assert r.evidence["threshold"] == ATR_RATIO_HIGH


# ─── C. 構造確認 ──────────────────────────────────────────────────────────────

class TestStructureVolatilityHigh:
    """リファクタリング後の構造を確認する"""

    def test_rule_is_module_level_function(self):
        """_rule_symbol_volatility_high が module レベルに存在する"""
        assert hasattr(_mod, "_rule_symbol_volatility_high")
        assert callable(_mod._rule_symbol_volatility_high)

    def test_evaluate_symbol_has_no_volatility_score_inline(self):
        """
        _evaluate_symbol() のソースに volatility_high 専用 score 式が残っていないこと。

        score = min(1.0, atr_ratio / 0.05) は symbol_volatility_high rule 専用の式。
        symbol_range の ATR 判定は atr_ratio / ATR_RATIO_HIGH を使うため混在しない。
        """
        source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert "atr_ratio / 0.05" not in source, \
            "_evaluate_symbol() 内に symbol_volatility_high の score 式が残っている"

    def test_evaluate_symbol_calls_rule(self):
        """_rule_symbol_volatility_high が evaluator module の _RULES に登録されている"""
        source = inspect.getsource(_mod)
        assert "_rule_symbol_volatility_high" in source, \
            "_rule_symbol_volatility_high が module に見当たらない"


# ─── D. 遷移テスト ────────────────────────────────────────────────────────────

# high / low ATR データ（他 rule が発火しないように最小構成）
_HIGH_ATR = {"current_price": 1000.0, "atr": 30.0}   # atr_ratio=0.03 >= 0.02
_LOW_ATR  = {"current_price": 1000.0, "atr": 10.0}   # atr_ratio=0.01 < 0.02


@pytest.mark.asyncio
class TestVolatilityHighTransitions:
    """symbol_volatility_high の activated / continued / deactivated 遷移テスト"""

    async def test_initial_activation_inserts_row(self, db_session: AsyncSession):
        """初回発火 → StateEvaluation が INSERT される（is_active=True）"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_ATR},
        )
        results = await engine.run(ctx)

        vh_results = [r for r in results if r.state_code == "symbol_volatility_high"]
        assert len(vh_results) == 1
        assert vh_results[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "symbol_volatility_high" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """
        run1: 発火 → 1 行 INSERT
        run2: 継続 → INSERT なし（合計 1 行）
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_ATR},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _HIGH_ATR},
        )

        await engine.run(ctx1)
        results2 = await engine.run(ctx2)

        vh2 = [r for r in results2
               if r.state_code == "symbol_volatility_high" and r.target_code == "7203"]
        assert len(vh2) == 1
        assert vh2[0].is_new_activation is False

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        vh_rows = [r for r in history if r.state_code == "symbol_volatility_high"]
        assert len(vh_rows) == 1, f"継続で INSERT が発生した。期待1行、実際{len(vh_rows)}行"

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """
        run1: 発火 → is_active=True
        run2: low ATR → is_active=False
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_ATR},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _LOW_ATR},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        vh_rows = [r for r in history if r.state_code == "symbol_volatility_high"]
        assert len(vh_rows) == 1
        assert vh_rows[0].is_active is False, \
            "解消後 symbol_volatility_high は is_active=False になるべき"

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """
        run1: 発火 → 行1 (is_active=True)
        run2: 解消 → 行1 (is_active=False)
        run3: 再発火 → 行2 (is_active=True)
        → DB に symbol_volatility_high が 2 行
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_ATR},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _LOW_ATR},
        )
        ctx3 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=20),
            symbol_data={"7203": _HIGH_ATR},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)
        results3 = await engine.run(ctx3)

        vh3 = [r for r in results3
               if r.state_code == "symbol_volatility_high" and r.target_code == "7203"]
        assert len(vh3) == 1
        assert vh3[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        vh_rows = [r for r in history if r.state_code == "symbol_volatility_high"]
        assert len(vh_rows) == 2, f"再発火で 2 行になるべき、実際 {len(vh_rows)} 行"
        active_rows = [r for r in vh_rows if r.is_active is True]
        assert len(active_rows) == 1
