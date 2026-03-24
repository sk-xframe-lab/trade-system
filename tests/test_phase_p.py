"""
Phase P — quote_only rule 追加テスト

確認項目:
  A. _rule_quote_only() 直テスト
     1. current_price=None, bid あり, ask なし → active
     2. current_price=None, bid なし, ask あり → active
     3. current_price=None, bid/ask 両方あり → active
     4. current_price あり, bid/ask あり → inactive / has_last_price
     5. current_price あり, bid/ask なし → inactive / has_last_price
     6. current_price=None, bid/ask 両方なし → inactive / no_quotes
     7. score = 1.0 固定
     8. evidence に必須キーが含まれる
     9. active 時の evidence reason = "quote_only"
    10. active diag の status="active", reason="quote_only"
    11. inactive diag の status="inactive"

  B. orchestrator テスト（evaluate() 経由）
    12. evaluate() 経由で quote_only が返る
    13. current_price あり → quote_only が返らない
    14. bid/ask 両方なし → quote_only が返らない
    15. quote_only と wide_spread の共存（bid/ask 両方あり＋価格なし）

  C. 構造テスト
    16. _rule_quote_only が module レベルに存在する
    17. quote_only が _RULE_REGISTRY に含まれる
    18. quote_only が _RULES に含まれる

  D. 遷移テスト（engine.run() 経由）
    19. 初回 active → StateEvaluation INSERT (is_active=True)
    20. 継続 active → INSERT なし（is_new_activation=False）
    21. 非 active 化 → is_active=False
    22. 再 active 化 → 再 INSERT（DB に 2 行）

  E. observability テスト（rule_diagnostics 確認）
    23. active 時 diagnostics: status=active, reason=quote_only
    24. inactive / has_last_price diagnostics: status=inactive, reason=has_last_price
    25. inactive / no_quotes diagnostics: status=inactive, reason=no_quotes

  F. notification テスト
    26. quote_only は NOTIFIABLE_STATE_CODES に含まれない
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.services.market_state import symbol_evaluator as _mod
from trade_app.services.market_state.engine import MarketStateEngine, NOTIFIABLE_STATE_CODES
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult
from trade_app.services.market_state.symbol_evaluator import (
    SymbolStateEvaluator,
    _RULES,
    _RULE_REGISTRY,
)

_UTC = timezone.utc
_EVAL_TIME = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)


# ─── テスト用ヘルパー ─────────────────────────────────────────────────────────

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


def _call_rule(data: dict[str, Any]):
    """_rule_quote_only() を呼び出し (result, diag) を返す。"""
    return _mod._rule_quote_only("7203", data, make=_make)


def _ctx(**fields) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=_EVAL_TIME,
        symbol_data={"7203": fields},
    )


def _diags(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evaluator = SymbolStateEvaluator()
    _results, diagnostics = evaluator._evaluate_symbol("7203", data, _EVAL_TIME)
    return diagnostics


# ─── A. _rule_quote_only() 直テスト ─────────────────────────────────────────

class TestRuleQuoteOnlyDirect:

    # ── active ──

    def test_active_bid_only(self):
        """current_price=None, bid あり, ask なし → active"""
        result, diag = _call_rule({"current_price": None, "best_bid": 990.0, "best_ask": None})
        assert result is not None
        assert result.state_code == "quote_only"
        assert diag["status"] == "active"

    def test_active_ask_only(self):
        """current_price=None, bid なし, ask あり → active"""
        result, diag = _call_rule({"current_price": None, "best_bid": None, "best_ask": 1010.0})
        assert result is not None
        assert result.state_code == "quote_only"
        assert diag["status"] == "active"

    def test_active_bid_and_ask(self):
        """current_price=None, bid/ask 両方あり → active"""
        result, diag = _call_rule({"current_price": None, "best_bid": 990.0, "best_ask": 1010.0})
        assert result is not None
        assert result.state_code == "quote_only"
        assert diag["status"] == "active"

    # ── inactive ──

    def test_inactive_has_current_price(self):
        """current_price あり, bid/ask あり → inactive / has_last_price"""
        result, diag = _call_rule({"current_price": 1000.0, "best_bid": 990.0, "best_ask": 1010.0})
        assert result is None
        assert diag["status"] == "inactive"
        assert diag["reason"] == "has_last_price"

    def test_inactive_has_current_price_no_quotes(self):
        """current_price あり, bid/ask なし → inactive / has_last_price（価格チェックが優先）"""
        result, diag = _call_rule({"current_price": 1000.0, "best_bid": None, "best_ask": None})
        assert result is None
        assert diag["status"] == "inactive"
        assert diag["reason"] == "has_last_price"

    def test_inactive_no_quotes(self):
        """current_price=None, bid/ask 両方なし → inactive / no_quotes"""
        result, diag = _call_rule({"current_price": None, "best_bid": None, "best_ask": None})
        assert result is None
        assert diag["status"] == "inactive"
        assert diag["reason"] == "no_quotes"

    def test_inactive_keys_missing(self):
        """current_price キーなし, bid/ask キーなし → inactive / no_quotes"""
        result, diag = _call_rule({})
        assert result is None
        assert diag["status"] == "inactive"
        assert diag["reason"] == "no_quotes"

    # ── score / evidence ──

    def test_score_is_1_0(self):
        """score は 1.0 固定"""
        result, _ = _call_rule({"current_price": None, "best_bid": 990.0, "best_ask": None})
        assert result is not None
        assert result.score == pytest.approx(1.0)

    def test_evidence_required_keys(self):
        """active 時 evidence に reason / current_price / best_bid / best_ask が含まれる"""
        result, _ = _call_rule({"current_price": None, "best_bid": 990.0, "best_ask": 1010.0})
        assert result is not None
        ev = result.evidence
        assert ev["reason"] == "quote_only"
        assert ev["current_price"] is None
        assert ev["best_bid"] == pytest.approx(990.0)
        assert ev["best_ask"] == pytest.approx(1010.0)

    def test_evidence_has_bid_has_ask_flags(self):
        """active 時 evidence に has_bid / has_ask フラグが含まれる"""
        result, _ = _call_rule({"current_price": None, "best_bid": 990.0, "best_ask": None})
        assert result is not None
        assert result.evidence["has_bid"] is True
        assert result.evidence["has_ask"] is False

    def test_active_diag_reason(self):
        """active diag に reason=quote_only が含まれる"""
        _, diag = _call_rule({"current_price": None, "best_bid": 990.0, "best_ask": None})
        assert diag["status"] == "active"
        assert diag["reason"] == "quote_only"

    def test_target_code_and_layer(self):
        """result.target_code = ticker, layer = symbol"""
        result, _ = _call_rule({"current_price": None, "best_bid": 990.0, "best_ask": None})
        assert result is not None
        assert result.target_code == "7203"
        assert result.layer == "symbol"


# ─── B. orchestrator テスト ───────────────────────────────────────────────────

class TestOrchestratorQuoteOnly:

    def test_quote_only_via_evaluate(self):
        """evaluate() 経由で quote_only が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=None, best_bid=990.0, best_ask=None)
        results = evaluator.evaluate(ctx)
        assert any(r.state_code == "quote_only" for r in results)

    def test_no_quote_only_when_has_price(self):
        """current_price あり → quote_only が返らない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=1000.0, best_bid=990.0, best_ask=1010.0)
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "quote_only" for r in results)

    def test_no_quote_only_when_no_quotes(self):
        """current_price=None, bid/ask 両方なし → quote_only が返らない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(current_price=None, best_bid=None, best_ask=None)
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "quote_only" for r in results)

    def test_coexists_with_other_rules(self):
        """quote_only と他 rule の評価が共存できる（他ルールのエラーで止まらない）"""
        evaluator = SymbolStateEvaluator()
        # current_price=None, bid/ask あり → quote_only 発火
        # rsi=80 → overextended 発火（quote_only との共存を確認）
        ctx = _ctx(
            current_price=None,
            best_bid=990.0,
            best_ask=1010.0,
            rsi=80.0,
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "quote_only" in codes
        assert "overextended" in codes


# ─── C. 構造テスト ────────────────────────────────────────────────────────────

class TestStructureQuoteOnly:

    def test_rule_function_exists_at_module_level(self):
        """_rule_quote_only が module レベルに存在する"""
        assert hasattr(_mod, "_rule_quote_only"), "_rule_quote_only が module にない"
        assert callable(_mod._rule_quote_only)

    def test_quote_only_in_rule_registry(self):
        """quote_only が _RULE_REGISTRY に含まれる"""
        assert "quote_only" in _RULE_REGISTRY, f"_RULE_REGISTRY: {_RULE_REGISTRY}"

    def test_quote_only_in_rules_list(self):
        """quote_only が _RULES リストに含まれる"""
        codes = [code for code, _ in _RULES]
        assert "quote_only" in codes, f"_RULES codes: {codes}"

    def test_rules_count_is_14(self):
        """_RULES に 14 エントリある"""
        assert len(_RULES) == 14, f"expected 14, got {len(_RULES)}"


# ─── D. 遷移テスト ───────────────────────────────────────────────────────────

# quote_only 発火データ（current_price なし、bid あり）
_ACTIVE = {"current_price": None, "best_bid": 990.0}
# quote_only 非発火データ（current_price あり）
_INACTIVE = {"current_price": 1000.0}


@pytest.mark.asyncio
class TestQuoteOnlyTransitions:

    async def test_initial_activation_inserts_row(self, db_session: AsyncSession):
        """初回 active → StateEvaluation が INSERT される"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _ACTIVE},
        )
        results = await engine.run(ctx)

        qo = [r for r in results if r.state_code == "quote_only"]
        assert len(qo) == 1
        assert qo[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "quote_only" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """run1: active → INSERT / run2: 継続 → INSERT なし（合計 1 行）"""
        engine = MarketStateEngine(db_session)
        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _ACTIVE},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _ACTIVE},
        )
        await engine.run(ctx1)
        results2 = await engine.run(ctx2)

        qo2 = [r for r in results2 if r.state_code == "quote_only" and r.target_code == "7203"]
        assert len(qo2) == 1
        assert qo2[0].is_new_activation is False

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        qo_rows = [r for r in history if r.state_code == "quote_only"]
        assert len(qo_rows) == 1, f"継続で INSERT が発生。期待1行、実際{len(qo_rows)}行"

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """run1: active → is_active=True / run2: current_price あり → is_active=False"""
        engine = MarketStateEngine(db_session)
        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _ACTIVE},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _INACTIVE},
        )
        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        qo_rows = [r for r in history if r.state_code == "quote_only"]
        assert len(qo_rows) == 1
        assert qo_rows[0].is_active is False

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """run1: active / run2: inactive / run3: active → DB に 2 行"""
        engine = MarketStateEngine(db_session)
        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _ACTIVE},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _INACTIVE},
        )
        ctx3 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=20),
            symbol_data={"7203": _ACTIVE},
        )
        await engine.run(ctx1)
        await engine.run(ctx2)
        results3 = await engine.run(ctx3)

        qo3 = [r for r in results3 if r.state_code == "quote_only" and r.target_code == "7203"]
        assert len(qo3) == 1
        assert qo3[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        qo_rows = [r for r in history if r.state_code == "quote_only"]
        assert len(qo_rows) == 2, f"再発火で 2 行になるべき、実際 {len(qo_rows)} 行"
        assert sum(1 for r in qo_rows if r.is_active) == 1


# ─── E. observability テスト ─────────────────────────────────────────────────

class TestQuoteOnlyDiagnostic:

    def test_active_diagnostic(self):
        """active 時 diagnostics: status=active, reason=quote_only"""
        diags = _diags({"current_price": None, "best_bid": 990.0, "best_ask": None})
        d = diags["quote_only"]
        assert d["status"] == "active"
        assert d["reason"] == "quote_only"

    def test_inactive_has_last_price_diagnostic(self):
        """current_price あり → status=inactive, reason=has_last_price"""
        diags = _diags({"current_price": 1000.0, "best_bid": 990.0, "best_ask": 1010.0})
        d = diags["quote_only"]
        assert d["status"] == "inactive"
        assert d["reason"] == "has_last_price"

    def test_inactive_no_quotes_diagnostic(self):
        """current_price=None, bid/ask なし → status=inactive, reason=no_quotes"""
        diags = _diags({"current_price": None, "best_bid": None, "best_ask": None})
        d = diags["quote_only"]
        assert d["status"] == "inactive"
        assert d["reason"] == "no_quotes"

    def test_quote_only_key_always_in_diagnostics(self):
        """symbol_data に何もなくても quote_only キーが rule_diagnostics に存在する"""
        diags = _diags({})
        assert "quote_only" in diags


# ─── F. notification テスト ───────────────────────────────────────────────────

class TestQuoteOnlyNotification:

    def test_not_in_notifiable_state_codes(self):
        """quote_only は NOTIFIABLE_STATE_CODES に含まれない"""
        assert "quote_only" not in NOTIFIABLE_STATE_CODES
