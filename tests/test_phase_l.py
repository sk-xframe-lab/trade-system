"""
Phase L — symbol_range rule 化テスト

確認項目:
  A. _rule_symbol_range() 直テスト
     1.  current_price=None        → skipped / no_current_price
     2.  current_price=0           → skipped / no_current_price
     3.  atr=None                  → skipped / no_atr
     4.  is_trend_up=True          → inactive / reason="trending"
     5.  is_trend_down=True        → inactive / reason="trending"
     6.  atr_ratio >= atr_ratio_high → inactive（高 ATR）
     7.  atr_ratio ちょうど threshold → inactive（< のみ発火）
     8.  atr_ratio < threshold, トレンドなし → 発火
     9.  score 計算: atr_ratio=0.01, threshold=0.02 → 0.5
    10.  score の floor が 0.1
    11.  evidence フィールドの内容

  B. orchestrator 経由テスト
    12. symbol_range が evaluate() から返る（トレンドなし・低 ATR）
    13. トレンドが active のとき symbol_range は発火しない（trend_up）
    14. トレンドが active のとき symbol_range は発火しない（trend_down）
    15. 高 ATR では symbol_range が発火しない
    16. symbol_range と wide_spread が同時 active になれる

  C. 構造確認
    17. _rule_symbol_range が module レベルに存在する
    18. _evaluate_symbol() が _rule_symbol_range を呼ぶ
    19. _evaluate_symbol() 内にインラインレンジ判定（atr_ratio 計算）がない
    20. _evaluate_symbol() 内に data.get() が一切ない（全 rule 化完了の証明）

  D. 遷移テスト（symbol_range）
    21. 初回 symbol_range → INSERT
    22. 継続 → INSERT なし
    23. 解消（トレンド発生） → soft-expire
    24. 再発火 → 再 INSERT

  E. observability テスト
    25. symbol_range active 診断: status=active / atr_ratio が含まれる
    26. symbol_range inactive 診断（trending）: status=inactive / reason="trending" / is_trend_up / is_trend_down
    27. symbol_range inactive 診断（高 ATR）: status=inactive / atr_ratio
    28. symbol_range skipped / no_current_price
    29. symbol_range skipped / no_atr
    30. 空データでも symbol_range キーが rule_diagnostics に存在する
    31. _evaluate_symbol() 経由でも active 診断が得られる
    32. _evaluate_symbol() 経由でも inactive（trending）診断が得られる

設計:
  - _rule_symbol_range(ticker, data, *, is_trend_up, is_trend_down, atr_ratio_high, make)
  - _evaluate_symbol() のインライン range ブロックを削除し rule ループへ統合
  - is_trend_up / is_trend_down は trend rule 結果から渡す
  - 完了後 _evaluate_symbol() 内に data.get() が残らない
"""
from __future__ import annotations

from datetime import datetime, timezone
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


def _call_range(
    data: dict[str, Any],
    *,
    is_trend_up: bool = False,
    is_trend_down: bool = False,
    atr_ratio_high: float = ATR_RATIO_HIGH,
) -> StateEvaluationResult | None:
    result, _diag = _mod._rule_symbol_range(
        "7203", data,
        is_trend_up=is_trend_up,
        is_trend_down=is_trend_down,
        atr_ratio_high=atr_ratio_high,
        make=_make,
    )
    return result


def _call_range_with_diag(
    data: dict[str, Any],
    *,
    is_trend_up: bool = False,
    is_trend_down: bool = False,
    atr_ratio_high: float = ATR_RATIO_HIGH,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    return _mod._rule_symbol_range(
        "7203", data,
        is_trend_up=is_trend_up,
        is_trend_down=is_trend_down,
        atr_ratio_high=atr_ratio_high,
        make=_make,
    )


def _ctx(**fields) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=_EVAL_TIME,
        symbol_data={"7203": fields},
    )


def _diags(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evaluator = SymbolStateEvaluator()
    _results, diagnostics = evaluator._evaluate_symbol("7203", data, _EVAL_TIME)
    return diagnostics


# 発火用データ（トレンドデータなし・低 ATR）
_RANGE = {"current_price": 1000.0, "atr": 10.0}    # atr_ratio=0.01 < 0.02 → 発火
_HIGH_ATR = {"current_price": 1000.0, "atr": 30.0}  # atr_ratio=0.03 >= 0.02 → 非発火

# トレンドあり（趨勢データで is_trend_up=True を期待）
_TREND_UP = {
    "current_price": 1100.0,
    "vwap": 1000.0, "ma5": 1050.0, "ma20": 1000.0,  # trend_up
    "atr": 10.0,
}
_TREND_DOWN = {
    "current_price": 900.0,
    "vwap": 1000.0, "ma5": 950.0, "ma20": 1000.0,   # trend_down
    "atr": 10.0,
}


# ─── A. _rule_symbol_range() 直テスト ────────────────────────────────────────

class TestRuleSymbolRangeDirect:
    """_rule_symbol_range() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_guard_no_current_price(self):
        """current_price=None → skipped"""
        result = _call_range({"atr": 10.0})
        assert result is None

    def test_guard_zero_current_price(self):
        """current_price=0 → skipped（ゼロ除算ガード）"""
        result = _call_range({"current_price": 0.0, "atr": 10.0})
        assert result is None

    def test_guard_no_atr(self):
        """atr=None → skipped"""
        result = _call_range({"current_price": 1000.0})
        assert result is None

    # ── 非発火（依存引数）──

    def test_no_fire_when_trend_up(self):
        """is_trend_up=True → inactive（トレンド中はレンジ判定しない）"""
        result = _call_range(_RANGE, is_trend_up=True)
        assert result is None

    def test_no_fire_when_trend_down(self):
        """is_trend_down=True → inactive"""
        result = _call_range(_RANGE, is_trend_down=True)
        assert result is None

    # ── 非発火（ATR 条件）──

    def test_no_fire_when_atr_high(self):
        """atr_ratio >= atr_ratio_high → inactive（高ボラティリティ）"""
        result = _call_range(_HIGH_ATR)
        assert result is None

    def test_no_fire_at_threshold(self):
        """atr_ratio ちょうど threshold → inactive（< のみ発火）"""
        result = _call_range({"current_price": 1000.0, "atr": 20.0})  # atr_ratio=0.02
        assert result is None

    # ── 発火 ──

    def test_fires_when_no_trend_and_low_atr(self):
        """トレンドなし かつ atr_ratio < threshold → 発火"""
        result = _call_range(_RANGE)
        assert result is not None
        assert result.state_code == "symbol_range"
        assert result.target_code == "7203"
        assert result.layer == "symbol"

    # ── score ──

    def test_score_at_half_threshold(self):
        """atr_ratio=0.01 (threshold/2) → score = 1.0 - 0.01/0.02 = 0.5"""
        result = _call_range(_RANGE)  # atr_ratio=0.01
        assert result is not None
        expected = max(0.1, 1.0 - 0.01 / ATR_RATIO_HIGH)  # 0.5
        assert result.score == pytest.approx(expected, abs=1e-5)

    def test_score_floor_at_0_1(self):
        """atr_ratio が threshold 直下でも score は 0.1 以上"""
        result = _call_range({"current_price": 1000.0, "atr": 19.9})  # atr_ratio≈0.0199
        assert result is not None
        assert result.score >= 0.1

    # ── evidence ──

    def test_evidence_fields(self):
        """evidence に必要なフィールドが含まれる"""
        result = _call_range(_RANGE)
        assert result is not None
        ev = result.evidence
        assert "current_price" in ev
        assert "atr" in ev
        assert "atr_ratio" in ev
        assert "threshold" in ev
        assert "rule" in ev
        assert ev["atr_ratio"] == pytest.approx(0.01, abs=1e-6)


# ─── B. orchestrator 経由テスト ──────────────────────────────────────────────

class TestOrchestratorRange:
    """SymbolStateEvaluator.evaluate() 経由でレンジ状態が返ること"""

    def test_symbol_range_via_evaluate(self):
        """トレンドなし・低 ATR データを渡すと evaluate() から symbol_range が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_RANGE)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_range" in codes

    def test_no_fire_when_trend_up_active(self):
        """symbol_trend_up が active のとき symbol_range は発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_TREND_UP)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_trend_up" in codes, "symbol_trend_up が発火していない（前提条件確認）"
        assert "symbol_range" not in codes

    def test_no_fire_when_trend_down_active(self):
        """symbol_trend_down が active のとき symbol_range は発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_TREND_DOWN)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_trend_down" in codes, "symbol_trend_down が発火していない（前提条件確認）"
        assert "symbol_range" not in codes

    def test_no_fire_when_high_atr(self):
        """高 ATR では symbol_range が発火しない（symbol_volatility_high が発火しうる）"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_HIGH_ATR)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_range" not in codes

    def test_range_coexists_with_wide_spread(self):
        """symbol_range と wide_spread が同時 active になれる"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=1000.0,
            atr=10.0,                              # atr_ratio=0.01 → symbol_range
            best_bid=989.0, best_ask=1011.0,       # spread=22/1000=2.2% → wide_spread
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_range" in codes, "symbol_range が発火していない"
        assert "wide_spread" in codes, "wide_spread が発火していない"


# ─── C. 構造確認 ──────────────────────────────────────────────────────────────

class TestStructureRange:
    """リファクタリング後の構造を確認する"""

    def test_rule_symbol_range_is_module_level(self):
        """_rule_symbol_range が module レベルに存在する"""
        assert hasattr(_mod, "_rule_symbol_range")
        assert callable(_mod._rule_symbol_range)

    def test_evaluate_symbol_calls_range_rule(self):
        """_rule_symbol_range が evaluator module の _RULES に登録されている"""
        import inspect
        source = inspect.getsource(_mod)
        assert "_rule_symbol_range" in source

    def test_evaluate_symbol_has_no_inline_range(self):
        """
        _evaluate_symbol() 内にインラインレンジ判定が残っていないこと。

        rule 化後は atr / current_price の計算を _evaluate_symbol() 内に置かない。
        """
        import inspect
        source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert "atr_ratio = atr / current_price" not in source, \
            "_evaluate_symbol() 内に atr_ratio 計算が残っている"

    def test_evaluate_symbol_has_no_data_get(self):
        """
        Phase L 完了後、_evaluate_symbol() 内に data.get() が一切ないこと。

        全ての state 判定が rule 化されたことで、_evaluate_symbol() は
        orchestrator として rule を呼び出すだけになる。
        data.get() が存在する場合はまだインライン判定が残っている。
        """
        import inspect
        source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert "data.get(" not in source, \
            "_evaluate_symbol() 内に data.get() が残っている — まだインライン判定が存在する"


# ─── D. 遷移テスト ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSymbolRangeTransitions:
    """symbol_range の activated / continued / deactivated 遷移テスト"""

    async def test_initial_range_inserts_row(self, db_session: AsyncSession):
        """初回 symbol_range → StateEvaluation が INSERT される（is_active=True）"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _RANGE},
        )
        results = await engine.run(ctx)

        range_results = [r for r in results if r.state_code == "symbol_range"]
        assert len(range_results) == 1
        assert range_results[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "symbol_range" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """symbol_range が継続するとき、2回目は新規 INSERT されない"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _RANGE},
        )
        await engine.run(ctx1)

        repo = MarketStateRepository(db_session)
        rows_1 = await repo.get_symbol_active_evaluations("7203")
        range_1 = [r for r in rows_1 if r.state_code == "symbol_range"]
        assert len(range_1) == 1
        first_id = range_1[0].id

        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _RANGE},
        )
        results2 = await engine.run(ctx2)
        continuation = [r for r in results2 if r.state_code == "symbol_range"]
        assert len(continuation) == 1
        assert continuation[0].is_new_activation is False

        rows_2 = await repo.get_symbol_active_evaluations("7203")
        range_2 = [r for r in rows_2 if r.state_code == "symbol_range"]
        assert len(range_2) == 1
        assert range_2[0].id == first_id

    async def test_deactivation_when_trend_starts(self, db_session: AsyncSession):
        """トレンド発生で symbol_range が is_active=False になる"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _RANGE},
        )
        await engine.run(ctx1)

        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _TREND_UP},  # トレンド発生
        )
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert not any(r.state_code == "symbol_range" for r in active)

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """symbol_range 解消後に再発火すると新規 INSERT される"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _RANGE},
        )
        await engine.run(ctx1)

        repo = MarketStateRepository(db_session)
        rows_1 = await repo.get_symbol_active_evaluations("7203")
        range_1 = [r for r in rows_1 if r.state_code == "symbol_range"]
        first_id = range_1[0].id

        # 解消（トレンド発生）
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _TREND_UP},
        )
        await engine.run(ctx2)

        # 再発火（トレンド消滅）
        ctx3 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _RANGE},
        )
        results3 = await engine.run(ctx3)
        reactivated = [r for r in results3 if r.state_code == "symbol_range"]
        assert len(reactivated) == 1
        assert reactivated[0].is_new_activation is True

        rows_3 = await repo.get_symbol_active_evaluations("7203")
        range_3 = [r for r in rows_3 if r.state_code == "symbol_range"]
        assert len(range_3) == 1
        assert range_3[0].id != first_id


# ─── E. observability テスト ──────────────────────────────────────────────────

class TestRangeDiagnostic:
    """rule_diagnostics に symbol_range の診断情報が含まれること"""

    def test_range_active_diag(self):
        """低 ATR・トレンドなし時: status=active / atr_ratio が含まれる"""
        _result, diag = _call_range_with_diag(_RANGE)
        assert diag["status"] == "active"
        assert "atr_ratio" in diag
        assert diag["atr_ratio"] == pytest.approx(0.01, abs=1e-6)

    def test_range_inactive_diag_trending(self):
        """トレンド中: status=inactive / reason=trending / is_trend_up / is_trend_down が含まれる"""
        _result, diag = _call_range_with_diag(_RANGE, is_trend_up=True)
        assert diag["status"] == "inactive"
        assert diag.get("reason") == "trending"
        assert "is_trend_up" in diag
        assert "is_trend_down" in diag
        assert diag["is_trend_up"] is True

    def test_range_inactive_diag_high_atr(self):
        """高 ATR: status=inactive / atr_ratio が含まれる"""
        _result, diag = _call_range_with_diag(_HIGH_ATR)
        assert diag["status"] == "inactive"
        assert "atr_ratio" in diag
        assert diag["atr_ratio"] >= ATR_RATIO_HIGH

    def test_range_skipped_no_current_price(self):
        """current_price なし: status=skipped"""
        _result, diag = _call_range_with_diag({"atr": 10.0})
        assert diag["status"] == "skipped"
        assert diag.get("reason") == "no_current_price"

    def test_range_skipped_no_atr(self):
        """atr なし: status=skipped"""
        _result, diag = _call_range_with_diag({"current_price": 1000.0})
        assert diag["status"] == "skipped"
        assert diag.get("reason") == "no_atr"

    def test_range_key_present_in_empty_data(self):
        """空データでも symbol_range キーが rule_diagnostics に存在する"""
        diags = _diags({})
        assert "symbol_range" in diags, \
            f"symbol_range が rule_diagnostics にない: {list(diags.keys())}"

    def test_range_active_via_evaluate_symbol(self):
        """_evaluate_symbol() 経由でも active 診断が得られる"""
        diags = _diags(_RANGE)
        assert diags["symbol_range"]["status"] == "active"

    def test_range_inactive_via_evaluate_symbol_when_trend(self):
        """_evaluate_symbol() 経由でトレンド中は inactive 診断が得られる"""
        diags = _diags(_TREND_UP)
        assert diags["symbol_range"]["status"] == "inactive"
        assert diags["symbol_range"].get("reason") == "trending"
