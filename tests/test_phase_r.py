"""
Phase R — priority ベース通知制御テスト

確認項目:
  A. Priority 1 state の通知
     1.  wide_spread activated → 通知される
     2.  price_stale activated → 通知される
     3.  stale_bid_ask activated → 通知される（Phase Q 追加 / Phase R で priority 1 に昇格）

  B. Priority 2 state の条件付き通知
     4.  breakout_candidate score >= 0.8 → 通知される
     5.  breakout_candidate score < 0.8 → 通知されない
     6.  quote_only bid/ask 両方あり → 通知される
     7.  quote_only bid のみ → 通知されない
     8.  quote_only ask のみ → 通知されない
     9.  quote_only bid/ask 両方 None → 通知されない

  C. 通知対象外
    10. 未定義 state は通知されない
    11. continued (is_new_activation=False) は通知されない
    12. 複数 state 混在時の正しいフィルタリング

  D. payload 構造
    13. payload に priority が含まれる（priority 1）
    14. payload に priority が含まれる（priority 2）
    15. wide_spread の state 別フィールド
    16. price_stale の state 別フィールド
    17. stale_bid_ask の state 別フィールド
    18. breakout_candidate の必須フィールドのみ（追加フィールドなし）
    19. quote_only の state 別フィールド

  E. STATE_NOTIFICATION_PRIORITY 定数
    20. module レベルに存在する
    21. dict 型である
    22. 5 エントリ（wide_spread / price_stale / stale_bid_ask / breakout_candidate / quote_only）
    23. priority 1 が 3 件
    24. priority 2 が 2 件

  F. dispatch / run の継続性
    25. dispatch 例外 → 握りつぶして継続
    26. 通知失敗でも run 全体が継続する
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from trade_app.services.market_state import engine as _engine_mod
from trade_app.services.market_state.engine import (
    STATE_NOTIFICATION_PRIORITY,
    dispatch_notifications,
    extract_notification_candidates,
)
from trade_app.services.market_state.schemas import StateEvaluationResult

_UTC = timezone.utc
_EVAL_TIME = datetime(2024, 11, 6, 10, 0, 0, tzinfo=_UTC)


# ─── ヘルパー ─────────────────────────────────────────────────────────────────

def _make_result(
    state_code: str,
    score: float = 1.0,
    is_new: bool = True,
    evidence: dict[str, Any] | None = None,
) -> StateEvaluationResult:
    return StateEvaluationResult(
        layer="symbol",
        target_type="symbol",
        target_code="7203",
        state_code=state_code,
        score=score,
        confidence=1.0,
        evidence=evidence or {},
        is_new_activation=is_new,
    )


def _extract(results: list[StateEvaluationResult]) -> list[dict]:
    return extract_notification_candidates(results, _EVAL_TIME)


# ─── A. Priority 1 state の通知 ──────────────────────────────────────────────

class TestPriority1Notification:
    def test_wide_spread_notified(self):
        r = _make_result("wide_spread", evidence={"spread": 12.0, "spread_rate": 0.012, "current_price": 1000.0})
        assert len(_extract([r])) == 1

    def test_price_stale_notified(self):
        r = _make_result("price_stale", evidence={"age_sec": 120.0, "threshold_sec": 60.0})
        assert len(_extract([r])) == 1

    def test_stale_bid_ask_notified(self):
        """Phase R: stale_bid_ask は priority 1 で無条件通知"""
        r = _make_result("stale_bid_ask", evidence={
            "bid_ask_updated": "2024-11-06T09:58:00+00:00",
            "age_sec": 120.0,
            "threshold_sec": 60.0,
            "best_bid": 999.0,
            "best_ask": 1001.0,
        })
        assert len(_extract([r])) == 1


# ─── B. Priority 2 条件付き通知 ──────────────────────────────────────────────

class TestPriority2Conditions:
    def test_breakout_score_high_notified(self):
        r = _make_result("breakout_candidate", score=0.8)
        assert len(_extract([r])) == 1

    def test_breakout_score_above_threshold_notified(self):
        r = _make_result("breakout_candidate", score=0.95)
        assert len(_extract([r])) == 1

    def test_breakout_score_low_not_notified(self):
        r = _make_result("breakout_candidate", score=0.79)
        assert len(_extract([r])) == 0

    def test_breakout_score_zero_not_notified(self):
        r = _make_result("breakout_candidate", score=0.0)
        assert len(_extract([r])) == 0

    def test_quote_only_both_bid_ask_notified(self):
        r = _make_result("quote_only", evidence={"best_bid": 999.0, "best_ask": 1001.0, "current_price": None})
        assert len(_extract([r])) == 1

    def test_quote_only_bid_only_not_notified(self):
        r = _make_result("quote_only", evidence={"best_bid": 999.0, "best_ask": None})
        assert len(_extract([r])) == 0

    def test_quote_only_ask_only_not_notified(self):
        r = _make_result("quote_only", evidence={"best_bid": None, "best_ask": 1001.0})
        assert len(_extract([r])) == 0

    def test_quote_only_neither_not_notified(self):
        r = _make_result("quote_only", evidence={"best_bid": None, "best_ask": None})
        assert len(_extract([r])) == 0


# ─── C. 通知対象外 ───────────────────────────────────────────────────────────

class TestNotNotified:
    def test_undefined_state_not_notified(self):
        r = _make_result("gap_up_open")
        assert _extract([r]) == []

    def test_undefined_state_symbol_range_not_notified(self):
        r = _make_result("symbol_range")
        assert _extract([r]) == []

    def test_continued_not_notified(self):
        r = _make_result("wide_spread", is_new=False)
        assert _extract([r]) == []

    def test_continued_priority2_not_notified(self):
        r = _make_result("breakout_candidate", score=0.9, is_new=False)
        assert _extract([r]) == []

    def test_mixed_results_correct_filter(self):
        """priority 1 + continued + 未定義 + priority 2 条件不足が混在するケース"""
        results = [
            _make_result("wide_spread", is_new=True),                    # → 通知
            _make_result("price_stale", is_new=False),                   # → continued なので除外
            _make_result("gap_up_open", is_new=True),                    # → 未定義なので除外
            _make_result("breakout_candidate", score=0.5, is_new=True),  # → score 不足で除外
        ]
        candidates = _extract(results)
        assert len(candidates) == 1
        assert candidates[0]["state_code"] == "wide_spread"


# ─── D. payload 構造 ──────────────────────────────────────────────────────────

class TestPayloadStructure:
    def test_priority_in_payload_p1(self):
        r = _make_result("wide_spread")
        candidates = _extract([r])
        assert candidates[0]["priority"] == 1

    def test_priority_in_payload_p2(self):
        r = _make_result("breakout_candidate", score=0.9)
        candidates = _extract([r])
        assert candidates[0]["priority"] == 2

    def test_required_keys_present(self):
        r = _make_result("wide_spread")
        payload = _extract([r])[0]
        for key in ("ticker", "state_code", "evaluation_time", "priority", "reason", "score"):
            assert key in payload, f"'{key}' がない"

    def test_wide_spread_extra_fields(self):
        r = _make_result("wide_spread", evidence={
            "spread": 12.0,
            "spread_rate": 0.012,
            "current_price": 1000.0,
        })
        p = _extract([r])[0]
        assert p["spread"] == 12.0
        assert p["spread_rate"] == 0.012
        assert p["current_price"] == 1000.0

    def test_price_stale_extra_fields(self):
        r = _make_result("price_stale", evidence={
            "last_updated": "2024-11-06T09:58:00+00:00",
            "age_sec": 120.0,
            "threshold_sec": 60.0,
        })
        p = _extract([r])[0]
        assert p["last_updated"] == "2024-11-06T09:58:00+00:00"
        assert p["age_sec"] == 120.0
        assert p["threshold_sec"] == 60.0

    def test_stale_bid_ask_extra_fields(self):
        r = _make_result("stale_bid_ask", evidence={
            "bid_ask_updated": "2024-11-06T09:58:00+00:00",
            "age_sec": 120.0,
            "threshold_sec": 60.0,
            "best_bid": 999.0,
            "best_ask": 1001.0,
        })
        p = _extract([r])[0]
        assert p["bid_ask_updated"] == "2024-11-06T09:58:00+00:00"
        assert p["age_sec"] == 120.0
        assert p["threshold_sec"] == 60.0
        assert p["best_bid"] == 999.0
        assert p["best_ask"] == 1001.0

    def test_breakout_candidate_no_extra_fields(self):
        """breakout_candidate は score / reason が共通フィールドで提供される（追加フィールドなし）"""
        r = _make_result("breakout_candidate", score=0.9, evidence={"reason": "price_above_ma"})
        p = _extract([r])[0]
        assert p["score"] == 0.9
        assert p["reason"] == "price_above_ma"
        # wide_spread 専用フィールドが混入しないこと
        assert "spread" not in p
        assert "spread_rate" not in p

    def test_quote_only_extra_fields(self):
        r = _make_result("quote_only", evidence={
            "best_bid": 999.0,
            "best_ask": 1001.0,
            "current_price": None,
        })
        p = _extract([r])[0]
        assert p["best_bid"] == 999.0
        assert p["best_ask"] == 1001.0
        assert p["current_price"] is None

    def test_evaluation_time_in_payload(self):
        r = _make_result("wide_spread")
        p = _extract([r])[0]
        assert p["evaluation_time"] == _EVAL_TIME


# ─── E. STATE_NOTIFICATION_PRIORITY 定数 ──────────────────────────────────────

class TestStateNotificationPriority:
    def test_exists_at_module_level(self):
        assert hasattr(_engine_mod, "STATE_NOTIFICATION_PRIORITY")

    def test_is_dict(self):
        assert isinstance(STATE_NOTIFICATION_PRIORITY, dict)

    def test_has_5_entries(self):
        assert len(STATE_NOTIFICATION_PRIORITY) == 5, \
            f"expected 5, got {len(STATE_NOTIFICATION_PRIORITY)}: {list(STATE_NOTIFICATION_PRIORITY)}"

    def test_contains_expected_states(self):
        expected = {"wide_spread", "price_stale", "stale_bid_ask", "breakout_candidate", "quote_only"}
        assert set(STATE_NOTIFICATION_PRIORITY.keys()) == expected

    def test_priority1_states(self):
        p1 = {k for k, v in STATE_NOTIFICATION_PRIORITY.items() if v == 1}
        assert p1 == {"wide_spread", "price_stale", "stale_bid_ask"}

    def test_priority2_states(self):
        p2 = {k for k, v in STATE_NOTIFICATION_PRIORITY.items() if v == 2}
        assert p2 == {"breakout_candidate", "quote_only"}


# ─── F. dispatch / run の継続性 ───────────────────────────────────────────────

class TestDispatchContinuity:
    def test_dispatch_exception_swallowed(self):
        """dispatch 例外は握りつぶされる"""
        candidates = [{"state_code": "wide_spread", "ticker": "7203"}]
        with patch("trade_app.services.market_state.engine.logger") as mock_log:
            mock_log.info.side_effect = RuntimeError("log failure")
            # 例外が外に出ないこと
            dispatch_notifications(candidates)

    def test_dispatch_continues_after_failure(self):
        """1エントリが例外を起こしても次のエントリを処理する"""
        call_count = 0

        def side_effect(msg, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first failure")

        candidates = [
            {"state_code": "wide_spread"},
            {"state_code": "price_stale"},
        ]
        with patch("trade_app.services.market_state.engine.logger") as mock_log:
            mock_log.info.side_effect = side_effect
            dispatch_notifications(candidates)
        # 2件目も処理された
        assert call_count == 2

    def test_empty_candidates_no_error(self):
        dispatch_notifications([])

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_stop_run(self, tmp_path):
        """extract_notification_candidates が例外を起こしても engine.run() は継続する"""
        from unittest.mock import MagicMock

        from trade_app.services.market_state.engine import MarketStateEngine
        from trade_app.services.market_state.schemas import EvaluationContext

        # DB セッションをモック
        db_mock = AsyncMock()
        db_mock.commit = AsyncMock()

        # dummy evaluator
        dummy_evaluator = MagicMock()
        dummy_evaluator.name = "dummy"
        dummy_evaluator.evaluate.return_value = []

        engine = MarketStateEngine(db=db_mock, evaluators=[dummy_evaluator])

        ctx = EvaluationContext(
            evaluation_time=_EVAL_TIME,
            symbol_data={},
        )

        with patch(
            "trade_app.services.market_state.engine.extract_notification_candidates",
            side_effect=RuntimeError("notification error"),
        ):
            # 例外が外に出ないこと
            result = await engine.run(ctx)
        # run は [] を返して正常終了
        assert isinstance(result, list)
