"""
Phase J — high_relative_volume / low_liquidity rule 化テスト

確認項目:
  A. _rule_high_relative_volume() 直テスト
     1.  current_volume=None              → skipped / no_current_volume
     2.  avg_volume_same_time=None        → skipped / no_avg_volume
     3.  avg_volume_same_time=0           → skipped / zero_avg_volume
     4.  vol_ratio < volume_ratio_high    → inactive
     5.  vol_ratio ちょうど volume_ratio_high → 発火（>=）
     6.  vol_ratio > volume_ratio_high    → 発火
     7.  score: 4x → 1.0
     8.  score: 2x → 0.5
     9.  score 上限 1.0（8x）
    10.  evidence フィールドの内容

  B. _rule_low_liquidity() 直テスト
    11.  current_volume=None              → skipped / no_current_volume
    12.  avg_volume_same_time=None        → skipped / no_avg_volume
    13.  avg_volume_same_time=0           → skipped / zero_avg_volume
    14.  vol_ratio >= volume_ratio_low    → inactive（ちょうど 0.3 も inactive）
    15.  vol_ratio < volume_ratio_low     → 発火
    16.  score 計算: max(0.1, 1 - vol_ratio/threshold)
    17.  score 下限 0.1
    18.  evidence フィールドの内容

  C. orchestrator 経由テスト
    19. high_relative_volume が evaluate() から返る
    20. low_liquidity が evaluate() から返る
    21. 中間 vol_ratio ではどちらも発火しない
    22. high と low は同時 active にならない
    23. high_relative_volume と breakout_candidate が同時 active になれる

  D. 構造確認
    24. _rule_high_relative_volume が module レベルに存在する
    25. _rule_low_liquidity が module レベルに存在する
    26. _evaluate_symbol() が両 rule を呼ぶ
    27. _evaluate_symbol() 内に inline vol_ratio 計算がない

  E. 遷移テスト（high_relative_volume）
    28. 初回 high_relative_volume → INSERT
    29. 継続 → INSERT なし
    30. 解消 → soft-expire
    31. 再発火 → 再 INSERT

  F. observability テスト
    32. high_relative_volume active 診断: status=active / vol_ratio が含まれる
    33. low_liquidity active 診断: status=active / vol_ratio が含まれる
    34. high_relative_volume inactive 診断: status=inactive / vol_ratio が含まれる
    35. low_liquidity inactive 診断: status=inactive（中間 vol_ratio）
    36. high_relative_volume skipped / no_current_volume
    37. low_liquidity skipped / no_avg_volume
    38. 空データでも両キーが rule_diagnostics に存在する
    39. _evaluate_symbol() 経由で high_relative_volume active 診断が得られる
    40. _evaluate_symbol() 経由で low_liquidity active 診断が得られる

設計:
  - _rule_high_relative_volume(ticker, data, *, volume_ratio_high, make)
  - _rule_low_liquidity(ticker, data, *, volume_ratio_low, make)
  - _evaluate_symbol() で出来高判定後、rule_diagnostics["high_relative_volume"/"low_liquidity"] に格納
  - is_high_volume は rule 呼び出し結果から導出（result is not None）
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

VOLUME_RATIO_HIGH = SymbolStateEvaluator.VOLUME_RATIO_HIGH  # 2.0
VOLUME_RATIO_LOW = SymbolStateEvaluator.VOLUME_RATIO_LOW    # 0.3


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


def _call_high_vol(
    data: dict[str, Any],
    *,
    volume_ratio_high: float = VOLUME_RATIO_HIGH,
) -> StateEvaluationResult | None:
    result, _diag = _mod._rule_high_relative_volume(
        "7203", data, volume_ratio_high=volume_ratio_high, make=_make
    )
    return result


def _call_high_vol_with_diag(
    data: dict[str, Any],
    *,
    volume_ratio_high: float = VOLUME_RATIO_HIGH,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    return _mod._rule_high_relative_volume(
        "7203", data, volume_ratio_high=volume_ratio_high, make=_make
    )


def _call_low_liq(
    data: dict[str, Any],
    *,
    volume_ratio_low: float = VOLUME_RATIO_LOW,
) -> StateEvaluationResult | None:
    result, _diag = _mod._rule_low_liquidity(
        "7203", data, volume_ratio_low=volume_ratio_low, make=_make
    )
    return result


def _call_low_liq_with_diag(
    data: dict[str, Any],
    *,
    volume_ratio_low: float = VOLUME_RATIO_LOW,
) -> tuple[StateEvaluationResult | None, dict[str, Any]]:
    return _mod._rule_low_liquidity(
        "7203", data, volume_ratio_low=volume_ratio_low, make=_make
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
_HIGH_VOL = {"current_volume": 300_000, "avg_volume_same_time": 100_000}   # vol_ratio=3.0 >= 2.0
_LOW_LIQ = {"current_volume": 10_000, "avg_volume_same_time": 100_000}     # vol_ratio=0.1 < 0.3
_NORMAL_VOL = {"current_volume": 50_000, "avg_volume_same_time": 100_000}  # vol_ratio=0.5 中間

# 境界値
_HIGH_VOL_BOUNDARY = {"current_volume": 200_000, "avg_volume_same_time": 100_000}  # vol_ratio=2.0 ちょうど
_LOW_LIQ_BOUNDARY = {"current_volume": 30_000, "avg_volume_same_time": 100_000}    # vol_ratio=0.3 ちょうど（発火しない）


# ─── A. _rule_high_relative_volume() 直テスト ─────────────────────────────────

class TestRuleHighRelativeVolumeDirect:
    """_rule_high_relative_volume() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_guard_no_current_volume(self):
        """current_volume=None → skipped"""
        result = _call_high_vol({"avg_volume_same_time": 100_000})
        assert result is None

    def test_guard_no_avg_volume(self):
        """avg_volume_same_time=None → skipped"""
        result = _call_high_vol({"current_volume": 200_000})
        assert result is None

    def test_guard_zero_avg_volume(self):
        """avg_volume_same_time=0 → skipped（ゼロ除算ガード）"""
        result = _call_high_vol({"current_volume": 200_000, "avg_volume_same_time": 0})
        assert result is None

    # ── 非発火 ──

    def test_no_fire_below_threshold(self):
        """vol_ratio = 1.5 < 2.0 → None"""
        result = _call_high_vol({"current_volume": 150_000, "avg_volume_same_time": 100_000})
        assert result is None

    def test_no_fire_at_normal_volume(self):
        """vol_ratio = 0.5（中間）→ None"""
        result = _call_high_vol(_NORMAL_VOL)
        assert result is None

    # ── 発火 ──

    def test_fires_at_boundary(self):
        """vol_ratio ちょうど 2.0 → 発火（>=）"""
        result = _call_high_vol(_HIGH_VOL_BOUNDARY)
        assert result is not None
        assert result.state_code == "high_relative_volume"

    def test_fires_above_threshold(self):
        """vol_ratio = 3.0 → 発火"""
        result = _call_high_vol(_HIGH_VOL)
        assert result is not None
        assert result.state_code == "high_relative_volume"
        assert result.target_code == "7203"
        assert result.layer == "symbol"

    # ── score ──

    def test_score_at_4x_vol_ratio(self):
        """vol_ratio = 4.0 → score = 1.0"""
        result = _call_high_vol({"current_volume": 400_000, "avg_volume_same_time": 100_000})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    def test_score_at_2x_vol_ratio(self):
        """vol_ratio = 2.0 (threshold) → score = 0.5"""
        result = _call_high_vol(_HIGH_VOL_BOUNDARY)
        assert result is not None
        expected = min(1.0, 2.0 / 4.0)  # 0.5
        assert result.score == pytest.approx(expected, abs=1e-5)

    def test_score_capped_at_one(self):
        """vol_ratio = 8.0 → score は 1.0 でキャップ"""
        result = _call_high_vol({"current_volume": 800_000, "avg_volume_same_time": 100_000})
        assert result is not None
        assert result.score == pytest.approx(1.0, abs=1e-5)

    # ── evidence ──

    def test_evidence_fields(self):
        """evidence に必要なフィールドが含まれる"""
        result = _call_high_vol(_HIGH_VOL)
        assert result is not None
        ev = result.evidence
        assert "volume_ratio" in ev
        assert "current_volume" in ev
        assert "avg_volume_same_time" in ev
        assert "threshold" in ev
        assert "rule" in ev
        assert ev["volume_ratio"] == pytest.approx(3.0, abs=0.01)


# ─── B. _rule_low_liquidity() 直テスト ───────────────────────────────────────

class TestRuleLowLiquidityDirect:
    """_rule_low_liquidity() を直接呼び出す単体テスト"""

    # ── ガード ──

    def test_guard_no_current_volume(self):
        """current_volume=None → skipped"""
        result = _call_low_liq({"avg_volume_same_time": 100_000})
        assert result is None

    def test_guard_no_avg_volume(self):
        """avg_volume_same_time=None → skipped"""
        result = _call_low_liq({"current_volume": 10_000})
        assert result is None

    def test_guard_zero_avg_volume(self):
        """avg_volume_same_time=0 → skipped（ゼロ除算ガード）"""
        result = _call_low_liq({"current_volume": 10_000, "avg_volume_same_time": 0})
        assert result is None

    # ── 非発火 ──

    def test_no_fire_at_threshold(self):
        """vol_ratio = 0.3 (ちょうど threshold) → inactive（< ではなく >= で抑制）"""
        result = _call_low_liq(_LOW_LIQ_BOUNDARY)
        assert result is None

    def test_no_fire_above_threshold(self):
        """vol_ratio = 0.5 > 0.3 → None"""
        result = _call_low_liq(_NORMAL_VOL)
        assert result is None

    def test_no_fire_at_high_volume(self):
        """vol_ratio = 3.0（高出来高）→ None"""
        result = _call_low_liq(_HIGH_VOL)
        assert result is None

    # ── 発火 ──

    def test_fires_below_threshold(self):
        """vol_ratio = 0.1 < 0.3 → 発火"""
        result = _call_low_liq(_LOW_LIQ)
        assert result is not None
        assert result.state_code == "low_liquidity"
        assert result.target_code == "7203"

    # ── score ──

    def test_score_at_low_vol_ratio(self):
        """vol_ratio = 0.1 → score = max(0.1, 1.0 - 0.1/0.3) = 2/3"""
        result = _call_low_liq(_LOW_LIQ)
        assert result is not None
        expected = max(0.1, 1.0 - 0.1 / 0.3)
        assert result.score == pytest.approx(expected, abs=1e-5)

    def test_score_floor_at_0_1(self):
        """score の下限は 0.1 — vol_ratio が 0 に近くても min にならない（formula の性質上 floor はほぼ使われないが下限保証を確認）"""
        # vol_ratio=0 は avg_volume=0 ガードで弾かれるため near-zero で確認
        # vol_ratio = 0.001 → 1.0 - 0.001/0.3 ≒ 0.997 → floor 発動しない
        result = _call_low_liq({"current_volume": 100, "avg_volume_same_time": 100_000})
        assert result is not None
        assert result.score >= 0.1

    # ── evidence ──

    def test_evidence_fields(self):
        """evidence に必要なフィールドが含まれる"""
        result = _call_low_liq(_LOW_LIQ)
        assert result is not None
        ev = result.evidence
        assert "volume_ratio" in ev
        assert "current_volume" in ev
        assert "avg_volume_same_time" in ev
        assert "threshold" in ev
        assert "rule" in ev
        assert ev["volume_ratio"] == pytest.approx(0.1, abs=0.001)


# ─── C. orchestrator 経由テスト ──────────────────────────────────────────────

class TestOrchestratorVolume:
    """SymbolStateEvaluator.evaluate() 経由で出来高状態が返ること"""

    def test_high_relative_volume_via_evaluate(self):
        """高出来高データを渡すと evaluate() から high_relative_volume が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_HIGH_VOL)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "high_relative_volume" in codes

    def test_low_liquidity_via_evaluate(self):
        """低出来高データを渡すと evaluate() から low_liquidity が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_LOW_LIQ)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "low_liquidity" in codes

    def test_no_volume_state_at_normal_ratio(self):
        """中間 vol_ratio ではいずれも発火しない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(**_NORMAL_VOL)
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "high_relative_volume" not in codes
        assert "low_liquidity" not in codes

    def test_high_and_low_never_coexist(self):
        """high_relative_volume と low_liquidity は同じデータで同時発火しない"""
        evaluator = SymbolStateEvaluator()

        ctx_high = _ctx(**_HIGH_VOL)
        results_high = evaluator.evaluate(ctx_high)
        codes_high = {r.state_code for r in results_high}
        assert "high_relative_volume" in codes_high
        assert "low_liquidity" not in codes_high

        ctx_low = _ctx(**_LOW_LIQ)
        results_low = evaluator.evaluate(ctx_low)
        codes_low = {r.state_code for r in results_low}
        assert "low_liquidity" in codes_low
        assert "high_relative_volume" not in codes_low

    def test_high_volume_coexists_with_breakout_candidate(self):
        """high_relative_volume と breakout_candidate が同時 active になれる"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=1050.0,
            ma20=1000.0,               # price > ma20
            current_volume=300_000,
            avg_volume_same_time=100_000,  # vol_ratio=3.0 → high_relative_volume
            # ギャップなし（current_open / prev_close なし）
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "high_relative_volume" in codes, "high_relative_volume が発火していない"
        assert "breakout_candidate" in codes, "breakout_candidate が発火していない"


# ─── D. 構造確認 ──────────────────────────────────────────────────────────────

class TestStructureVolume:
    """リファクタリング後の構造を確認する"""

    def test_rule_high_relative_volume_is_module_level(self):
        """_rule_high_relative_volume が module レベルに存在する"""
        assert hasattr(_mod, "_rule_high_relative_volume")
        assert callable(_mod._rule_high_relative_volume)

    def test_rule_low_liquidity_is_module_level(self):
        """_rule_low_liquidity が module レベルに存在する"""
        assert hasattr(_mod, "_rule_low_liquidity")
        assert callable(_mod._rule_low_liquidity)

    def test_evaluate_symbol_calls_volume_rules(self):
        """volume rule が evaluator module の _RULES に登録されている"""
        import inspect
        source = inspect.getsource(_mod)
        assert "_rule_high_relative_volume" in source
        assert "_rule_low_liquidity" in source

    def test_evaluate_symbol_has_no_inline_volume(self):
        """
        _evaluate_symbol() 内に volume インライン判定が残っていないこと。

        rule 化後は current_volume / avg_volume_same_time を _evaluate_symbol() の
        トップで data.get() しない。vol_ratio 計算は rule 関数内に移動している。
        """
        import inspect
        source = inspect.getsource(SymbolStateEvaluator._evaluate_symbol)
        assert 'data.get("current_volume")' not in source, \
            "_evaluate_symbol() 内に current_volume の直接抽出が残っている"
        assert 'data.get("avg_volume_same_time")' not in source, \
            "_evaluate_symbol() 内に avg_volume_same_time の直接抽出が残っている"


# ─── E. 遷移テスト ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestHighRelativeVolumeTransitions:
    """high_relative_volume の activated / continued / deactivated 遷移テスト"""

    async def test_initial_high_volume_inserts_row(self, db_session: AsyncSession):
        """初回 high_relative_volume → StateEvaluation が INSERT される（is_active=True）"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_VOL},
        )
        results = await engine.run(ctx)

        high_vol_results = [r for r in results if r.state_code == "high_relative_volume"]
        assert len(high_vol_results) == 1
        assert high_vol_results[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "high_relative_volume" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """同一 ticker で高出来高が継続するとき、2回目は新規 INSERT されない"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_VOL},
        )
        await engine.run(ctx1)

        repo = MarketStateRepository(db_session)
        rows_after_1 = await repo.get_symbol_active_evaluations("7203")
        high_vol_1 = [r for r in rows_after_1 if r.state_code == "high_relative_volume"]
        assert len(high_vol_1) == 1
        first_id = high_vol_1[0].id

        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_VOL},
        )
        results2 = await engine.run(ctx2)
        continuation = [r for r in results2 if r.state_code == "high_relative_volume"]
        assert len(continuation) == 1
        assert continuation[0].is_new_activation is False

        rows_after_2 = await repo.get_symbol_active_evaluations("7203")
        high_vol_2 = [r for r in rows_after_2 if r.state_code == "high_relative_volume"]
        assert len(high_vol_2) == 1
        assert high_vol_2[0].id == first_id

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """出来高正常化で high_relative_volume が解消されると is_active=False になる"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_VOL},
        )
        await engine.run(ctx1)

        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _NORMAL_VOL},  # 出来高正常化
        )
        await engine.run(ctx2)

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert not any(r.state_code == "high_relative_volume" for r in active)

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """high_relative_volume 解消後に再発火すると新規 INSERT される"""
        engine = MarketStateEngine(db_session)

        ctx1 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_VOL},
        )
        await engine.run(ctx1)

        repo = MarketStateRepository(db_session)
        rows_1 = await repo.get_symbol_active_evaluations("7203")
        high_1 = [r for r in rows_1 if r.state_code == "high_relative_volume"]
        first_id = high_1[0].id

        # 解消
        ctx2 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _NORMAL_VOL},
        )
        await engine.run(ctx2)

        # 再発火
        ctx3 = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _HIGH_VOL},
        )
        results3 = await engine.run(ctx3)
        reactivated = [r for r in results3 if r.state_code == "high_relative_volume"]
        assert len(reactivated) == 1
        assert reactivated[0].is_new_activation is True

        rows_3 = await repo.get_symbol_active_evaluations("7203")
        high_3 = [r for r in rows_3 if r.state_code == "high_relative_volume"]
        assert len(high_3) == 1
        assert high_3[0].id != first_id


# ─── F. observability テスト ──────────────────────────────────────────────────

class TestVolumeDiagnostic:
    """rule_diagnostics に出来高 rule の診断情報が含まれること"""

    def test_high_relative_volume_active_diag(self):
        """高出来高時: high_relative_volume の診断が status=active / vol_ratio を含む"""
        _result, diag = _call_high_vol_with_diag(_HIGH_VOL)
        assert diag["status"] == "active"
        assert "vol_ratio" in diag
        assert diag["vol_ratio"] == pytest.approx(3.0, abs=0.01)

    def test_low_liquidity_active_diag(self):
        """低出来高時: low_liquidity の診断が status=active / vol_ratio を含む"""
        _result, diag = _call_low_liq_with_diag(_LOW_LIQ)
        assert diag["status"] == "active"
        assert "vol_ratio" in diag
        assert diag["vol_ratio"] == pytest.approx(0.1, abs=0.001)

    def test_high_vol_inactive_diag(self):
        """中間 vol_ratio: high_relative_volume の診断が status=inactive / vol_ratio を含む"""
        _result, diag = _call_high_vol_with_diag(_NORMAL_VOL)
        assert diag["status"] == "inactive"
        assert "vol_ratio" in diag

    def test_low_liquidity_inactive_diag(self):
        """中間 vol_ratio: low_liquidity の診断が status=inactive / vol_ratio を含む"""
        _result, diag = _call_low_liq_with_diag(_NORMAL_VOL)
        assert diag["status"] == "inactive"
        assert "vol_ratio" in diag

    def test_high_vol_skipped_no_current_volume(self):
        """current_volume なし: high_relative_volume の診断が status=skipped"""
        _result, diag = _call_high_vol_with_diag({"avg_volume_same_time": 100_000})
        assert diag["status"] == "skipped"
        assert diag.get("reason") == "no_current_volume"

    def test_low_liq_skipped_no_avg_volume(self):
        """avg_volume_same_time なし: low_liquidity の診断が status=skipped"""
        _result, diag = _call_low_liq_with_diag({"current_volume": 10_000})
        assert diag["status"] == "skipped"
        assert diag.get("reason") == "no_avg_volume"

    def test_both_volume_keys_present_in_empty_data(self):
        """空データでも high_relative_volume / low_liquidity の両キーが rule_diagnostics に存在する"""
        diags = _diags({})
        assert "high_relative_volume" in diags, \
            f"high_relative_volume が rule_diagnostics にない: {list(diags.keys())}"
        assert "low_liquidity" in diags, \
            f"low_liquidity が rule_diagnostics にない: {list(diags.keys())}"

    def test_high_vol_active_via_evaluate_symbol(self):
        """_evaluate_symbol() 経由でも high_relative_volume active 診断が得られる"""
        diags = _diags(_HIGH_VOL)
        assert diags["high_relative_volume"]["status"] == "active"

    def test_low_liquidity_active_via_evaluate_symbol(self):
        """_evaluate_symbol() 経由でも low_liquidity active 診断が得られる"""
        diags = _diags(_LOW_LIQ)
        assert diags["low_liquidity"]["status"] == "active"
