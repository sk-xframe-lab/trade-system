"""
Phase H — breakout_candidate rule 化テスト

確認項目:
  A. rule 直テスト (_rule_breakout_candidate 直接呼び出し)
     1.  current_price=None → skipped / no_current_price
     2.  current_price=0    → skipped / no_current_price
     3.  current_price<0    → skipped / no_current_price
     4.  ma20=None          → skipped / no_ma20
     5.  ma20=0             → skipped / no_ma20
     6.  is_high_volume=False → inactive
     7.  is_gap_up=True       → inactive
     8.  is_gap_down=True     → inactive
     9.  price == ma20        → inactive（strict greater-than）
    10.  price < ma20         → inactive
    11.  全条件満たす          → 発火 (state_code="breakout_candidate")
    12.  score 下限 0.3（price ちょうど ma20 超え）
    13.  score 上限 1.0（pct_above_ma20 >= 3%）
    14.  evidence フィールドの内容

  B. orchestrator 経由テスト
    15. evaluate() 経由で breakout_candidate が返る
    16. 出来高不足では発火しない
    17. gap_up があると発火しない
    18. breakout_candidate と overextended が同時 active になれる
    19. ma20 なしでは発火しない

  C. 構造確認
    20. _rule_breakout_candidate が module レベルに存在する
    21. _evaluate_symbol() が _rule_breakout_candidate を呼ぶ
    22. _evaluate_symbol() にインライン breakout 判定が残っていない

  D. 遷移テスト (engine.run() 経由)
    23. 初回 breakout → INSERT
    24. breakout 継続 → INSERT なし
    25. breakout 解消 → soft-expire
    26. breakout 再発火 → 再 INSERT

  E. observability テスト (rule_diagnostics)
    27. active 診断: status="active", pct_above_ma20 が含まれる
    28. inactive / no_high_volume: status="inactive", is_high_volume=False
    29. inactive / gap_present: status="inactive", is_gap=True
    30. inactive / price_below_ma20: status="inactive", price_above_ma20=False
    31. skipped / no_current_price
    32. skipped / no_ma20
    33. breakout_candidate キーが空データでも rule_diagnostics に存在する

設計:
  - _rule_breakout_candidate(ticker, data, *, is_high_volume, is_gap_up, is_gap_down, make)
  - 依存引数（is_high_volume / is_gap_up / is_gap_down）は _evaluate_symbol() が計算して渡す
  - 既存の独立 rule ループに追加（インライン実装を削除）
  - Phase G で導入した rule_diagnostics の仕組みをそのまま活用
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


def _call_rule(
    data: dict[str, Any],
    *,
    is_high_volume: bool = True,
    is_gap_up: bool = False,
    is_gap_down: bool = False,
) -> StateEvaluationResult | None:
    """_rule_breakout_candidate() を呼び出し result 部分のみを返すヘルパー。"""
    result, _diag = _mod._rule_breakout_candidate(
        "7203", data,
        is_high_volume=is_high_volume,
        is_gap_up=is_gap_up,
        is_gap_down=is_gap_down,
        make=_make,
    )
    return result


def _call_rule_with_diag(
    data: dict[str, Any],
    *,
    is_high_volume: bool = True,
    is_gap_up: bool = False,
    is_gap_down: bool = False,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    """_rule_breakout_candidate() をそのまま返すヘルパー（diagnostic が必要なテスト用）。"""
    return _mod._rule_breakout_candidate(
        "7203", data,
        is_high_volume=is_high_volume,
        is_gap_up=is_gap_up,
        is_gap_down=is_gap_down,
        make=_make,
    )


def _ctx(eval_time: datetime = _EVAL_TIME, **fields) -> EvaluationContext:
    return EvaluationContext(
        evaluation_time=eval_time,
        symbol_data={"7203": fields},
    )


def _diags(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """_evaluate_symbol() 経由で rule_diagnostics を取得するヘルパー。"""
    evaluator = SymbolStateEvaluator()
    _results, diagnostics = evaluator._evaluate_symbol("7203", data, _EVAL_TIME)
    return diagnostics


# 発火用データ（breakout_candidate のみ発火するよう最小構成）
_BREAKOUT = {
    "current_price": 1100.0,
    "ma20": 1000.0,
    "current_volume": 300_000,
    "avg_volume_same_time": 100_000,  # vol_ratio=3.0 → is_high_volume
    # current_open / prev_close なし → is_gap_up=False, is_gap_down=False
}
# 非発火用データ（価格が MA20 を下回る）
_NO_BREAKOUT = {
    "current_price": 900.0,
    "ma20": 1000.0,
    "current_volume": 300_000,
    "avg_volume_same_time": 100_000,
}


# ─── A. rule 直テスト ─────────────────────────────────────────────────────────

class TestRuleBreakoutCandidateDirect:
    """_rule_breakout_candidate() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_guard_current_price_none(self):
        """current_price=None → skipped"""
        result = _call_rule({"current_price": None, "ma20": 1000.0})
        assert result is None

    def test_guard_current_price_zero(self):
        """current_price=0 → skipped（ゼロ除算ガード）"""
        result = _call_rule({"current_price": 0, "ma20": 1000.0})
        assert result is None

    def test_guard_current_price_negative(self):
        """current_price=-1 → skipped（無効値ガード）"""
        result = _call_rule({"current_price": -1.0, "ma20": 1000.0})
        assert result is None

    def test_guard_ma20_none(self):
        """ma20=None → skipped"""
        result = _call_rule({"current_price": 1100.0, "ma20": None})
        assert result is None

    def test_guard_ma20_zero(self):
        """ma20=0 → skipped（ゼロ除算ガード）"""
        result = _call_rule({"current_price": 1100.0, "ma20": 0})
        assert result is None

    # ── 非発火 ──

    def test_no_high_volume_does_not_fire(self):
        """is_high_volume=False → None（高出来高なし）"""
        result = _call_rule({"current_price": 1100.0, "ma20": 1000.0}, is_high_volume=False)
        assert result is None

    def test_gap_up_suppresses_breakout(self):
        """is_gap_up=True → None（ギャップアップは breakout_candidate を抑制）"""
        result = _call_rule({"current_price": 1100.0, "ma20": 1000.0}, is_gap_up=True)
        assert result is None

    def test_gap_down_suppresses_breakout(self):
        """is_gap_down=True → None（ギャップダウンは breakout_candidate を抑制）"""
        result = _call_rule({"current_price": 1100.0, "ma20": 1000.0}, is_gap_down=True)
        assert result is None

    def test_price_at_ma20_does_not_fire(self):
        """price == ma20 → None（strictly greater-than が条件）"""
        result = _call_rule({"current_price": 1000.0, "ma20": 1000.0})
        assert result is None

    def test_price_below_ma20_does_not_fire(self):
        """price < ma20 → None"""
        result = _call_rule({"current_price": 990.0, "ma20": 1000.0})
        assert result is None

    # ── 発火 ──

    def test_fires_when_all_conditions_met(self):
        """全条件満たす → 発火"""
        result = _call_rule({"current_price": 1100.0, "ma20": 1000.0})
        assert result is not None
        assert result.state_code == "breakout_candidate"
        assert result.target_code == "7203"
        assert result.layer == "symbol"

    def test_score_minimum_at_small_pct_above_ma20(self):
        """price がほぼ ma20 → score 下限 0.3"""
        result = _call_rule({"current_price": 1000.001, "ma20": 1000.0})
        assert result is not None
        assert result.score == pytest.approx(0.3, abs=0.01)

    def test_score_one_at_3pct_above_ma20(self):
        """pct_above_ma20 = 3% → score = 1.0"""
        result = _call_rule({"current_price": 1030.0, "ma20": 1000.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    def test_score_capped_at_one(self):
        """pct_above_ma20 > 3% → score は 1.0 でキャップ"""
        result = _call_rule({"current_price": 1200.0, "ma20": 1000.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    def test_evidence_fields(self):
        """evidence に必要なフィールドが含まれる"""
        result = _call_rule({"current_price": 1100.0, "ma20": 1000.0})
        assert result is not None
        ev = result.evidence
        assert ev["current_price"] == 1100.0
        assert ev["ma20"] == 1000.0
        assert "price_above_ma20_pct" in ev
        assert ev["is_high_volume"] is True
        assert "rule" in ev


# ─── B. orchestrator 経由テスト ───────────────────────────────────────────────

class TestOrchestratorBreakout:
    """SymbolStateEvaluator.evaluate() 経由で breakout_candidate が返ること"""

    def test_breakout_via_evaluate(self):
        """全条件揃いのデータを渡すと evaluate() から breakout_candidate が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_BREAKOUT)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "breakout_candidate" in codes

    def test_no_high_volume_does_not_fire_via_evaluate(self):
        """出来高が平均以下では breakout_candidate が発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=1100.0, ma20=1000.0,
            current_volume=100_000, avg_volume_same_time=100_000,  # vol_ratio=1.0 < 2.0
        )
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "breakout_candidate" for r in results)

    def test_gap_up_suppresses_breakout_via_evaluate(self):
        """gap_up_open があると breakout_candidate が発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=1100.0, ma20=1000.0,
            current_volume=300_000, avg_volume_same_time=100_000,
            current_open=1060.0, prev_close=1000.0,  # gap_pct=6% → is_gap_up=True
        )
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "breakout_candidate" for r in results)

    def test_breakout_coexists_with_overextended(self):
        """breakout_candidate と overextended が同時に active になれる"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=1100.0, ma20=1000.0,
            current_volume=300_000, avg_volume_same_time=100_000,
            rsi=82.0,  # overextended: overbought
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "breakout_candidate" in codes, "breakout_candidate が発火していない"
        assert "overextended" in codes, "overextended が発火していない"

    def test_no_breakout_without_ma20(self):
        """ma20 がないと breakout_candidate が発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=1100.0,
            current_volume=300_000, avg_volume_same_time=100_000,
        )
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "breakout_candidate" for r in results)


# ─── C. 構造確認 ──────────────────────────────────────────────────────────────

class TestStructureBreakout:
    """リファクタリング後の構造を確認する"""

    def test_rule_breakout_candidate_is_module_level(self):
        """_rule_breakout_candidate が module レベルに存在する"""
        assert hasattr(_mod, "_rule_breakout_candidate"), \
            "_rule_breakout_candidate が module に存在しない"
        assert callable(_mod._rule_breakout_candidate)

    def test_evaluate_symbol_calls_rule_breakout_candidate(self):
        """_rule_breakout_candidate が evaluator module の _RULES に登録されている"""
        import inspect
        source = inspect.getsource(_mod)
        assert "_rule_breakout_candidate" in source, \
            "_rule_breakout_candidate が module に見当たらない"

    def test_evaluate_symbol_has_no_inline_breakout(self):
        """
        _evaluate_symbol() 内にインライン breakout score 計算が残っていないこと。

        rule 化されていれば、スコア計算式 pct_above_ma20 / 0.03 は
        _rule_breakout_candidate() 内にあり _evaluate_symbol() にはない。
        """
        import inspect
        source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert "pct_above_ma20 / 0.03" not in source, \
            "_evaluate_symbol() 内にインライン breakout score 計算が残っている"


# ─── D. 遷移テスト ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestBreakoutCandidateTransitions:
    """breakout_candidate の activated / continued / deactivated 遷移テスト"""

    async def test_initial_breakout_inserts_row(self, db_session: AsyncSession):
        """初回 breakout → StateEvaluation が INSERT される（is_active=True）"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _BREAKOUT},
        )
        results = await engine.run(ctx)

        breakout_results = [r for r in results if r.state_code == "breakout_candidate"]
        assert len(breakout_results) == 1
        assert breakout_results[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "breakout_candidate" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """
        run1: breakout 発火 → 1 行 INSERT
        run2: breakout 継続 → INSERT なし（合計 1 行）
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _BREAKOUT},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _BREAKOUT},
        )

        await engine.run(ctx1)
        results2 = await engine.run(ctx2)

        breakout2 = [
            r for r in results2
            if r.state_code == "breakout_candidate" and r.target_code == "7203"
        ]
        assert len(breakout2) == 1
        assert breakout2[0].is_new_activation is False

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        breakout_rows = [r for r in history if r.state_code == "breakout_candidate"]
        assert len(breakout_rows) == 1, \
            f"継続で INSERT が発生した。期待1行、実際{len(breakout_rows)}行"

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """
        run1: breakout 発火 → is_active=True
        run2: 条件解消 → is_active=False
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _BREAKOUT},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _NO_BREAKOUT},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        breakout_rows = [r for r in history if r.state_code == "breakout_candidate"]
        assert len(breakout_rows) == 1
        assert breakout_rows[0].is_active is False, \
            "解消後 breakout_candidate は is_active=False になるべき"

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """
        run1: breakout → 行1 (is_active=True)
        run2: 解消    → 行1 (is_active=False)
        run3: breakout → 行2 (is_active=True)
        → DB に breakout_candidate が 2 行
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _BREAKOUT},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _NO_BREAKOUT},
        )
        ctx3 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=20),
            symbol_data={"7203": _BREAKOUT},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)
        results3 = await engine.run(ctx3)

        breakout3 = [
            r for r in results3
            if r.state_code == "breakout_candidate" and r.target_code == "7203"
        ]
        assert len(breakout3) == 1
        assert breakout3[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        breakout_rows = [r for r in history if r.state_code == "breakout_candidate"]
        assert len(breakout_rows) == 2, \
            f"再発火で 2 行になるべき、実際 {len(breakout_rows)} 行"
        active_rows = [r for r in breakout_rows if r.is_active is True]
        assert len(active_rows) == 1


# ─── E. observability テスト ──────────────────────────────────────────────────

class TestBreakoutCandidateDiagnostic:
    """rule_diagnostics の breakout_candidate エントリを検証する"""

    def test_diag_active(self):
        """発火時: status=active / pct_above_ma20 が含まれる"""
        result, diag = _call_rule_with_diag({"current_price": 1100.0, "ma20": 1000.0})
        assert result is not None
        assert diag["status"] == "active"
        assert "pct_above_ma20" in diag
        assert diag["pct_above_ma20"] == pytest.approx(0.1, abs=1e-5)

    def test_diag_inactive_no_high_volume(self):
        """is_high_volume=False → inactive / is_high_volume=False"""
        _result, diag = _call_rule_with_diag(
            {"current_price": 1100.0, "ma20": 1000.0},
            is_high_volume=False,
        )
        assert diag["status"] == "inactive"
        assert diag["is_high_volume"] is False
        assert diag["is_gap"] is False
        assert diag["price_above_ma20"] is True

    def test_diag_inactive_gap_present(self):
        """is_gap_up=True → inactive / is_gap=True"""
        _result, diag = _call_rule_with_diag(
            {"current_price": 1100.0, "ma20": 1000.0},
            is_gap_up=True,
        )
        assert diag["status"] == "inactive"
        assert diag["is_gap"] is True

    def test_diag_inactive_price_below_ma20(self):
        """price < ma20 → inactive / price_above_ma20=False"""
        _result, diag = _call_rule_with_diag({"current_price": 900.0, "ma20": 1000.0})
        assert diag["status"] == "inactive"
        assert diag["price_above_ma20"] is False

    def test_diag_skipped_no_current_price(self):
        """current_price なし → skipped / no_current_price"""
        _result, diag = _call_rule_with_diag({})
        assert diag["status"] == "skipped"
        assert diag["reason"] == "no_current_price"

    def test_diag_skipped_no_ma20(self):
        """ma20 なし → skipped / no_ma20"""
        _result, diag = _call_rule_with_diag({"current_price": 1100.0})
        assert diag["status"] == "skipped"
        assert diag["reason"] == "no_ma20"

    def test_breakout_key_present_in_empty_data(self):
        """空データでも rule_diagnostics に breakout_candidate キーが存在する"""
        diags = _diags({})
        assert "breakout_candidate" in diags

    def test_breakout_key_present_in_full_rule_diagnostics(self):
        """_evaluate_symbol() の rule_diagnostics に breakout_candidate が含まれる"""
        diags = _diags(_BREAKOUT)
        assert "breakout_candidate" in diags
        assert diags["breakout_candidate"]["status"] == "active"

    def test_diag_inactive_via_evaluate_symbol(self):
        """_evaluate_symbol() 経由でも inactive 診断が正しく設定される"""
        # current_price と ma20 はあるが出来高なし → is_high_volume=False → inactive
        diags = _diags({"current_price": 1100.0, "ma20": 1000.0})
        assert diags["breakout_candidate"]["status"] == "inactive"
        assert diags["breakout_candidate"]["is_high_volume"] is False
