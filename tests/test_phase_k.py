"""
Phase K — symbol_trend_up / symbol_trend_down rule 化テスト

確認項目:
  A. _rule_symbol_trend_up() 直テスト
     1.  current_price=None  → skipped / no_current_price
     2.  vwap=None           → skipped / no_vwap
     3.  ma5=None            → skipped / no_ma5
     4.  ma20=None           → skipped / no_ma20
     5.  vwap=0              → skipped / zero_vwap
     6.  ma20=0              → skipped / zero_ma20
     7.  price < vwap, ma5 > ma20 → inactive（片方のみ）
     8.  price > vwap, ma5 < ma20 → inactive（片方のみ）
     9.  price < vwap, ma5 < ma20 → inactive（trend_up は発火しない）
    10.  price > vwap AND ma5 > ma20 → 発火
    11.  score の floor が 0.3
    12.  score 計算: (vwap_diff + ma_diff) * 20
    13.  score の cap が 1.0
    14.  evidence フィールドの内容（price_above_vwap=True, ma5_above_ma20=True）

  B. _rule_symbol_trend_down() 直テスト
    15.  current_price=None → skipped
    16.  vwap=None          → skipped
    17.  price > vwap → inactive（price の向きが逆）
    18.  ma5 > ma20 → inactive（ma の向きが逆）
    19.  price > vwap AND ma5 < ma20 → inactive（混合）
    20.  price < vwap AND ma5 < ma20 → 発火
    21.  score 計算: (vwap_diff + ma_diff) * 20
    22.  score の cap が 1.0
    23.  evidence フィールドの内容（price_above_vwap=False, ma5_above_ma20=False）

  C. orchestrator 経由テスト
    24. symbol_trend_up が evaluate() から返る
    25. symbol_trend_down が evaluate() から返る
    26. 混合状態ではどちらも発火しない
    27. trend_up と trend_down は同時 active にならない
    28. trend_up と high_relative_volume が同時 active になれる

  D. 構造確認
    29. _rule_symbol_trend_up が module レベルに存在する
    30. _rule_symbol_trend_down が module レベルに存在する
    31. _evaluate_symbol() が両 rule を呼ぶ
    32. _evaluate_symbol() 内に inline vwap / ma5 / ma20 の data.get() がない

  E. 遷移テスト（symbol_trend_up）
    33. 初回 trend_up → INSERT
    34. 継続 → INSERT なし
    35. 解消 → soft-expire
    36. 再発火 → 再 INSERT

  F. observability テスト
    37. trend_up active 診断: status=active / vwap_diff / ma_diff
    38. trend_down active 診断: status=active
    39. trend_up inactive 診断: status=inactive / price_above_vwap / ma5_above_ma20
    40. trend_down inactive 診断: status=inactive
    41. trend_up skipped / no_vwap
    42. trend_down skipped / no_ma5
    43. 空データでも両キーが rule_diagnostics に存在する
    44. _evaluate_symbol() 経由で trend_up active 診断が得られる
    45. _evaluate_symbol() 経由で trend_down active 診断が得られる

設計:
  - _rule_symbol_trend_up(ticker, data, *, make)
  - _rule_symbol_trend_down(ticker, data, *, make)
  - _evaluate_symbol() でトレンド判定後、rule_diagnostics に格納
  - is_trend_up / is_trend_down は rule 呼び出し結果から導出（result is not None）
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


def _call_trend_up(data: dict[str, Any]) -> StateEvaluationResult | None:
    result, _diag = _mod._rule_symbol_trend_up("7203", data, make=_make)
    return result


def _call_trend_up_with_diag(
    data: dict[str, Any],
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    return _mod._rule_symbol_trend_up("7203", data, make=_make)


def _call_trend_down(data: dict[str, Any]) -> StateEvaluationResult | None:
    result, _diag = _mod._rule_symbol_trend_down("7203", data, make=_make)
    return result


def _call_trend_down_with_diag(
    data: dict[str, Any],
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    return _mod._rule_symbol_trend_down("7203", data, make=_make)


def _ctx(**fields) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=_EVAL_TIME,
        symbol_data={"7203": fields},
    )


def _diags(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evaluator = SymbolStateEvaluator()
    _results, diagnostics = evaluator._evaluate_symbol("7203", data, _EVAL_TIME)
    return diagnostics


# 発火用データ
_TREND_UP = {
    "current_price": 1100.0,
    "vwap": 1000.0,   # price(1100) > vwap(1000)
    "ma5": 1050.0,
    "ma20": 1000.0,   # ma5(1050) > ma20(1000)
}
_TREND_DOWN = {
    "current_price": 900.0,
    "vwap": 1000.0,   # price(900) < vwap(1000)
    "ma5": 950.0,
    "ma20": 1000.0,   # ma5(950) < ma20(1000)
}
# 混合状態: price > vwap だが ma5 < ma20 → どちらも発火しない
_MIXED = {
    "current_price": 1100.0,
    "vwap": 1000.0,
    "ma5": 950.0,
    "ma20": 1000.0,
}


# ─── A. _rule_symbol_trend_up() 直テスト ─────────────────────────────────────

class TestRuleSymbolTrendUpDirect:
    """_rule_symbol_trend_up() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_guard_no_current_price(self):
        """current_price=None → skipped"""
        result = _call_trend_up({"vwap": 1000.0, "ma5": 1050.0, "ma20": 1000.0})
        assert result is None

    def test_guard_no_vwap(self):
        """vwap=None → skipped"""
        result = _call_trend_up({"current_price": 1100.0, "ma5": 1050.0, "ma20": 1000.0})
        assert result is None

    def test_guard_no_ma5(self):
        """ma5=None → skipped"""
        result = _call_trend_up({"current_price": 1100.0, "vwap": 1000.0, "ma20": 1000.0})
        assert result is None

    def test_guard_no_ma20(self):
        """ma20=None → skipped"""
        result = _call_trend_up({"current_price": 1100.0, "vwap": 1000.0, "ma5": 1050.0})
        assert result is None

    def test_guard_zero_vwap(self):
        """vwap=0 → skipped（ゼロ除算ガード）"""
        result = _call_trend_up({"current_price": 1100.0, "vwap": 0.0, "ma5": 1050.0, "ma20": 1000.0})
        assert result is None

    def test_guard_zero_ma20(self):
        """ma20=0 → skipped（ゼロ除算ガード）"""
        result = _call_trend_up({"current_price": 1100.0, "vwap": 1000.0, "ma5": 1050.0, "ma20": 0.0})
        assert result is None

    # ── 非発火 ──

    def test_no_fire_price_below_vwap(self):
        """price < vwap, ma5 > ma20 → inactive（trend_up は片方の条件だけでは発火しない）"""
        result = _call_trend_up({"current_price": 900.0, "vwap": 1000.0, "ma5": 1050.0, "ma20": 1000.0})
        assert result is None

    def test_no_fire_ma5_below_ma20(self):
        """price > vwap, ma5 < ma20 → inactive"""
        result = _call_trend_up(_MIXED)
        assert result is None

    def test_no_fire_both_bearish(self):
        """price < vwap AND ma5 < ma20（ダウントレンド）→ trend_up は発火しない"""
        result = _call_trend_up(_TREND_DOWN)
        assert result is None

    # ── 発火 ──

    def test_fires_when_both_bullish(self):
        """price > vwap AND ma5 > ma20 → 発火"""
        result = _call_trend_up(_TREND_UP)
        assert result is not None
        assert result.state_code == "symbol_trend_up"
        assert result.target_code == "7203"
        assert result.layer == "symbol"

    # ── score ──

    def test_score_floor_at_0_3(self):
        """差分が極小でも score は 0.3 以上"""
        result = _call_trend_up({
            "current_price": 1000.01,
            "vwap": 1000.0,
            "ma5": 1000.01,
            "ma20": 1000.0,
        })
        assert result is not None
        assert result.score >= 0.3

    def test_score_calculation(self):
        """既知の値で score を確認: vwap_diff=0.1, ma_diff=0.05 → (0.1+0.05)*20=3.0 → cap 1.0"""
        result = _call_trend_up({
            "current_price": 1100.0,  # vwap_diff = 100/1000 = 0.1
            "vwap": 1000.0,
            "ma5": 1050.0,            # ma_diff = 50/1000 = 0.05
            "ma20": 1000.0,
        })
        assert result is not None
        # (0.1 + 0.05) * 20 = 3.0 → min(1.0, 3.0) = 1.0
        assert result.score == pytest.approx(1.0, abs=1e-5)

    def test_score_capped_at_1(self):
        """大きな差分でも score は 1.0 でキャップ"""
        result = _call_trend_up({
            "current_price": 2000.0,
            "vwap": 1000.0,
            "ma5": 1500.0,
            "ma20": 1000.0,
        })
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    # ── evidence ──

    def test_evidence_fields(self):
        """evidence に必要なフィールドが含まれる"""
        result = _call_trend_up(_TREND_UP)
        assert result is not None
        ev = result.evidence
        assert ev["price_above_vwap"] is True
        assert ev["ma5_above_ma20"] is True
        assert "current_price" in ev
        assert "vwap" in ev
        assert "ma5" in ev
        assert "ma20" in ev
        assert "rule" in ev


# ─── B. _rule_symbol_trend_down() 直テスト ───────────────────────────────────

class TestRuleSymbolTrendDownDirect:
    """_rule_symbol_trend_down() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_guard_no_current_price(self):
        """current_price=None → skipped"""
        result = _call_trend_down({"vwap": 1000.0, "ma5": 950.0, "ma20": 1000.0})
        assert result is None

    def test_guard_no_vwap(self):
        """vwap=None → skipped"""
        result = _call_trend_down({"current_price": 900.0, "ma5": 950.0, "ma20": 1000.0})
        assert result is None

    # ── 非発火 ──

    def test_no_fire_price_above_vwap(self):
        """price > vwap → inactive（trend_down は発火しない）"""
        result = _call_trend_down({"current_price": 1100.0, "vwap": 1000.0, "ma5": 950.0, "ma20": 1000.0})
        assert result is None

    def test_no_fire_ma5_above_ma20(self):
        """ma5 > ma20 → inactive"""
        result = _call_trend_down({"current_price": 900.0, "vwap": 1000.0, "ma5": 1050.0, "ma20": 1000.0})
        assert result is None

    def test_no_fire_mixed_state(self):
        """price > vwap AND ma5 < ma20 → inactive（混合状態）"""
        result = _call_trend_down(_MIXED)
        assert result is None

    def test_no_fire_when_bullish(self):
        """price > vwap AND ma5 > ma20（アップトレンド）→ trend_down は発火しない"""
        result = _call_trend_down(_TREND_UP)
        assert result is None

    # ── 発火 ──

    def test_fires_when_both_bearish(self):
        """price < vwap AND ma5 < ma20 → 発火"""
        result = _call_trend_down(_TREND_DOWN)
        assert result is not None
        assert result.state_code == "symbol_trend_down"
        assert result.target_code == "7203"

    # ── score ──

    def test_score_calculation(self):
        """既知の値で score を確認: vwap_diff=0.1, ma_diff=0.05 → (0.1+0.05)*20=3.0 → cap 1.0"""
        result = _call_trend_down({
            "current_price": 900.0,   # vwap_diff = 100/1000 = 0.1
            "vwap": 1000.0,
            "ma5": 950.0,             # ma_diff = 50/1000 = 0.05
            "ma20": 1000.0,
        })
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    def test_score_capped_at_1(self):
        """大きな差分でも score は 1.0 でキャップ"""
        result = _call_trend_down({
            "current_price": 100.0,
            "vwap": 1000.0,
            "ma5": 500.0,
            "ma20": 1000.0,
        })
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    # ── evidence ──

    def test_evidence_fields(self):
        """evidence に price_above_vwap=False / ma5_above_ma20=False が含まれる"""
        result = _call_trend_down(_TREND_DOWN)
        assert result is not None
        ev = result.evidence
        assert ev["price_above_vwap"] is False
        assert ev["ma5_above_ma20"] is False
        assert "current_price" in ev
        assert "rule" in ev


# ─── C. orchestrator 経由テスト ──────────────────────────────────────────────

class TestOrchestratorTrend:
    """SymbolStateEvaluator.evaluate() 経由でトレンド状態が返ること"""

    def test_trend_up_via_evaluate(self):
        """アップトレンドデータを渡すと evaluate() から symbol_trend_up が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_TREND_UP)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_trend_up" in codes

    def test_trend_down_via_evaluate(self):
        """ダウントレンドデータを渡すと evaluate() から symbol_trend_down が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_TREND_DOWN)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_trend_down" in codes

    def test_no_trend_in_mixed_state(self):
        """混合状態ではどちらも発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_MIXED)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_trend_up" not in codes
        assert "symbol_trend_down" not in codes

    def test_trend_up_and_down_never_coexist(self):
        """trend_up と trend_down は同じデータで同時に active にならない（排他的）"""
        evaluator = SymbolStateEvaluator()

        ctx_up = _ctx(**_TREND_UP)
        results_up = evaluator.evaluate(ctx_up)
        codes_up = {r.state_code for r in results_up}
        assert "symbol_trend_up" in codes_up
        assert "symbol_trend_down" not in codes_up

        ctx_down = _ctx(**_TREND_DOWN)
        results_down = evaluator.evaluate(ctx_down)
        codes_down = {r.state_code for r in results_down}
        assert "symbol_trend_down" in codes_down
        assert "symbol_trend_up" not in codes_down

    def test_trend_up_coexists_with_high_relative_volume(self):
        """symbol_trend_up と high_relative_volume が同時 active になれる"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            **_TREND_UP,
            current_volume=300_000,
            avg_volume_same_time=100_000,  # vol_ratio=3.0 → high_relative_volume
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "symbol_trend_up" in codes, "symbol_trend_up が発火していない"
        assert "high_relative_volume" in codes, "high_relative_volume が発火していない"


# ─── D. 構造確認 ──────────────────────────────────────────────────────────────

class TestStructureTrend:
    """リファクタリング後の構造を確認する"""

    def test_rule_symbol_trend_up_is_module_level(self):
        """_rule_symbol_trend_up が module レベルに存在する"""
        assert hasattr(_mod, "_rule_symbol_trend_up")
        assert callable(_mod._rule_symbol_trend_up)

    def test_rule_symbol_trend_down_is_module_level(self):
        """_rule_symbol_trend_down が module レベルに存在する"""
        assert hasattr(_mod, "_rule_symbol_trend_down")
        assert callable(_mod._rule_symbol_trend_down)

    def test_evaluate_symbol_calls_trend_rules(self):
        """trend rule が evaluator module の _RULES に登録されている"""
        import inspect
        source = inspect.getsource(_mod)
        assert "_rule_symbol_trend_up" in source
        assert "_rule_symbol_trend_down" in source

    def test_evaluate_symbol_has_no_inline_trend(self):
        """
        _evaluate_symbol() 内に trend インライン判定が残っていないこと。

        rule 化後は vwap / ma5 / ma20 を _evaluate_symbol() のトップで
        data.get() しない。trend 計算は rule 関数内に移動している。
        """
        import inspect
        source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert 'data.get("vwap")' not in source, \
            "_evaluate_symbol() 内に vwap の直接抽出が残っている"
        assert 'data.get("ma5")' not in source, \
            "_evaluate_symbol() 内に ma5 の直接抽出が残っている"
        assert 'data.get("ma20")' not in source, \
            "_evaluate_symbol() 内に ma20 の直接抽出が残っている"


# ─── E. 遷移テスト ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSymbolTrendUpTransitions:
    """symbol_trend_up の activated / continued / deactivated 遷移テスト"""

    async def test_initial_trend_up_inserts_row(self, db_session: AsyncSession):
        """初回 trend_up → StateEvaluation が INSERT される（is_active=True）"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _TREND_UP},
        )
        results = await engine.run(ctx)

        trend_results = [r for r in results if r.state_code == "symbol_trend_up"]
        assert len(trend_results) == 1
        assert trend_results[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "symbol_trend_up" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """trend_up が継続するとき、2回目は新規 INSERT されない"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _TREND_UP},
        )
        await engine.run(ctx1)

        repo = MarketStateRepository(db_session)
        rows_after_1 = await repo.get_symbol_active_evaluations("7203")
        trend_1 = [r for r in rows_after_1 if r.state_code == "symbol_trend_up"]
        assert len(trend_1) == 1
        first_id = trend_1[0].id

        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _TREND_UP},
        )
        results2 = await engine.run(ctx2)
        continuation = [r for r in results2 if r.state_code == "symbol_trend_up"]
        assert len(continuation) == 1
        assert continuation[0].is_new_activation is False

        rows_after_2 = await repo.get_symbol_active_evaluations("7203")
        trend_2 = [r for r in rows_after_2 if r.state_code == "symbol_trend_up"]
        assert len(trend_2) == 1
        assert trend_2[0].id == first_id

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """トレンド解消で symbol_trend_up が is_active=False になる"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _TREND_UP},
        )
        await engine.run(ctx1)

        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _MIXED},  # トレンド解消（混合状態）
        )
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert not any(r.state_code == "symbol_trend_up" for r in active)

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """trend_up 解消後に再発火すると新規 INSERT される"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _TREND_UP},
        )
        await engine.run(ctx1)

        repo = MarketStateRepository(db_session)
        rows_1 = await repo.get_symbol_active_evaluations("7203")
        trend_1 = [r for r in rows_1 if r.state_code == "symbol_trend_up"]
        first_id = trend_1[0].id

        # 解消
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _MIXED},
        )
        await engine.run(ctx2)

        # 再発火
        ctx3 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _TREND_UP},
        )
        results3 = await engine.run(ctx3)
        reactivated = [r for r in results3 if r.state_code == "symbol_trend_up"]
        assert len(reactivated) == 1
        assert reactivated[0].is_new_activation is True

        rows_3 = await repo.get_symbol_active_evaluations("7203")
        trend_3 = [r for r in rows_3 if r.state_code == "symbol_trend_up"]
        assert len(trend_3) == 1
        assert trend_3[0].id != first_id


# ─── F. observability テスト ──────────────────────────────────────────────────

class TestTrendDiagnostic:
    """rule_diagnostics にトレンド rule の診断情報が含まれること"""

    def test_trend_up_active_diag(self):
        """アップトレンド時: status=active / vwap_diff / ma_diff が含まれる"""
        _result, diag = _call_trend_up_with_diag(_TREND_UP)
        assert diag["status"] == "active"
        assert "vwap_diff" in diag
        assert "ma_diff" in diag
        assert diag["vwap_diff"] > 0
        assert diag["ma_diff"] > 0

    def test_trend_down_active_diag(self):
        """ダウントレンド時: status=active / vwap_diff / ma_diff が含まれる"""
        _result, diag = _call_trend_down_with_diag(_TREND_DOWN)
        assert diag["status"] == "active"
        assert "vwap_diff" in diag
        assert "ma_diff" in diag

    def test_trend_up_inactive_diag(self):
        """混合状態: trend_up の診断が status=inactive / price_above_vwap / ma5_above_ma20 を含む"""
        _result, diag = _call_trend_up_with_diag(_MIXED)
        assert diag["status"] == "inactive"
        assert "price_above_vwap" in diag
        assert "ma5_above_ma20" in diag

    def test_trend_down_inactive_diag(self):
        """アップトレンド状態: trend_down の診断が status=inactive"""
        _result, diag = _call_trend_down_with_diag(_TREND_UP)
        assert diag["status"] == "inactive"
        assert "price_above_vwap" in diag

    def test_trend_up_skipped_no_vwap(self):
        """vwap なし: trend_up の診断が status=skipped"""
        _result, diag = _call_trend_up_with_diag({"current_price": 1100.0, "ma5": 1050.0, "ma20": 1000.0})
        assert diag["status"] == "skipped"
        assert diag.get("reason") == "no_vwap"

    def test_trend_down_skipped_no_ma5(self):
        """ma5 なし: trend_down の診断が status=skipped"""
        _result, diag = _call_trend_down_with_diag({"current_price": 900.0, "vwap": 1000.0, "ma20": 1000.0})
        assert diag["status"] == "skipped"
        assert diag.get("reason") == "no_ma5"

    def test_both_trend_keys_present_in_empty_data(self):
        """空データでも symbol_trend_up / symbol_trend_down の両キーが rule_diagnostics に存在する"""
        diags = _diags({})
        assert "symbol_trend_up" in diags, \
            f"symbol_trend_up が rule_diagnostics にない: {list(diags.keys())}"
        assert "symbol_trend_down" in diags, \
            f"symbol_trend_down が rule_diagnostics にない: {list(diags.keys())}"

    def test_trend_up_active_via_evaluate_symbol(self):
        """_evaluate_symbol() 経由でも trend_up active 診断が得られる"""
        diags = _diags(_TREND_UP)
        assert diags["symbol_trend_up"]["status"] == "active"

    def test_trend_down_active_via_evaluate_symbol(self):
        """_evaluate_symbol() 経由でも trend_down active 診断が得られる"""
        diags = _diags(_TREND_DOWN)
        assert diags["symbol_trend_down"]["status"] == "active"
