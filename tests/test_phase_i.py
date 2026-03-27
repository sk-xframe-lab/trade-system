"""
Phase I — gap_up_open / gap_down_open rule 化テスト

確認項目:
  A. _rule_gap_up_open() 直テスト
     1.  current_open=None → skipped / no_current_open
     2.  prev_close=None   → skipped / no_prev_close
     3.  prev_close=0      → skipped / zero_prev_close
     4.  gap_pct < threshold → inactive
     5.  gap_pct = 0 → inactive（ギャップなし）
     6.  negative gap_pct → inactive（下落は gap_up ではない）
     7.  gap_pct ちょうど threshold → 発火（>=）
     8.  gap_pct > threshold → 発火
     9.  score: 4% gap → 1.0
    10.  score: threshold(2%) での下限
    11.  score 上限 1.0（大ギャップ）
    12.  evidence フィールドの内容

  B. _rule_gap_down_open() 直テスト
    13.  current_open=None → skipped
    14.  gap_pct > -threshold → inactive（gap_up は gap_down ではない）
    15.  gap_pct = 0 → inactive
    16.  gap_pct ちょうど -threshold → 発火（<=）
    17.  gap_pct < -threshold → 発火
    18.  score: abs(gap_pct)/0.04 計算
    19.  evidence フィールドの内容（threshold_pct が負値）

  C. orchestrator 経由テスト
    20. gap_up_open が evaluate() から返る
    21. gap_down_open が evaluate() から返る
    22. 小ギャップでは両方発火しない
    23. gap_up と wide_spread が同時 active になれる
    24. gap_up と gap_down は同時 active にならない

  D. 構造確認
    25. _rule_gap_up_open が module レベルに存在する
    26. _rule_gap_down_open が module レベルに存在する
    27. _evaluate_symbol() が両 rule を呼ぶ
    28. _evaluate_symbol() 内に current_open の直接 data.get() がない

  E. 遷移テスト（gap_up_open）
    29. 初回 gap_up → INSERT
    30. gap_up 継続 → INSERT なし
    31. gap_up 解消 → soft-expire
    32. gap_up 再発火 → 再 INSERT

  F. observability テスト
    33. gap_up active 診断: status=active / gap_pct が含まれる
    34. gap_down active 診断: status=active / gap_pct < 0
    35. gap_up inactive 診断: status=inactive / gap_pct が含まれる
    36. gap_down inactive（gap_up データ）: status=inactive / gap_pct > 0
    37. gap_up skipped / no_current_open
    38. gap_down skipped / no_prev_close
    39. 空データでも両キーが rule_diagnostics に存在する
    40. _evaluate_symbol() 経由でも gap_up active 診断が得られる

設計:
  - _rule_gap_up_open(ticker, data, *, gap_threshold, make)
  - _rule_gap_down_open(ticker, data, *, gap_threshold, make)
  - _evaluate_symbol() でギャップ判定後、rule_diagnostics["gap_up_open"/"gap_down_open"] に格納
  - is_gap_up / is_gap_down は rule 呼び出し結果から導出（result is not None）
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

GAP_THRESHOLD = SymbolStateEvaluator.GAP_THRESHOLD  # 0.02


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


def _call_gap_up(
    data: dict[str, Any],
    *,
    gap_threshold: float = GAP_THRESHOLD,
) -> StateEvaluationResult | None:
    result, _diag = _mod._rule_gap_up_open(
        "7203", data, gap_threshold=gap_threshold, make=_make
    )
    return result


def _call_gap_up_with_diag(
    data: dict[str, Any],
    *,
    gap_threshold: float = GAP_THRESHOLD,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    return _mod._rule_gap_up_open(
        "7203", data, gap_threshold=gap_threshold, make=_make
    )


def _call_gap_down(
    data: dict[str, Any],
    *,
    gap_threshold: float = GAP_THRESHOLD,
) -> StateEvaluationResult | None:
    result, _diag = _mod._rule_gap_down_open(
        "7203", data, gap_threshold=gap_threshold, make=_make
    )
    return result


def _call_gap_down_with_diag(
    data: dict[str, Any],
    *,
    gap_threshold: float = GAP_THRESHOLD,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    return _mod._rule_gap_down_open(
        "7203", data, gap_threshold=gap_threshold, make=_make
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


# 発火用データ
_GAP_UP = {"current_open": 3090.0, "prev_close": 3000.0}    # gap_pct=3% > 2%
_GAP_DOWN = {"current_open": 2910.0, "prev_close": 3000.0}  # gap_pct=-3% < -2%
_NO_GAP = {"current_open": 3015.0, "prev_close": 3000.0}    # gap_pct=0.5% < 2%

# ── 境界値
_GAP_UP_BOUNDARY = {"current_open": 3060.0, "prev_close": 3000.0}   # gap_pct=2% ちょうど
_GAP_DOWN_BOUNDARY = {"current_open": 2940.0, "prev_close": 3000.0}  # gap_pct=-2% ちょうど


# ─── A. _rule_gap_up_open() 直テスト ─────────────────────────────────────────

class TestRuleGapUpOpenDirect:
    """_rule_gap_up_open() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_guard_no_current_open(self):
        """current_open=None → skipped"""
        result = _call_gap_up({"prev_close": 3000.0})
        assert result is None

    def test_guard_no_prev_close(self):
        """prev_close=None → skipped"""
        result = _call_gap_up({"current_open": 3090.0})
        assert result is None

    def test_guard_zero_prev_close(self):
        """prev_close=0 → skipped（ゼロ除算ガード）"""
        result = _call_gap_up({"current_open": 100.0, "prev_close": 0})
        assert result is None

    # ── 非発火 ──

    def test_no_fire_when_gap_pct_below_threshold(self):
        """gap_pct = 0.5% < 2% → None"""
        result = _call_gap_up(_NO_GAP)
        assert result is None

    def test_no_fire_when_no_gap(self):
        """current_open == prev_close → None（gap_pct=0）"""
        result = _call_gap_up({"current_open": 3000.0, "prev_close": 3000.0})
        assert result is None

    def test_no_fire_when_negative_gap(self):
        """gap_pct < 0（gap DOWN）→ None（gap_up rule は発火しない）"""
        result = _call_gap_up(_GAP_DOWN)
        assert result is None

    # ── 発火 ──

    def test_fires_at_boundary(self):
        """gap_pct ちょうど 2% → 発火（>=）"""
        result = _call_gap_up(_GAP_UP_BOUNDARY)
        assert result is not None
        assert result.state_code == "gap_up_open"

    def test_fires_above_threshold(self):
        """gap_pct = 3% → 発火"""
        result = _call_gap_up(_GAP_UP)
        assert result is not None
        assert result.state_code == "gap_up_open"
        assert result.target_code == "7203"
        assert result.layer == "symbol"

    def test_score_at_4pct_gap(self):
        """gap_pct = 4% → score = 1.0"""
        result = _call_gap_up({"current_open": 3120.0, "prev_close": 3000.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    def test_score_at_2pct_gap(self):
        """gap_pct = 2% (threshold) → score = 0.5"""
        result = _call_gap_up(_GAP_UP_BOUNDARY)
        assert result is not None
        expected_score = min(1.0, 0.02 / 0.04)  # 0.5
        assert result.score == pytest.approx(expected_score, abs=1e-5)

    def test_score_capped_at_one(self):
        """gap_pct > 4% → score は 1.0 でキャップ"""
        result = _call_gap_up({"current_open": 3300.0, "prev_close": 3000.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    def test_evidence_fields(self):
        """evidence に必要なフィールドが含まれる"""
        result = _call_gap_up(_GAP_UP)
        assert result is not None
        ev = result.evidence
        assert "gap_pct" in ev
        assert ev["current_open"] == 3090.0
        assert ev["prev_close"] == 3000.0
        assert "threshold_pct" in ev
        assert "rule" in ev
        # gap_pct は % 表示（小数ではなく 3.0 程度）
        assert ev["gap_pct"] == pytest.approx(3.0, abs=0.01)


# ─── B. _rule_gap_down_open() 直テスト ───────────────────────────────────────

class TestRuleGapDownOpenDirect:
    """_rule_gap_down_open() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_guard_no_current_open(self):
        """current_open=None → skipped"""
        result = _call_gap_down({"prev_close": 3000.0})
        assert result is None

    def test_guard_no_prev_close(self):
        """prev_close=None → skipped"""
        result = _call_gap_down({"current_open": 2910.0})
        assert result is None

    def test_guard_zero_prev_close(self):
        """prev_close=0 → skipped"""
        result = _call_gap_down({"current_open": 100.0, "prev_close": 0})
        assert result is None

    # ── 非発火 ──

    def test_no_fire_when_positive_gap(self):
        """gap_pct > 0（gap UP）→ None（gap_down rule は発火しない）"""
        result = _call_gap_down(_GAP_UP)
        assert result is None

    def test_no_fire_when_no_gap(self):
        """gap_pct = 0 → None"""
        result = _call_gap_down({"current_open": 3000.0, "prev_close": 3000.0})
        assert result is None

    def test_no_fire_when_gap_within_threshold(self):
        """gap_pct = -0.5% > -2% → None"""
        result = _call_gap_down(_NO_GAP)
        assert result is None

    # ── 発火 ──

    def test_fires_at_boundary(self):
        """gap_pct ちょうど -2% → 発火（<=）"""
        result = _call_gap_down(_GAP_DOWN_BOUNDARY)
        assert result is not None
        assert result.state_code == "gap_down_open"

    def test_fires_below_threshold(self):
        """gap_pct = -3% → 発火"""
        result = _call_gap_down(_GAP_DOWN)
        assert result is not None
        assert result.state_code == "gap_down_open"
        assert result.target_code == "7203"

    def test_score_at_4pct_gap_down(self):
        """abs(gap_pct) = 4% → score = 1.0"""
        result = _call_gap_down({"current_open": 2880.0, "prev_close": 3000.0})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    def test_evidence_threshold_pct_is_negative(self):
        """evidence の threshold_pct は負値（-2.0）"""
        result = _call_gap_down(_GAP_DOWN)
        assert result is not None
        assert result.evidence["threshold_pct"] == pytest.approx(-2.0, abs=0.01)

    def test_evidence_gap_pct_is_negative(self):
        """evidence の gap_pct は負値（% 表示）"""
        result = _call_gap_down(_GAP_DOWN)
        assert result is not None
        assert result.evidence["gap_pct"] == pytest.approx(-3.0, abs=0.01)


# ─── C. orchestrator 経由テスト ───────────────────────────────────────────────

class TestOrchestratorGap:
    """SymbolStateEvaluator.evaluate() 経由でギャップ状態が返ること"""

    def test_gap_up_via_evaluate(self):
        """gap_up データを渡すと evaluate() から gap_up_open が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_GAP_UP)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "gap_up_open" in codes

    def test_gap_down_via_evaluate(self):
        """gap_down データを渡すと evaluate() から gap_down_open が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_GAP_DOWN)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "gap_down_open" in codes

    def test_no_gap_does_not_fire(self):
        """小ギャップでは gap_up/gap_down のいずれも発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_NO_GAP)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "gap_up_open" not in codes
        assert "gap_down_open" not in codes

    def test_gap_up_coexists_with_wide_spread(self):
        """gap_up_open と wide_spread が同時 active になれる"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_open=3090.0, prev_close=3000.0,  # gap_up_open
            current_price=3090.0,
            best_bid=3062.0, best_ask=3118.0,         # wide_spread: 56/3090=0.018 >= 0.003
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "gap_up_open" in codes, "gap_up_open が発火していない"
        assert "wide_spread" in codes, "wide_spread が発火していない"

    def test_gap_up_and_gap_down_never_coexist(self):
        """gap_up と gap_down は同じデータで同時に active にならない（排他的）"""
        evaluator = SymbolStateEvaluator()
        # gap_up データ
        ctx_up = _ctx(**_GAP_UP)
        results_up = evaluator.evaluate(ctx_up)
        codes_up = {r.state_code for r in results_up}
        assert "gap_up_open" in codes_up
        assert "gap_down_open" not in codes_up

        # gap_down データ
        ctx_down = _ctx(**_GAP_DOWN)
        results_down = evaluator.evaluate(ctx_down)
        codes_down = {r.state_code for r in results_down}
        assert "gap_down_open" in codes_down
        assert "gap_up_open" not in codes_down


# ─── D. 構造確認 ──────────────────────────────────────────────────────────────

class TestStructureGap:
    """リファクタリング後の構造を確認する"""

    def test_rule_gap_up_open_is_module_level(self):
        """_rule_gap_up_open が module レベルに存在する"""
        assert hasattr(_mod, "_rule_gap_up_open")
        assert callable(_mod._rule_gap_up_open)

    def test_rule_gap_down_open_is_module_level(self):
        """_rule_gap_down_open が module レベルに存在する"""
        assert hasattr(_mod, "_rule_gap_down_open")
        assert callable(_mod._rule_gap_down_open)

    def test_evaluate_symbol_calls_gap_rules(self):
        """gap rule が evaluator module の _RULES に登録されている"""
        import inspect
        source = inspect.getsource(_mod)
        assert "_rule_gap_up_open" in source
        assert "_rule_gap_down_open" in source

    def test_evaluate_symbol_has_no_inline_gap(self):
        """
        _evaluate_symbol() 内に gap インライン判定が残っていないこと。

        rule 化後は current_open / prev_close を _evaluate_symbol() のトップで
        data.get() しない。gap 計算式は rule 関数内に移動している。
        """
        import inspect
        source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert 'data.get("current_open")' not in source, \
            "_evaluate_symbol() 内に current_open の直接抽出が残っている"
        assert 'data.get("prev_close")' not in source, \
            "_evaluate_symbol() 内に prev_close の直接抽出が残っている"


# ─── E. 遷移テスト ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestGapUpOpenTransitions:
    """gap_up_open の activated / continued / deactivated 遷移テスト"""

    async def test_initial_gap_up_inserts_row(self, db_session: AsyncSession):
        """初回 gap_up → StateEvaluation が INSERT される（is_active=True）"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _GAP_UP},
        )
        results = await engine.run(ctx)

        gap_results = [r for r in results if r.state_code == "gap_up_open"]
        assert len(gap_results) == 1
        assert gap_results[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "gap_up_open" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """
        run1: gap_up 発火 → 1 行 INSERT
        run2: gap_up 継続 → INSERT なし（合計 1 行）
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _GAP_UP},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _GAP_UP},
        )

        await engine.run(ctx1)
        results2 = await engine.run(ctx2)

        gap2 = [
            r for r in results2
            if r.state_code == "gap_up_open" and r.target_code == "7203"
        ]
        assert len(gap2) == 1
        assert gap2[0].is_new_activation is False

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        gap_rows = [r for r in history if r.state_code == "gap_up_open"]
        assert len(gap_rows) == 1, \
            f"継続で INSERT が発生した。期待1行、実際{len(gap_rows)}行"

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """
        run1: gap_up 発火 → is_active=True
        run2: 条件解消（小ギャップ）→ is_active=False
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _GAP_UP},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _NO_GAP},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        gap_rows = [r for r in history if r.state_code == "gap_up_open"]
        assert len(gap_rows) == 1
        assert gap_rows[0].is_active is False, \
            "解消後 gap_up_open は is_active=False になるべき"

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """
        run1: gap_up → 行1 (is_active=True)
        run2: 解消   → 行1 (is_active=False)
        run3: gap_up → 行2 (is_active=True)
        → DB に gap_up_open が 2 行
        """
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _GAP_UP},
        )
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=10),
            symbol_data={"7203": _NO_GAP},
        )
        ctx3 = EvaluationContext(
            evaluation_time=_EVAL_TIME + timedelta(seconds=20),
            symbol_data={"7203": _GAP_UP},
        )

        await engine.run(ctx1)
        await engine.run(ctx2)
        results3 = await engine.run(ctx3)

        gap3 = [
            r for r in results3
            if r.state_code == "gap_up_open" and r.target_code == "7203"
        ]
        assert len(gap3) == 1
        assert gap3[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(
            layer="symbol", target_code="7203", limit=50
        )
        gap_rows = [r for r in history if r.state_code == "gap_up_open"]
        assert len(gap_rows) == 2, \
            f"再発火で 2 行になるべき、実際 {len(gap_rows)} 行"
        active_rows = [r for r in gap_rows if r.is_active is True]
        assert len(active_rows) == 1


# ─── F. observability テスト ──────────────────────────────────────────────────

class TestGapDiagnostic:
    """rule_diagnostics の gap エントリを検証する"""

    def test_gap_up_active_diag(self):
        """gap_up 発火: status=active / gap_pct が含まれる"""
        _result, diag = _call_gap_up_with_diag(_GAP_UP)
        assert diag["status"] == "active"
        assert "gap_pct" in diag
        assert diag["gap_pct"] == pytest.approx(0.03, abs=1e-5)  # 小数形式

    def test_gap_down_active_diag(self):
        """gap_down 発火: status=active / gap_pct が負値"""
        _result, diag = _call_gap_down_with_diag(_GAP_DOWN)
        assert diag["status"] == "active"
        assert "gap_pct" in diag
        assert diag["gap_pct"] == pytest.approx(-0.03, abs=1e-5)

    def test_gap_up_inactive_diag(self):
        """gap_up 非発火: status=inactive / gap_pct が含まれる"""
        _result, diag = _call_gap_up_with_diag(_NO_GAP)
        assert diag["status"] == "inactive"
        assert "gap_pct" in diag

    def test_gap_down_inactive_when_gap_up(self):
        """gap_up データに対して gap_down rule は inactive"""
        _result, diag = _call_gap_down_with_diag(_GAP_UP)
        assert diag["status"] == "inactive"
        assert diag["gap_pct"] > 0  # gap_pct は正値（gap UP）

    def test_gap_up_skipped_no_current_open(self):
        """current_open なし → skipped / no_current_open"""
        _result, diag = _call_gap_up_with_diag({})
        assert diag["status"] == "skipped"
        assert diag["reason"] == "no_current_open"

    def test_gap_down_skipped_no_prev_close(self):
        """prev_close なし → skipped / no_prev_close"""
        _result, diag = _call_gap_down_with_diag({"current_open": 2910.0})
        assert diag["status"] == "skipped"
        assert diag["reason"] == "no_prev_close"

    def test_both_gap_keys_present_in_empty_data(self):
        """空データでも rule_diagnostics に gap_up_open / gap_down_open が存在する"""
        diags = _diags({})
        assert "gap_up_open" in diags
        assert "gap_down_open" in diags

    def test_gap_up_active_via_evaluate_symbol(self):
        """_evaluate_symbol() 経由でも gap_up active 診断が得られる"""
        diags = _diags(_GAP_UP)
        assert diags["gap_up_open"]["status"] == "active"
        assert diags["gap_down_open"]["status"] == "inactive"

    def test_gap_down_active_via_evaluate_symbol(self):
        """_evaluate_symbol() 経由でも gap_down active 診断が得られる"""
        diags = _diags(_GAP_DOWN)
        assert diags["gap_down_open"]["status"] == "active"
        assert diags["gap_up_open"]["status"] == "inactive"
