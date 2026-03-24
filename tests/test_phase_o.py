"""
Phase O — 通知連携テスト

確認項目:
  1. activated のみ抽出される
  2. continued (is_new_activation=False) は抽出されない
  3. deactivated は symbol_results に含まれないので抽出されない
  4. whitelist 外 state は抽出されない
  5. payload に必須キーが含まれる（ticker / state_code / evaluation_time / reason / score）
  6. state 別追加項目が正しく入る
     - wide_spread: spread / spread_rate / current_price
     - price_stale: last_updated / age_sec / threshold_sec
     - breakout_candidate: score が上書きなく保持される
  7. dispatch_notifications の例外発生時も処理継続される
  8. NOTIFIABLE_STATE_CODES 定数が正しく定義されている

設計:
  - extract_notification_candidates / dispatch_notifications を直接呼び出す単体テスト
  - engine.run() 経由の結合テストは行わない（engine のテストは test_phase5_regression 等が担う）
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from trade_app.services.market_state.engine import (
    NOTIFIABLE_STATE_CODES,
    dispatch_notifications,
    extract_notification_candidates,
)
from trade_app.services.market_state.schemas import StateEvaluationResult

_UTC = timezone.utc
_NOW = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)


# ─── テスト用ファクトリ ────────────────────────────────────────────────────────

def _result(
    state_code: str,
    ticker: str = "7203",
    is_new_activation: bool = True,
    evidence: dict[str, Any] | None = None,
    score: float = 0.5,
) -> StateEvaluationResult:
    return StateEvaluationResult(
        layer="symbol",
        target_type="symbol",
        target_code=ticker,
        state_code=state_code,
        score=score,
        confidence=1.0,
        evidence=evidence or {},
        is_new_activation=is_new_activation,
    )


# ─── 1. activated のみ抽出される ──────────────────────────────────────────────

class TestExtractionFilter:

    def test_activated_notifiable_is_included(self):
        """is_new_activation=True かつ whitelist 内 → 抽出される"""
        results = [_result("wide_spread", is_new_activation=True)]
        candidates = extract_notification_candidates(results, _NOW)
        assert len(candidates) == 1
        assert candidates[0]["state_code"] == "wide_spread"

    def test_continued_is_excluded(self):
        """is_new_activation=False → 抽出されない（continued）"""
        results = [_result("wide_spread", is_new_activation=False)]
        candidates = extract_notification_candidates(results, _NOW)
        assert candidates == []

    def test_whitelist_outside_state_is_excluded(self):
        """whitelist 外 state_code → 抽出されない"""
        results = [_result("gap_up_open", is_new_activation=True)]
        candidates = extract_notification_candidates(results, _NOW)
        assert candidates == []

    def test_all_three_notifiable_states_are_included(self):
        """全 3 whitelist state が activated → 3件抽出"""
        results = [
            _result("wide_spread"),
            _result("price_stale"),
            _result("breakout_candidate"),
        ]
        candidates = extract_notification_candidates(results, _NOW)
        codes = {c["state_code"] for c in candidates}
        assert codes == {"wide_spread", "price_stale", "breakout_candidate"}

    def test_mixed_results_only_activated_notifiable_extracted(self):
        """activated notifiable / continued notifiable / activated non-notifiable が混在 → 1件のみ"""
        results = [
            _result("wide_spread", is_new_activation=True),       # ← 抽出
            _result("price_stale", is_new_activation=False),      # continued → 除外
            _result("gap_up_open", is_new_activation=True),       # whitelist 外 → 除外
            _result("symbol_trend_up", is_new_activation=True),   # whitelist 外 → 除外
        ]
        candidates = extract_notification_candidates(results, _NOW)
        assert len(candidates) == 1
        assert candidates[0]["state_code"] == "wide_spread"

    def test_empty_results_returns_empty(self):
        """symbol_results が空 → 空リスト"""
        candidates = extract_notification_candidates([], _NOW)
        assert candidates == []


# ─── 2. payload 必須キー ─────────────────────────────────────────────────────

class TestPayloadRequiredKeys:

    def test_required_keys_present(self):
        """payload に必須キー 5 つが含まれる"""
        results = [_result("wide_spread", ticker="7203", score=0.7)]
        candidates = extract_notification_candidates(results, _NOW)
        assert len(candidates) == 1
        c = candidates[0]
        assert "ticker" in c
        assert "state_code" in c
        assert "evaluation_time" in c
        assert "reason" in c
        assert "score" in c

    def test_ticker_value(self):
        """ticker が target_code の値になっている"""
        results = [_result("wide_spread", ticker="9984")]
        candidates = extract_notification_candidates(results, _NOW)
        assert candidates[0]["ticker"] == "9984"

    def test_evaluation_time_value(self):
        """evaluation_time が渡した datetime と一致する"""
        results = [_result("wide_spread")]
        candidates = extract_notification_candidates(results, _NOW)
        assert candidates[0]["evaluation_time"] == _NOW

    def test_score_value(self):
        """score が StateEvaluationResult.score と一致する"""
        results = [_result("wide_spread", score=0.85)]
        candidates = extract_notification_candidates(results, _NOW)
        assert candidates[0]["score"] == pytest.approx(0.85)

    def test_reason_from_evidence(self):
        """reason が evidence["reason"] の値になっている"""
        results = [_result("wide_spread", evidence={"reason": "wide_spread", "spread": 10.0})]
        candidates = extract_notification_candidates(results, _NOW)
        assert candidates[0]["reason"] == "wide_spread"

    def test_reason_none_when_missing(self):
        """evidence に reason キーがない場合は None"""
        results = [_result("wide_spread", evidence={})]
        candidates = extract_notification_candidates(results, _NOW)
        assert candidates[0]["reason"] is None


# ─── 3. state 別追加項目 ─────────────────────────────────────────────────────

class TestStateSpecificPayload:

    def test_wide_spread_extra_fields(self):
        """wide_spread: spread / spread_rate / current_price が追加される"""
        ev = {
            "reason": "wide_spread",
            "spread": 12.0,
            "spread_rate": 0.004,
            "current_price": 3000.0,
        }
        results = [_result("wide_spread", evidence=ev)]
        candidates = extract_notification_candidates(results, _NOW)
        c = candidates[0]
        assert c["spread"] == pytest.approx(12.0)
        assert c["spread_rate"] == pytest.approx(0.004)
        assert c["current_price"] == pytest.approx(3000.0)

    def test_wide_spread_missing_evidence_fields_are_none(self):
        """wide_spread で evidence にフィールドがない場合は None"""
        results = [_result("wide_spread", evidence={"reason": "wide_spread"})]
        candidates = extract_notification_candidates(results, _NOW)
        c = candidates[0]
        assert c["spread"] is None
        assert c["spread_rate"] is None
        assert c["current_price"] is None

    def test_price_stale_extra_fields(self):
        """price_stale: last_updated / age_sec / threshold_sec が追加される"""
        ev = {
            "reason": "stale_price",
            "last_updated": "2024-11-06T09:58:00+00:00",
            "age_sec": 120.0,
            "threshold_sec": 60,
        }
        results = [_result("price_stale", evidence=ev)]
        candidates = extract_notification_candidates(results, _NOW)
        c = candidates[0]
        assert c["last_updated"] == "2024-11-06T09:58:00+00:00"
        assert c["age_sec"] == pytest.approx(120.0)
        assert c["threshold_sec"] == 60

    def test_price_stale_missing_evidence_fields_are_none(self):
        """price_stale で evidence にフィールドがない場合は None"""
        results = [_result("price_stale", evidence={"reason": "stale_price"})]
        candidates = extract_notification_candidates(results, _NOW)
        c = candidates[0]
        assert c["last_updated"] is None
        assert c["age_sec"] is None
        assert c["threshold_sec"] is None

    def test_breakout_candidate_has_score(self):
        """breakout_candidate: score が正しく含まれる"""
        results = [_result("breakout_candidate", score=0.9)]
        candidates = extract_notification_candidates(results, _NOW)
        assert candidates[0]["score"] == pytest.approx(0.9)

    def test_wide_spread_does_not_have_price_stale_fields(self):
        """wide_spread payload に price_stale 固有フィールドが含まれない"""
        results = [_result("wide_spread", evidence={"reason": "wide_spread"})]
        candidates = extract_notification_candidates(results, _NOW)
        c = candidates[0]
        assert "last_updated" not in c
        assert "age_sec" not in c
        assert "threshold_sec" not in c

    def test_price_stale_does_not_have_wide_spread_fields(self):
        """price_stale payload に wide_spread 固有フィールドが含まれない"""
        results = [_result("price_stale", evidence={"reason": "stale_price"})]
        candidates = extract_notification_candidates(results, _NOW)
        c = candidates[0]
        assert "spread" not in c
        assert "spread_rate" not in c


# ─── 4. dispatch_notifications エラーハンドリング ─────────────────────────────

class TestDispatchNotifications:

    def test_dispatch_does_not_raise_on_exception(self):
        """dispatch 中に例外が発生しても呼び出し元に伝播しない"""
        candidates = [{"ticker": "7203", "state_code": "wide_spread"}]
        with patch(
            "trade_app.services.market_state.engine.logger.info",
            side_effect=RuntimeError("log error"),
        ):
            dispatch_notifications(candidates)  # 例外が出ないこと

    def test_dispatch_continues_after_exception(self):
        """1件目の例外後も2件目が処理される"""
        call_log = []

        def fake_info(msg, *args, **kwargs):
            payload = args[0] if args else {}
            if isinstance(payload, dict) and payload.get("state_code") == "wide_spread":
                raise RuntimeError("fail")
            call_log.append(payload)

        candidates = [
            {"ticker": "7203", "state_code": "wide_spread"},
            {"ticker": "7203", "state_code": "price_stale"},
        ]
        with patch("trade_app.services.market_state.engine.logger.info", side_effect=fake_info):
            dispatch_notifications(candidates)
        assert any(
            isinstance(x, dict) and x.get("state_code") == "price_stale"
            for x in call_log
        )

    def test_dispatch_empty_candidates_is_noop(self):
        """空リストを渡しても例外が出ない"""
        dispatch_notifications([])  # 例外が出ないこと


# ─── 5. NOTIFIABLE_STATE_CODES 定数確認 ─────────────────────────────────────

class TestNotifiableStateCodes:

    def test_is_frozenset(self):
        """NOTIFIABLE_STATE_CODES が frozenset であること"""
        assert isinstance(NOTIFIABLE_STATE_CODES, frozenset)

    def test_contains_wide_spread(self):
        assert "wide_spread" in NOTIFIABLE_STATE_CODES

    def test_contains_price_stale(self):
        assert "price_stale" in NOTIFIABLE_STATE_CODES

    def test_contains_breakout_candidate(self):
        assert "breakout_candidate" in NOTIFIABLE_STATE_CODES

    def test_has_exactly_three_entries(self):
        assert len(NOTIFIABLE_STATE_CODES) == 3

    def test_gap_up_not_in_whitelist(self):
        assert "gap_up_open" not in NOTIFIABLE_STATE_CODES

    def test_symbol_trend_up_not_in_whitelist(self):
        assert "symbol_trend_up" not in NOTIFIABLE_STATE_CODES
