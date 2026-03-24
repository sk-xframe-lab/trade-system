"""
Phase Q — stale_bid_ask rule 追加テスト

確認項目:
  A. _rule_stale_bid_ask() 直テスト
     1.  bid あり / ask なし / bid_ask_updated=None → active, reason=missing_bid_ask_timestamp
     2.  bid なし / ask あり / bid_ask_updated=None → active
     3.  bid/ask あり / bid_ask_updated が 60秒以上前 → active, reason=stale_bid_ask
     4.  bid/ask あり / bid_ask_updated が 60秒未満 → inactive, reason=fresh_bid_ask
     5.  bid/ask 両方なし → inactive, reason=no_quotes
     6.  境界値: age_sec = ちょうど 60秒 → active
     7.  "bid_ask_updated" キーが存在しない → skipped（gate 発動）
     8.  score = 1.0 固定
     9.  evidence 必須キーが含まれる（stale_bid_ask 時 age_sec も含む）
    10.  active / inactive の diag 内容確認

  B. orchestrator テスト（_evaluate_symbol() 経由）
    11. stale_bid_ask が返る（bid_ask_updated=None）
    12. price_stale / wide_spread との共存
    13. bid/ask なし → stale_bid_ask が返らない
    14. bid_ask_updated キーなし → stale_bid_ask が返らない（gate）

  C. 構造テスト
    15. _rule_stale_bid_ask が module レベルに存在する
    16. stale_bid_ask が _RULE_REGISTRY に含まれる
    17. stale_bid_ask が _RULES に含まれる
    18. _RULES に 14 エントリある

  D. 遷移テスト（engine.run() 経由）
    19. 初回 active → StateEvaluation INSERT
    20. 継続 active → INSERT なし
    21. 非 active 化 → is_active=False
    22. 再 active 化 → 再 INSERT（DB に 2 行）

  E. observability テスト
    23. active (missing timestamp) diagnostics
    24. active (stale) diagnostics
    25. inactive (fresh) diagnostics
    26. inactive (no_quotes) diagnostics
    27. skipped (gate) diagnostics
    28. stale_bid_ask キーが常に rule_diagnostics に存在する

  F. fetcher テスト
    29. fetch 結果に bid_ask_updated が含まれる
    30. bid_ask_updated は datetime オブジェクト（UTC）

  G. notification テスト
    31. stale_bid_ask が NOTIFIABLE_STATE_CODES に含まれない
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.brokers.base import MarketData
from trade_app.services.market_state import symbol_evaluator as _mod
from trade_app.services.market_state.engine import MarketStateEngine, NOTIFIABLE_STATE_CODES
from trade_app.services.market_state.repository import MarketStateRepository
from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult
from trade_app.services.market_state.symbol_data_fetcher import SymbolDataFetcher
from trade_app.services.market_state.symbol_evaluator import (
    SymbolStateEvaluator,
    _RULES,
    _RULE_REGISTRY,
)

_UTC = timezone.utc
_EVAL_TIME = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)
_THRESHOLD = SymbolStateEvaluator.BID_ASK_STALE_THRESHOLD_SEC  # 60.0


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
    """_rule_stale_bid_ask() を呼び出し (result, diag) を返す。"""
    return _mod._rule_stale_bid_ask(
        "7203", data,
        evaluation_time=_EVAL_TIME,
        threshold_sec=_THRESHOLD,
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


# 共通テストデータ
_STALE_TIME = _EVAL_TIME - timedelta(seconds=120)   # 2分前 → stale
_FRESH_TIME = _EVAL_TIME - timedelta(seconds=30)    # 30秒前 → fresh
_BOUNDARY_TIME = _EVAL_TIME - timedelta(seconds=60) # ちょうど 60秒 → active (>=)


# ─── A. _rule_stale_bid_ask() 直テスト ───────────────────────────────────────

class TestRuleStaleBidAskDirect:

    # ── active ──

    def test_active_bid_only_no_timestamp(self):
        """bid あり / ask なし / bid_ask_updated=None → active, reason=missing_bid_ask_timestamp"""
        result, diag = _call_rule({
            "best_bid": 990.0, "best_ask": None,
            "bid_ask_updated": None,
        })
        assert result is not None
        assert result.state_code == "stale_bid_ask"
        assert result.evidence["reason"] == "missing_bid_ask_timestamp"
        assert diag["status"] == "active"
        assert diag["reason"] == "missing_bid_ask_timestamp"

    def test_active_ask_only_no_timestamp(self):
        """bid なし / ask あり / bid_ask_updated=None → active"""
        result, diag = _call_rule({
            "best_bid": None, "best_ask": 1010.0,
            "bid_ask_updated": None,
        })
        assert result is not None
        assert result.state_code == "stale_bid_ask"
        assert diag["status"] == "active"

    def test_active_stale_timestamp(self):
        """bid/ask あり / bid_ask_updated が 60秒以上前 → active, reason=stale_bid_ask"""
        result, diag = _call_rule({
            "best_bid": 990.0, "best_ask": 1010.0,
            "bid_ask_updated": _STALE_TIME,
        })
        assert result is not None
        assert result.state_code == "stale_bid_ask"
        assert result.evidence["reason"] == "stale_bid_ask"
        assert "age_sec" in result.evidence
        assert diag["status"] == "active"
        assert diag["reason"] == "stale_bid_ask"
        assert "age_sec" in diag

    def test_active_boundary_exactly_60sec(self):
        """境界値: age_sec = ちょうど 60秒 → active（>=）"""
        result, _ = _call_rule({
            "best_bid": 990.0, "best_ask": 1010.0,
            "bid_ask_updated": _BOUNDARY_TIME,
        })
        assert result is not None
        assert result.state_code == "stale_bid_ask"

    # ── inactive ──

    def test_inactive_fresh_timestamp(self):
        """bid/ask あり / bid_ask_updated が 60秒未満 → inactive, reason=fresh_bid_ask"""
        result, diag = _call_rule({
            "best_bid": 990.0, "best_ask": 1010.0,
            "bid_ask_updated": _FRESH_TIME,
        })
        assert result is None
        assert diag["status"] == "inactive"
        assert diag["reason"] == "fresh_bid_ask"
        assert "age_sec" in diag

    def test_inactive_no_quotes(self):
        """bid/ask 両方なし → inactive, reason=no_quotes"""
        result, diag = _call_rule({
            "best_bid": None, "best_ask": None,
            "bid_ask_updated": _STALE_TIME,
        })
        assert result is None
        assert diag["status"] == "inactive"
        assert diag["reason"] == "no_quotes"

    def test_skipped_no_key(self):
        """`bid_ask_updated` キーが data にない → skipped（gate 発動）"""
        result, diag = _call_rule({"best_bid": 990.0, "best_ask": 1010.0})
        assert result is None
        assert diag["status"] == "skipped"
        assert diag["reason"] == "no_bid_ask_updated_key"

    # ── score / evidence ──

    def test_score_is_1_0(self):
        """score は 1.0 固定"""
        result, _ = _call_rule({
            "best_bid": 990.0, "best_ask": 1010.0,
            "bid_ask_updated": _STALE_TIME,
        })
        assert result is not None
        assert result.score == pytest.approx(1.0)

    def test_evidence_required_fields_missing_timestamp(self):
        """missing_bid_ask_timestamp 時の evidence 必須キー"""
        result, _ = _call_rule({
            "best_bid": 990.0, "best_ask": 1010.0,
            "bid_ask_updated": None,
        })
        assert result is not None
        ev = result.evidence
        assert ev["reason"] == "missing_bid_ask_timestamp"
        assert ev["best_bid"] == pytest.approx(990.0)
        assert ev["best_ask"] == pytest.approx(1010.0)
        assert ev["bid_ask_updated"] is None
        assert "threshold_sec" in ev

    def test_evidence_required_fields_stale(self):
        """stale_bid_ask 時の evidence に age_sec が含まれる"""
        result, _ = _call_rule({
            "best_bid": 990.0, "best_ask": 1010.0,
            "bid_ask_updated": _STALE_TIME,
        })
        assert result is not None
        ev = result.evidence
        assert ev["reason"] == "stale_bid_ask"
        assert "age_sec" in ev
        assert ev["age_sec"] == pytest.approx(120.0, abs=0.1)
        assert "bid_ask_updated" in ev
        assert "threshold_sec" in ev


# ─── B. orchestrator テスト ───────────────────────────────────────────────────

class TestOrchestratorStaleBidAsk:

    def test_stale_bid_ask_fires_via_evaluate(self):
        """_evaluate_symbol() 経由で stale_bid_ask が返る"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            best_bid=990.0, best_ask=1010.0,
            bid_ask_updated=None,
        )
        results = evaluator.evaluate(ctx)
        assert any(r.state_code == "stale_bid_ask" for r in results)

    def test_coexists_with_wide_spread(self):
        """stale_bid_ask と wide_spread が共存できる（current_price あり・spread 大・bid_ask_updated stale）"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            current_price=1000.0,
            best_bid=985.0, best_ask=1015.0,  # spread=3% >= 0.3%
            bid_ask_updated=_STALE_TIME,
        )
        results = evaluator.evaluate(ctx)
        codes = {r.state_code for r in results}
        assert "wide_spread" in codes
        assert "stale_bid_ask" in codes

    def test_no_stale_bid_ask_without_quotes(self):
        """bid/ask なし → stale_bid_ask が返らない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            best_bid=None, best_ask=None,
            bid_ask_updated=_STALE_TIME,
        )
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "stale_bid_ask" for r in results)

    def test_gate_fires_without_bid_ask_updated_key(self):
        """bid_ask_updated キーなし → stale_bid_ask が返らない（gate）"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            best_bid=990.0, best_ask=1010.0,
            # bid_ask_updated キーなし
        )
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "stale_bid_ask" for r in results)

    def test_inactive_with_fresh_timestamp(self):
        """fresh な bid_ask_updated → stale_bid_ask が返らない"""
        evaluator = SymbolStateEvaluator()
        ctx = _ctx(
            best_bid=990.0, best_ask=1010.0,
            bid_ask_updated=_FRESH_TIME,
        )
        results = evaluator.evaluate(ctx)
        assert not any(r.state_code == "stale_bid_ask" for r in results)


# ─── C. 構造テスト ────────────────────────────────────────────────────────────

class TestStructureStaleBidAsk:

    def test_rule_function_exists_at_module_level(self):
        """_rule_stale_bid_ask が module レベルに存在する"""
        assert hasattr(_mod, "_rule_stale_bid_ask"), "_rule_stale_bid_ask が module にない"
        assert callable(_mod._rule_stale_bid_ask)

    def test_stale_bid_ask_in_rule_registry(self):
        """stale_bid_ask が _RULE_REGISTRY に含まれる"""
        assert "stale_bid_ask" in _RULE_REGISTRY, f"_RULE_REGISTRY: {_RULE_REGISTRY}"

    def test_stale_bid_ask_in_rules_list(self):
        """stale_bid_ask が _RULES リストに含まれる"""
        codes = [code for code, _ in _RULES]
        assert "stale_bid_ask" in codes

    def test_rules_count_is_14(self):
        """_RULES に 14 エントリある"""
        assert len(_RULES) == 14, f"expected 14, got {len(_RULES)}"


# ─── D. 遷移テスト ───────────────────────────────────────────────────────────

_ACTIVE = {"best_bid": 990.0, "best_ask": 1010.0, "bid_ask_updated": None}
_INACTIVE = {"best_bid": None, "best_ask": None, "bid_ask_updated": None}


@pytest.mark.asyncio
class TestStaleBidAskTransitions:

    async def test_initial_activation_inserts_row(self, db_session: AsyncSession):
        """初回 active → StateEvaluation が INSERT される"""
        engine = MarketStateEngine(db_session)
        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={"7203": _ACTIVE},
        )
        results = await engine.run(ctx)

        sba = [r for r in results if r.state_code == "stale_bid_ask"]
        assert len(sba) == 1
        assert sba[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        active = await repo.get_symbol_active_evaluations("7203")
        assert any(r.state_code == "stale_bid_ask" for r in active)

    async def test_continuation_does_not_insert_new_row(self, db_session: AsyncSession):
        """run1: active → INSERT / run2: 継続 → INSERT なし"""
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

        sba2 = [r for r in results2 if r.state_code == "stale_bid_ask" and r.target_code == "7203"]
        assert len(sba2) == 1
        assert sba2[0].is_new_activation is False

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        sba_rows = [r for r in history if r.state_code == "stale_bid_ask"]
        assert len(sba_rows) == 1, f"継続で INSERT が発生。期待1行、実際{len(sba_rows)}行"

    async def test_deactivation_sets_is_active_false(self, db_session: AsyncSession):
        """run1: active → is_active=True / run2: no quotes → is_active=False"""
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
        sba_rows = [r for r in history if r.state_code == "stale_bid_ask"]
        assert len(sba_rows) == 1
        assert sba_rows[0].is_active is False

    async def test_reactivation_inserts_new_row(self, db_session: AsyncSession):
        """run1: active / run2: inactive / run3: active → DB に 2 行"""
        engine = MarketStateEngine(db_session)
        ctx1 = EvaluationContext(evaluation_time=_EVAL_TIME, symbol_data={"7203": _ACTIVE})
        ctx2 = EvaluationContext(evaluation_time=_EVAL_TIME + timedelta(seconds=10), symbol_data={"7203": _INACTIVE})
        ctx3 = EvaluationContext(evaluation_time=_EVAL_TIME + timedelta(seconds=20), symbol_data={"7203": _ACTIVE})

        await engine.run(ctx1)
        await engine.run(ctx2)
        results3 = await engine.run(ctx3)

        sba3 = [r for r in results3 if r.state_code == "stale_bid_ask" and r.target_code == "7203"]
        assert len(sba3) == 1
        assert sba3[0].is_new_activation is True

        repo = MarketStateRepository(db_session)
        history = await repo.get_evaluation_history(layer="symbol", target_code="7203", limit=50)
        sba_rows = [r for r in history if r.state_code == "stale_bid_ask"]
        assert len(sba_rows) == 2
        assert sum(1 for r in sba_rows if r.is_active) == 1


# ─── E. observability テスト ─────────────────────────────────────────────────

class TestStaleBidAskDiagnostic:

    def test_active_missing_timestamp_diagnostic(self):
        """bid_ask_updated=None → status=active, reason=missing_bid_ask_timestamp"""
        diags = _diags({"best_bid": 990.0, "best_ask": 1010.0, "bid_ask_updated": None})
        d = diags["stale_bid_ask"]
        assert d["status"] == "active"
        assert d["reason"] == "missing_bid_ask_timestamp"

    def test_active_stale_diagnostic(self):
        """stale timestamp → status=active, reason=stale_bid_ask, age_sec"""
        diags = _diags({"best_bid": 990.0, "best_ask": 1010.0, "bid_ask_updated": _STALE_TIME})
        d = diags["stale_bid_ask"]
        assert d["status"] == "active"
        assert d["reason"] == "stale_bid_ask"
        assert "age_sec" in d

    def test_inactive_fresh_diagnostic(self):
        """fresh timestamp → status=inactive, reason=fresh_bid_ask"""
        diags = _diags({"best_bid": 990.0, "best_ask": 1010.0, "bid_ask_updated": _FRESH_TIME})
        d = diags["stale_bid_ask"]
        assert d["status"] == "inactive"
        assert d["reason"] == "fresh_bid_ask"

    def test_inactive_no_quotes_diagnostic(self):
        """bid/ask なし → status=inactive, reason=no_quotes"""
        diags = _diags({"best_bid": None, "best_ask": None, "bid_ask_updated": _STALE_TIME})
        d = diags["stale_bid_ask"]
        assert d["status"] == "inactive"
        assert d["reason"] == "no_quotes"

    def test_skipped_gate_diagnostic(self):
        """bid_ask_updated キーなし → status=skipped"""
        diags = _diags({"best_bid": 990.0, "best_ask": 1010.0})
        d = diags["stale_bid_ask"]
        assert d["status"] == "skipped"
        assert d["reason"] == "no_bid_ask_updated_key"

    def test_key_always_present_in_diagnostics(self):
        """symbol_data に何もなくても stale_bid_ask キーが存在する"""
        diags = _diags({})
        assert "stale_bid_ask" in diags


# ─── F. fetcher テスト ────────────────────────────────────────────────────────

def _make_broker(data) -> AsyncMock:
    broker = AsyncMock()
    async def _get_data(ticker):
        val = data.get(ticker)
        if isinstance(val, Exception):
            raise val
        return val
    broker.get_market_data.side_effect = _get_data
    return broker


def _md(price, bid=None, ask=None):
    return MarketData(current_price=price, best_bid=bid, best_ask=ask)


@pytest.mark.asyncio
class TestFetcherBidAskUpdated:

    async def test_bid_ask_updated_is_present(self):
        """fetch 結果に bid_ask_updated が含まれる"""
        broker = _make_broker({"7203": _md(3400.0, bid=3390.0, ask=3410.0)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert "bid_ask_updated" in result["7203"]

    async def test_bid_ask_updated_is_datetime(self):
        """bid_ask_updated は datetime オブジェクト（UTC）"""
        broker = _make_broker({"7203": _md(3400.0, bid=3390.0, ask=3410.0)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        bau = result["7203"]["bid_ask_updated"]
        assert isinstance(bau, datetime)
        assert bau.tzinfo is not None

    async def test_bid_ask_updated_when_quotes_none(self):
        """bid/ask が None でも bid_ask_updated は設定される"""
        broker = _make_broker({"7203": _md(1000.0, bid=None, ask=None)})
        fetcher = SymbolDataFetcher(broker)
        result = await fetcher.fetch(["7203"])
        assert isinstance(result["7203"]["bid_ask_updated"], datetime)


# ─── G. notification テスト ───────────────────────────────────────────────────

class TestStaleBidAskNotification:

    def test_not_in_notifiable_state_codes(self):
        """stale_bid_ask は NOTIFIABLE_STATE_CODES に含まれない"""
        assert "stale_bid_ask" not in NOTIFIABLE_STATE_CODES
