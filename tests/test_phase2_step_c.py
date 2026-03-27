"""
Phase 2 Step C 統合確認テスト

確認項目:
  1. current_price が None / <= 0 の場合 wide_spread がスキップされる
  2. best_bid / best_ask が None / <= 0 の場合 wide_spread がスキップされる
  3. 逆転スプレッド（ask < bid）は wide_spread 非発火 + WARNING ログ
  4. spread_rate の分母が current_price であることを formula テストで証明する
     — mid_price 分母なら誤発火するが current_price 分母では発火しないケースを使用
  5. 発火時 evidence に reason / current_price / spread / spread_rate が含まれる

設計検証:
  - SymbolStateEvaluator の wide_spread ブロックのみ変更（fetcher / repository / schema は不変）
  - DB INSERT なし系は StateEvaluationResult を返さない（evaluator は active state のみを返す）
  - inverted_spread は WARNING ログとして記録するがシステムを止めない
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from trade_app.services.market_state.schemas import EvaluationContext
from trade_app.services.market_state.symbol_evaluator import SymbolStateEvaluator

_UTC = timezone.utc


# ─── ヘルパー ──────────────────────────────────────────────────────────────────

def _ctx(**fields) -> EvaluationContext:
    """ticker="7203" の symbol_data を持つ EvaluationContext を生成する。"""
    return EvaluationContext(
        evaluation_time=datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC),
        symbol_data={"7203": fields},
    )


def _states(ctx: EvaluationContext) -> list[str]:
    return [r.state_code for r in SymbolStateEvaluator().evaluate(ctx)]


def _results(ctx: EvaluationContext):
    return SymbolStateEvaluator().evaluate(ctx)


# ─── 確認 1: current_price ガード ─────────────────────────────────────────────

class TestCurrentPriceGuard:
    """current_price が None / <= 0 なら bid/ask が揃っていても wide_spread はスキップ"""

    def test_no_wide_spread_when_current_price_none(self):
        """current_price=None → spread 評価を全スキップ（reason: invalid_current_price）"""
        ctx = _ctx(
            current_price=None,
            best_bid=990.0,
            best_ask=1010.0,  # spread=20/? — current_price なしでは評価不可
        )
        assert "wide_spread" not in _states(ctx)

    def test_no_wide_spread_when_current_price_zero(self):
        """current_price=0 → ゼロ除算ガード（reason: invalid_current_price）"""
        ctx = _ctx(
            current_price=0,
            best_bid=990.0,
            best_ask=1010.0,
        )
        assert "wide_spread" not in _states(ctx)

    def test_no_wide_spread_when_current_price_negative(self):
        """current_price=-1 → 無効値ガード（reason: invalid_current_price）"""
        ctx = _ctx(
            current_price=-1.0,
            best_bid=990.0,
            best_ask=1010.0,
        )
        assert "wide_spread" not in _states(ctx)


# ─── 確認 2: bid / ask ガード ─────────────────────────────────────────────────

class TestBidAskGuard:
    """best_bid / best_ask が None または <= 0 の場合 wide_spread はスキップ"""

    def test_no_wide_spread_when_bid_none(self):
        """best_bid=None → reason: no_bid — wide_spread 非発火"""
        ctx = _ctx(
            current_price=1000.0,
            best_bid=None,
            best_ask=1010.0,
        )
        assert "wide_spread" not in _states(ctx)

    def test_no_wide_spread_when_ask_none(self):
        """best_ask=None → reason: no_ask — wide_spread 非発火"""
        ctx = _ctx(
            current_price=1000.0,
            best_bid=990.0,
            best_ask=None,
        )
        assert "wide_spread" not in _states(ctx)


# ─── 確認 3: 逆転スプレッド ────────────────────────────────────────────────────

class TestInvertedSpread:
    """ask < bid のデータ異常は wide_spread 非発火 + WARNING ログ"""

    def test_no_wide_spread_when_inverted(self):
        """bid=1010, ask=990 → 逆転スプレッド（reason: inverted_spread）→ 非発火"""
        ctx = _ctx(
            current_price=1000.0,
            best_bid=1010.0,
            best_ask=990.0,
        )
        assert "wide_spread" not in _states(ctx)

    def test_inverted_spread_emits_warning(self, caplog):
        """逆転スプレッドで WARNING ログが出力される（サイレントにスキップしない）"""
        ctx = _ctx(
            current_price=1000.0,
            best_bid=1010.0,
            best_ask=990.0,
        )
        with caplog.at_level(logging.WARNING):
            _states(ctx)
        assert any("inverted spread" in record.message for record in caplog.records)


# ─── 確認 4: spread_rate の分母が current_price であることを証明 ───────────────

class TestSpreadRateFormula:
    """
    分母が current_price であることを、mid_price と current_price が乖離するケースで検証する。

    formula テスト:
        bid=997, ask=1003, current_price=3000
        spread = 6
        spread_rate (current_price) = 6 / 3000 = 0.200% < 0.3% → 非発火
        spread_rate (mid_price)     = 6 / 1000 = 0.600% >= 0.3% → 誤発火

    この差分が「分母が current_price であること」を証明する。
    """

    def test_no_fire_with_current_price_denominator(self):
        """
        mid_price 分母なら発火するが、current_price 分母では発火しない。

        bid=997, ask=1003, current_price=3000:
          spread_rate (correct)  = 6 / 3000 = 0.0020 < 0.003 → 非発火
          spread_rate (wrong)    = 6 / 1000 = 0.0060 >= 0.003 → 誤発火 (mid_price 使用時)
        """
        ctx = _ctx(
            current_price=3000.0,  # mid_price (=1000) とは大きく異なる
            best_bid=997.0,
            best_ask=1003.0,
        )
        assert "wide_spread" not in _states(ctx), (
            "wide_spread が誤発火している。"
            "分母に mid_price が使われている可能性がある。"
            "current_price を分母として spread_rate = (ask-bid) / current_price で計算すること。"
        )

    def test_fires_at_boundary_with_current_price_denominator(self):
        """
        current_price 分母でちょうど 0.3% を超える境界値で発火する。

        bid=998.5, ask=1001.5, current_price=1000:
          spread = 3.0
          spread_rate = 3.0 / 1000 = 0.003 = 0.3% → 発火（>=）
        """
        ctx = _ctx(
            current_price=1000.0,
            best_bid=998.5,
            best_ask=1001.5,
        )
        assert "wide_spread" in _states(ctx)

    def test_spread_rate_value_uses_current_price(self):
        """
        evidence["spread_rate"] が (ask - bid) / current_price で算出された値であること。

        bid=994, ask=1006, current_price=2000:
          spread = 12
          spread_rate = 12 / 2000 = 0.006
        """
        ctx = _ctx(
            current_price=2000.0,
            best_bid=994.0,
            best_ask=1006.0,
        )
        results = _results(ctx)
        r = next(r for r in results if r.state_code == "wide_spread")
        expected_rate = (1006.0 - 994.0) / 2000.0  # = 0.006
        # evidence["spread_rate"] は round(..., 6) で格納されるため abs 許容値で比較
        assert r.evidence["spread_rate"] == pytest.approx(expected_rate, abs=1e-5)
        assert r.evidence["spread_rate"] != pytest.approx(
            (1006.0 - 994.0) / ((994.0 + 1006.0) / 2),  # mid_price 分母 = 0.012
            rel=1e-6,
        ), "spread_rate が mid_price 分母の値と一致している（実装バグ）"


# ─── 確認 5: 発火時 evidence の内容 ──────────────────────────────────────────

class TestWideSpreadEvidence:
    """wide_spread 発火時 evidence に reason / current_price / spread / spread_rate が含まれる"""

    @pytest.fixture
    def fired_result(self):
        """wide_spread が発火する標準ケースの result を返す"""
        ctx = _ctx(
            current_price=3000.0,
            best_bid=2950.0,
            best_ask=3050.0,  # spread=100, spread_rate=100/3000≈3.33%
        )
        results = _results(ctx)
        return next(r for r in results if r.state_code == "wide_spread")

    def test_evidence_contains_reason_wide_spread(self, fired_result):
        """evidence["reason"] == "wide_spread"（状態が確定した理由を明示）"""
        assert fired_result.evidence["reason"] == "wide_spread"

    def test_evidence_contains_current_price(self, fired_result):
        """evidence["current_price"] に分母として使用した値が含まれる"""
        assert fired_result.evidence["current_price"] == 3000.0

    def test_evidence_spread_rate_is_current_price_based(self, fired_result):
        """
        evidence["spread_rate"] が (ask - bid) / current_price であること。

        spread=100, current_price=3000 → spread_rate ≈ 0.033333
        """
        expected = (3050.0 - 2950.0) / 3000.0
        # evidence["spread_rate"] は round(..., 6) で格納されるため abs 許容値で比較
        assert fired_result.evidence["spread_rate"] == pytest.approx(expected, abs=1e-5)
        assert fired_result.evidence["best_bid"] == 2950.0
        assert fired_result.evidence["best_ask"] == 3050.0
        assert fired_result.evidence["spread"] == pytest.approx(100.0, abs=0.001)
