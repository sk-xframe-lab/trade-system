"""
Phase E — overextended rule 化テスト

確認項目:
  A. _rule_overextended() 直テスト
     1. rsi=None → None（ガード）
     2. 中立 RSI（50） → None（非発火）
     3. rsi = RSI_OVERBOUGHT ちょうど → 発火 / direction="overbought"
     4. rsi > RSI_OVERBOUGHT → 発火
     5. rsi = RSI_OVERSOLD ちょうど → 発火 / direction="oversold"
     6. rsi < RSI_OVERSOLD → 発火
     7. overbought score 計算: min(1.0, (rsi - 75) / 15), 最小 0.3
     8. oversold score 計算: min(1.0, (25 - rsi) / 15), 最小 0.3
     9. score 最大値（rsi=90 → 1.0）
    10. evidence フィールド（rsi / direction / threshold / rule）

  B. orchestrator 経由テスト
    11. evaluate() 経由で overbought 検出
    12. evaluate() 経由で oversold 検出
    13. 中立 RSI では overextended が返らない
    14. overextended + gap_up_open が共存する
    15. rsi=None では overextended が返らない

  C. 構造確認
    16. _rule_overextended が module レベルに存在する
    17. _evaluate_symbol() にインライン RSI 判定が残っていない

  D. 遷移テスト（engine.run() 経由）
    18. 初回 overbought → INSERT
    19. overbought 継続 → INSERT なし
    20. overbought 解消 → soft-expire
    21. oversold → INSERT（overbought と独立）

設計:
  - _rule_overextended() は RSI のみに依存する独立 rule
  - score 最小値 0.3（バウンダリでも必ず発火）
  - 既存の wide_spread / price_stale テストへのリグレッションがないこと
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

RSI_OVERBOUGHT = SymbolStateEvaluator.RSI_OVERBOUGHT  # 75.0
RSI_OVERSOLD = SymbolStateEvaluator.RSI_OVERSOLD      # 25.0


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
    result, _diag = _mod._rule_overextended(
        "7203", data,
        rsi_overbought=RSI_OVERBOUGHT,
        rsi_oversold=RSI_OVERSOLD,
        make=_make,
    )
    return result


def _ctx(**fields) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=_EVAL_TIME,
        symbol_data={"7203": fields},
    )


# ─── A. _rule_overextended() 直テスト ─────────────────────────────────────────

class TestRuleOverextendedDirect:
    """_rule_overextended() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_returns_none_when_rsi_none(self):
        """rsi=None → None"""
        result = _call_rule({"rsi": None})
        assert result is None

    def test_returns_none_when_rsi_key_missing(self):
        """rsi キー自体がない → None"""
        result = _call_rule({})
        assert result is None

    # ── 非発火 ──

    def test_returns_none_when_rsi_neutral(self):
        """rsi=50 → 中立、非発火"""
        result = _call_rule({"rsi": 50.0})
        assert result is None

    def test_returns_none_just_below_overbought(self):
        """rsi = 74.99 → 発火しない（< 75）"""
        result = _call_rule({"rsi": 74.99})
        assert result is None

    def test_returns_none_just_above_oversold(self):
        """rsi = 25.01 → 発火しない（> 25）"""
        result = _call_rule({"rsi": 25.01})
        assert result is None

    # ── 発火: overbought ──

    def test_fires_at_overbought_threshold(self):
        """rsi = 75 ちょうど → overbought 発火（>=）"""
        result = _call_rule({"rsi": 75.0})
        assert result is not None
        assert result.state_code == "overextended"
        assert result.evidence["direction"] == "overbought"

    def test_fires_above_overbought(self):
        """rsi = 85 → overbought 発火"""
        result = _call_rule({"rsi": 85.0})
        assert result is not None
        assert result.state_code == "overextended"
        assert result.evidence["direction"] == "overbought"

    def test_overbought_threshold_in_evidence(self):
        """evidence["threshold"] == RSI_OVERBOUGHT"""
        result = _call_rule({"rsi": 80.0})
        assert result is not None
        assert result.evidence["threshold"] == RSI_OVERBOUGHT

    def test_overbought_score_at_threshold_is_min(self):
        """
        rsi = 75: score = min(1.0, (75 - 75) / 15) = 0.0 → max(0.3, 0.0) = 0.3
        """
        result = _call_rule({"rsi": 75.0})
        assert result is not None
        assert result.score == pytest.approx(0.3, abs=1e-5)

    def test_overbought_score_scales_with_rsi(self):
        """
        rsi = 82.5: score = min(1.0, (82.5 - 75) / 15) = min(1.0, 0.5) = 0.5
        """
        result = _call_rule({"rsi": 82.5})
        assert result is not None
        assert result.score == pytest.approx(0.5, abs=1e-5)

    def test_overbought_score_caps_at_1(self):
        """rsi = 90: score = min(1.0, (90 - 75) / 15) = 1.0"""
        result = _call_rule({"rsi": 90.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    # ── 発火: oversold ──

    def test_fires_at_oversold_threshold(self):
        """rsi = 25 ちょうど → oversold 発火（<=）"""
        result = _call_rule({"rsi": 25.0})
        assert result is not None
        assert result.state_code == "overextended"
        assert result.evidence["direction"] == "oversold"

    def test_fires_below_oversold(self):
        """rsi = 15 → oversold 発火"""
        result = _call_rule({"rsi": 15.0})
        assert result is not None
        assert result.state_code == "overextended"
        assert result.evidence["direction"] == "oversold"

    def test_oversold_threshold_in_evidence(self):
        """evidence["threshold"] == RSI_OVERSOLD"""
        result = _call_rule({"rsi": 20.0})
        assert result is not None
        assert result.evidence["threshold"] == RSI_OVERSOLD

    def test_oversold_score_at_threshold_is_min(self):
        """
        rsi = 25: score = min(1.0, (25 - 25) / 15) = 0.0 → max(0.3, 0.0) = 0.3
        """
        result = _call_rule({"rsi": 25.0})
        assert result is not None
        assert result.score == pytest.approx(0.3, abs=1e-5)

    def test_oversold_score_scales_with_rsi(self):
        """
        rsi = 17.5: score = min(1.0, (25 - 17.5) / 15) = min(1.0, 0.5) = 0.5
        """
        result = _call_rule({"rsi": 17.5})
        assert result is not None
        assert result.score == pytest.approx(0.5, abs=1e-5)

    def test_oversold_score_caps_at_1(self):
        """rsi = 10: score = min(1.0, (25 - 10) / 15) = 1.0"""
        result = _call_rule({"rsi": 10.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    # ── evidence ──

    def test_evidence_fields_overbought(self):
        """overbought の evidence: rsi / direction / threshold / rule"""
        result = _call_rule({"rsi": 80.0})
        assert result is not None
        ev = result.evidence
        assert ev["rsi"] == 80.0
        assert ev["direction"] == "overbought"
        assert ev["threshold"] == RSI_OVERBOUGHT
        assert "rule" in ev

    def test_evidence_fields_oversold(self):
        """oversold の evidence: rsi / direction / threshold / rule"""
        result = _call_rule({"rsi": 20.0})
        assert result is not None
        ev = result.evidence
        assert ev["rsi"] == 20.0
        assert ev["direction"] == "oversold"
        assert ev["threshold"] == RSI_OVERSOLD
        assert "rule" in ev

    def test_result_metadata(self):
        """layer / target_type / target_code が正しい"""
        result = _call_rule({"rsi": 80.0})
        assert result is not None
        assert result.layer == "symbol"
        assert result.target_type == "symbol"
        assert result.target_code == "7203"


# ─── B. orchestrator 経由テスト ───────────────────────────────────────────────

class TestOrchestratorOverextended:
    """SymbolStateEvaluator.evaluate() 経由で overextended が返ること"""

    def test_overbought_via_evaluate(self):
        """rsi=80 → evaluate() が overextended を返す"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(rsi=80.0)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "overextended" in codes

    def test_oversold_via_evaluate(self):
        """rsi=20 → evaluate() が overextended (oversold) を返す"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(rsi=20.0)
        results = evaluator.evaluate(ctx)
        r = next((r for r in results if r.state_code == "overextended"), None)
        assert r is not None
        assert r.evidence["direction"] == "oversold"

    def test_neutral_rsi_no_overextended(self):
        """rsi=50 → overextended が返らない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(rsi=50.0)
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "overextended" for r in results)

    def test_none_rsi_no_overextended(self):
        """rsi=None → overextended が返らない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(rsi=None)
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "overextended" for r in results)

    def test_overextended_and_gap_up_coexist(self):
        """overextended と gap_up_open が同時に発火する"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            rsi=80.0,
            current_open=3060.0, prev_close=3000.0,  # gap_up_open: 2% gap
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "overextended" in codes
        assert "gap_up_open" in codes

    def test_overextended_evidence_via_evaluate(self):
        """evaluate() 経由でも evidence フィールドが正しい"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(rsi=80.0)
        results = evaluator.evaluate(ctx)
        r = next(r for r in results if r.state_code == "overextended")
        assert r.evidence["rsi"] == 80.0
        assert r.evidence["direction"] == "overbought"


# ─── C. 構造確認 ──────────────────────────────────────────────────────────────

class TestStructureOverextended:
    """リファクタリング後の構造を確認する"""

    def test_rule_overextended_is_module_level_function(self):
        """_rule_overextended が module レベルに存在する"""
        assert hasattr(_mod, "_rule_overextended"), "_rule_overextended が module に存在しない"
        assert callable(_mod._rule_overextended)

    def test_evaluate_symbol_has_no_rsi_inline(self):
        """
        _evaluate_symbol() のソースコードにインライン RSI 判定が残っていないこと。

        overextended ロジックが rule 関数に移っていれば、
        _evaluate_symbol() 内に rsi >= ... / rsi <= ... の直接比較がないはず。
        RSI_OVERBOUGHT / RSI_OVERSOLD は _rule_overextended() への引数渡しとして
        存在するのは正しい。問題は "rsi >=" / "rsi <=" のインライン比較が残っているかどうか。
        """
        mod_source = inspect.getsource(_mod)
        ev_source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        # _rule_overextended が module (_RULES) に登録されている
        assert "rsi_overbought" in mod_source, "_rule_overextended の呼び出しが見当たらない"
        # インライン条件判定の痕跡がない
        assert "rsi >=" not in ev_source, "_evaluate_symbol() 内に rsi >= のインライン比較が残っている"
        assert "rsi <=" not in ev_source, "_evaluate_symbol() 内に rsi <= のインライン比較が残っている"

    def test_evaluate_symbol_has_no_rsi_extraction(self):
        """
        _evaluate_symbol() がトップで rsi を data.get() していないこと。
        rsi は _rule_overextended() 内で取得する。
        """
        source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert 'data.get("rsi")' not in source, "_evaluate_symbol() 内に rsi の直接抽出が残っている"


# ─── D. 遷移テスト ────────────────────────────────────────────────────────────

# overbought / neutral の最小データ（他の rule が発火しないように）
_OVERBOUGHT = {"rsi": 80.0}
_NEUTRAL = {"rsi": 50.0}
_OVERSOLD = {"rsi": 20.0}


@pytest.mark.asyncio
class TestOverextendedTransitions:
    """overextended の activated / continued / deactivated 遷移テスト"""

    async def test_initial_overbought_inserts_row(self, db_session: AsyncSession):
        """初回 overbought → StateEvaluation が INSERT される（is_active=True）"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _OVERBOUGHT},
        )
        results = await engine.run(ctx)

        oe_results = [r for r in results if r.state_code == "overextended"]
        assert len(oe_results) == 1
        assert oe_results[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "overextended" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """
        run1: overextended 発火 → 1 行 INSERT
        run2: overextended 継続 → INSERT なし（合計 1 行）
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _OVERBOUGHT},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _OVERBOUGHT},
        )

        await engine.run(ctx1)
        results2 = await engine.run(ctx2)

        oe2 = [r for r in results2 if r.state_code == "overextended" and r.target_code == "7203"]
        assert len(oe2) == 1
        assert oe2[0].is_new_activation is False

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        oe_rows = [r for r in history if r.state_code == "overextended"]
        assert len(oe_rows) == 1, f"継続で INSERT が発生した。期待1行、実際{len(oe_rows)}行"

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """
        run1: overextended 発火 → is_active=True
        run2: 中立 RSI → is_active=False
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _OVERBOUGHT},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _NEUTRAL},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        oe_rows = [r for r in history if r.state_code == "overextended"]
        assert len(oe_rows) == 1
        assert oe_rows[0].is_active is False, "解消後 overextended は is_active=False になるべき"

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """
        run1: overbought → 行1 (is_active=True)
        run2: 中立 → 行1 (is_active=False)
        run3: overbought → 行2 (is_active=True)
        → DB に overextended が 2 行
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _OVERBOUGHT},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _NEUTRAL},
        )
        ctx3 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=20),
            symbol_data={"7203": _OVERBOUGHT},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)
        results3 = await engine.run(ctx3)

        oe3 = [r for r in results3 if r.state_code == "overextended" and r.target_code == "7203"]
        assert len(oe3) == 1
        assert oe3[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        oe_rows = [r for r in history if r.state_code == "overextended"]
        assert len(oe_rows) == 2, f"再発火で 2 行になるべき、実際 {len(oe_rows)} 行"
        active_rows = [r for r in oe_rows if r.is_active is True]
        assert len(active_rows) == 1
