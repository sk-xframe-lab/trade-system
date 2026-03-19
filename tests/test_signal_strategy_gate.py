"""
SignalStrategyGate Phase 8 テスト

カバー内容:
  - SignalStrategyGate.check(): pass / reject パターン全種
    - global decision missing → reject
    - symbol decision missing → reject
    - global decision stale → reject
    - symbol decision stale → reject
    - global entry_allowed=False → reject
    - symbol entry_allowed=False → reject
    - 両方 allowed → pass
    - size_ratio は min(all decisions) を使用
    - size_ratio <= 0 → reject
    - direction 不一致で除外 → missing 扱い
    - signal_type="exit" → bypass（signal_strategy_decisions に記録なし）
  - _save_decision(): signal_strategy_decisions に INSERT されること
  - pipeline 統合: StrategyGateRejectedError → signal.status=REJECTED
  - API: GET /api/signals/{signal_id}/strategy-decision
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.current_strategy_decision import CurrentStrategyDecision
from trade_app.models.enums import SignalStatus
from trade_app.models.signal import TradeSignal
from trade_app.models.signal_strategy_decision import SignalStrategyDecision
from trade_app.services.signal_strategy_gate import (
    SignalStrategyGate,
    StrategyGateRejectedError,
    _is_direction_compatible,
    _is_stale,
    _signal_direction,
)

_UTC = timezone.utc
_NOW = datetime(2026, 3, 16, 9, 30, 0, tzinfo=_UTC)

# ─── ヘルパー ──────────────────────────────────────────────────────────────────


def _make_signal(
    ticker: str = "7203",
    side: str = "buy",
    signal_type: str = "entry",
) -> TradeSignal:
    now = datetime.now(_UTC)
    return TradeSignal(
        id=str(uuid.uuid4()),
        ticker=ticker,
        signal_type=signal_type,
        order_type="limit",
        side=side,
        quantity=100,
        limit_price=2850.0,
        status=SignalStatus.RECEIVED.value,
        idempotency_key=str(uuid.uuid4()),
        source_system="test",
        generated_at=now,
        received_at=now,
    )


def _make_decision(
    ticker: str | None = None,
    entry_allowed: bool = True,
    size_ratio: float = 1.0,
    direction: str = "both",
    evaluation_time: datetime | None = None,
    strategy_code: str = "strat_a",
) -> CurrentStrategyDecision:
    return CurrentStrategyDecision(
        id=str(uuid.uuid4()),
        strategy_id=str(uuid.uuid4()),
        strategy_code=strategy_code,
        ticker=ticker,
        is_active=entry_allowed,
        entry_allowed=entry_allowed,
        size_ratio=size_ratio,
        blocking_reasons_json=[] if entry_allowed else ["test_block"],
        matched_required_states_json=[],
        missing_required_states_json=[],
        matched_forbidden_states_json=[],
        evidence_json={"direction": direction},
        evaluation_time=evaluation_time or _NOW,
        updated_at=_NOW,
    )


async def _insert_decision(db: AsyncSession, decision: CurrentStrategyDecision) -> None:
    db.add(decision)
    await db.flush()


# ─── 純関数テスト ──────────────────────────────────────────────────────────────


class TestHelperFunctions:
    def test_signal_direction_buy_is_long(self):
        signal = _make_signal(side="buy")
        assert _signal_direction(signal) == "long"

    def test_signal_direction_sell_is_short(self):
        signal = _make_signal(side="sell")
        assert _signal_direction(signal) == "short"

    def test_is_direction_compatible_both_always_matches(self):
        dec = _make_decision(direction="both")
        assert _is_direction_compatible(dec, "long") is True
        assert _is_direction_compatible(dec, "short") is True

    def test_is_direction_compatible_long_matches_long_only(self):
        dec = _make_decision(direction="long")
        assert _is_direction_compatible(dec, "long") is True
        assert _is_direction_compatible(dec, "short") is False

    def test_is_direction_compatible_short_matches_short_only(self):
        dec = _make_decision(direction="short")
        assert _is_direction_compatible(dec, "short") is True
        assert _is_direction_compatible(dec, "long") is False

    def test_is_direction_compatible_missing_direction_defaults_to_both(self):
        dec = _make_decision()
        dec.evidence_json = {}  # direction キーなし → "both" として扱う
        assert _is_direction_compatible(dec, "long") is True

    def test_is_stale_fresh(self):
        dec = _make_decision(evaluation_time=_NOW - timedelta(seconds=60))
        assert _is_stale(dec, _NOW, max_age_sec=180) is False

    def test_is_stale_exactly_at_boundary_not_stale(self):
        dec = _make_decision(evaluation_time=_NOW - timedelta(seconds=180))
        assert _is_stale(dec, _NOW, max_age_sec=180) is False

    def test_is_stale_over_boundary(self):
        dec = _make_decision(evaluation_time=_NOW - timedelta(seconds=181))
        assert _is_stale(dec, _NOW, max_age_sec=180) is True

    def test_is_stale_naive_datetime_treated_as_utc(self):
        naive_time = _NOW.replace(tzinfo=None) - timedelta(seconds=200)
        dec = _make_decision(evaluation_time=naive_time)
        assert _is_stale(dec, _NOW, max_age_sec=180) is True


# ─── SignalStrategyGate.check() テスト ────────────────────────────────────────


class TestSignalStrategyGateCheck:

    @pytest.mark.asyncio
    async def test_pass_when_both_decisions_allow(self, db_session: AsyncSession):
        """global + symbol 両方 entry_allowed=True → pass"""
        signal = _make_signal()
        db_session.add(signal)
        await _insert_decision(db_session, _make_decision(ticker=None))
        await _insert_decision(db_session, _make_decision(ticker="7203"))

        gate = SignalStrategyGate(db_session)
        result = await gate.check(signal, evaluation_time=_NOW)

        assert result.entry_allowed is True
        assert result.size_ratio == 1.0
        assert result.blocking_reasons == []
        assert result.bypassed is False

    @pytest.mark.asyncio
    async def test_reject_when_global_missing(self, db_session: AsyncSession):
        """global decision が存在しない → reject with decision_missing:global"""
        signal = _make_signal()
        db_session.add(signal)
        # symbol decision のみ
        await _insert_decision(db_session, _make_decision(ticker="7203"))

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError) as exc_info:
            await gate.check(signal, evaluation_time=_NOW)

        assert "decision_missing:global" in exc_info.value.blocking_reasons

    @pytest.mark.asyncio
    async def test_reject_when_symbol_missing(self, db_session: AsyncSession):
        """symbol decision が存在しない → reject with decision_missing:symbol"""
        signal = _make_signal()
        db_session.add(signal)
        # global decision のみ
        await _insert_decision(db_session, _make_decision(ticker=None))

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError) as exc_info:
            await gate.check(signal, evaluation_time=_NOW)

        assert "decision_missing:symbol" in exc_info.value.blocking_reasons

    @pytest.mark.asyncio
    async def test_reject_when_both_missing(self, db_session: AsyncSession):
        """global も symbol も存在しない → 両方の missing が blocking_reasons に含まれる"""
        signal = _make_signal()
        db_session.add(signal)

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError) as exc_info:
            await gate.check(signal, evaluation_time=_NOW)

        reasons = exc_info.value.blocking_reasons
        assert "decision_missing:global" in reasons
        assert "decision_missing:symbol" in reasons

    @pytest.mark.asyncio
    async def test_reject_when_global_stale(self, db_session: AsyncSession):
        """global decision が stale → reject"""
        signal = _make_signal()
        db_session.add(signal)
        stale_time = _NOW - timedelta(seconds=200)
        await _insert_decision(
            db_session, _make_decision(ticker=None, evaluation_time=stale_time, strategy_code="strat_global")
        )
        await _insert_decision(db_session, _make_decision(ticker="7203"))

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError) as exc_info:
            await gate.check(signal, evaluation_time=_NOW)

        assert any("decision_stale:global" in r for r in exc_info.value.blocking_reasons)

    @pytest.mark.asyncio
    async def test_reject_when_symbol_stale(self, db_session: AsyncSession):
        """symbol decision が stale → reject"""
        signal = _make_signal()
        db_session.add(signal)
        stale_time = _NOW - timedelta(seconds=200)
        await _insert_decision(db_session, _make_decision(ticker=None))
        await _insert_decision(
            db_session, _make_decision(ticker="7203", evaluation_time=stale_time, strategy_code="strat_sym")
        )

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError) as exc_info:
            await gate.check(signal, evaluation_time=_NOW)

        assert any("decision_stale:symbol" in r for r in exc_info.value.blocking_reasons)

    @pytest.mark.asyncio
    async def test_reject_when_global_entry_not_allowed(self, db_session: AsyncSession):
        """global entry_allowed=False → reject"""
        signal = _make_signal()
        db_session.add(signal)
        await _insert_decision(
            db_session, _make_decision(ticker=None, entry_allowed=False, strategy_code="strat_g")
        )
        await _insert_decision(db_session, _make_decision(ticker="7203"))

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError) as exc_info:
            await gate.check(signal, evaluation_time=_NOW)

        assert any("decision_blocked:global" in r for r in exc_info.value.blocking_reasons)

    @pytest.mark.asyncio
    async def test_reject_when_symbol_entry_not_allowed(self, db_session: AsyncSession):
        """symbol entry_allowed=False → reject"""
        signal = _make_signal()
        db_session.add(signal)
        await _insert_decision(db_session, _make_decision(ticker=None))
        await _insert_decision(
            db_session, _make_decision(ticker="7203", entry_allowed=False, strategy_code="strat_s")
        )

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError) as exc_info:
            await gate.check(signal, evaluation_time=_NOW)

        assert any("decision_blocked:symbol" in r for r in exc_info.value.blocking_reasons)

    @pytest.mark.asyncio
    async def test_size_ratio_uses_minimum(self, db_session: AsyncSession):
        """size_ratio は global と symbol の min を使用する"""
        signal = _make_signal()
        db_session.add(signal)
        await _insert_decision(db_session, _make_decision(ticker=None, size_ratio=0.5))
        await _insert_decision(db_session, _make_decision(ticker="7203", size_ratio=0.8))

        gate = SignalStrategyGate(db_session)
        result = await gate.check(signal, evaluation_time=_NOW)

        assert result.size_ratio == 0.5

    @pytest.mark.asyncio
    async def test_reject_when_size_ratio_zero(self, db_session: AsyncSession):
        """size_ratio == 0.0 → reject with size_ratio_zero"""
        signal = _make_signal()
        db_session.add(signal)
        await _insert_decision(db_session, _make_decision(ticker=None, size_ratio=0.0))
        await _insert_decision(db_session, _make_decision(ticker="7203", size_ratio=1.0))

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError) as exc_info:
            await gate.check(signal, evaluation_time=_NOW)

        assert "size_ratio_zero" in exc_info.value.blocking_reasons

    @pytest.mark.asyncio
    async def test_direction_mismatch_treated_as_missing(self, db_session: AsyncSession):
        """signal=buy(long)なのに decision.direction=short → direction 不一致 → missing 扱い"""
        signal = _make_signal(side="buy")
        db_session.add(signal)
        # global は long 向けのみ存在するが symbol は short 向けしかない
        await _insert_decision(db_session, _make_decision(ticker=None, direction="long"))
        await _insert_decision(db_session, _make_decision(ticker="7203", direction="short"))

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError) as exc_info:
            await gate.check(signal, evaluation_time=_NOW)

        assert "decision_missing:symbol" in exc_info.value.blocking_reasons

    @pytest.mark.asyncio
    async def test_exit_signal_bypasses_gate(self, db_session: AsyncSession):
        """signal_type=exit は gate をバイパスして entry_allowed=True を返す"""
        signal = _make_signal(signal_type="exit")
        db_session.add(signal)
        # decision を一切用意しない

        gate = SignalStrategyGate(db_session)
        result = await gate.check(signal, evaluation_time=_NOW)

        assert result.entry_allowed is True
        assert result.bypassed is True
        assert result.size_ratio == 1.0


# ─── signal_strategy_decisions 保存テスト ─────────────────────────────────────


class TestSignalStrategyDecisionSave:

    @pytest.mark.asyncio
    async def test_pass_result_is_saved_to_db(self, db_session: AsyncSession):
        """pass 時に signal_strategy_decisions に 1 件 INSERT されること"""
        signal = _make_signal()
        db_session.add(signal)
        await _insert_decision(db_session, _make_decision(ticker=None))
        await _insert_decision(db_session, _make_decision(ticker="7203"))

        gate = SignalStrategyGate(db_session)
        await gate.check(signal, evaluation_time=_NOW)
        await db_session.flush()

        rows = (await db_session.execute(
            select(SignalStrategyDecision).where(SignalStrategyDecision.signal_id == signal.id)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].entry_allowed is True
        assert rows[0].size_ratio == 1.0

    @pytest.mark.asyncio
    async def test_reject_result_is_saved_to_db(self, db_session: AsyncSession):
        """reject 時にも signal_strategy_decisions に INSERT されること"""
        signal = _make_signal()
        db_session.add(signal)
        # global のみ → symbol missing → reject

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError):
            await gate.check(signal, evaluation_time=_NOW)
        await db_session.flush()

        rows = (await db_session.execute(
            select(SignalStrategyDecision).where(SignalStrategyDecision.signal_id == signal.id)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].entry_allowed is False
        assert "decision_missing:symbol" in rows[0].blocking_reasons_json

    @pytest.mark.asyncio
    async def test_saved_record_has_correct_fields(self, db_session: AsyncSession):
        """保存レコードのフィールドが正しいこと"""
        signal = _make_signal(ticker="7203", side="buy")
        db_session.add(signal)
        global_dec = _make_decision(ticker=None, size_ratio=0.7)
        symbol_dec = _make_decision(ticker="7203", size_ratio=0.9)
        await _insert_decision(db_session, global_dec)
        await _insert_decision(db_session, symbol_dec)

        gate = SignalStrategyGate(db_session)
        await gate.check(signal, evaluation_time=_NOW)
        await db_session.flush()

        row = (await db_session.execute(
            select(SignalStrategyDecision).where(SignalStrategyDecision.signal_id == signal.id)
        )).scalar_one()

        assert row.ticker == "7203"
        assert row.signal_direction == "long"
        assert row.global_decision_id == global_dec.id
        assert row.symbol_decision_id == symbol_dec.id
        assert row.size_ratio == 0.7  # min(0.7, 0.9)

    @pytest.mark.asyncio
    async def test_exit_bypass_does_not_save_to_db(self, db_session: AsyncSession):
        """exit signal のバイパス時は signal_strategy_decisions に保存されないこと"""
        signal = _make_signal(signal_type="exit")
        db_session.add(signal)

        gate = SignalStrategyGate(db_session)
        await gate.check(signal, evaluation_time=_NOW)
        await db_session.flush()

        rows = (await db_session.execute(
            select(SignalStrategyDecision).where(SignalStrategyDecision.signal_id == signal.id)
        )).scalars().all()
        assert len(rows) == 0


# ─── Pipeline 統合テスト ───────────────────────────────────────────────────────


class TestPipelineIntegration:

    @pytest.mark.asyncio
    async def test_pipeline_rejects_signal_when_gate_raises(self, db_session: AsyncSession):
        """Strategy Gate が拒否したとき signal.status が REJECTED になること"""
        from trade_app.services.pipeline import SignalPipeline

        signal = _make_signal()
        db_session.add(signal)
        await db_session.flush()

        # global decision のみ（symbol missing → gate rejected）
        await _insert_decision(db_session, _make_decision(ticker=None))
        await db_session.flush()

        with (
            patch("trade_app.services.pipeline._get_broker"),
            patch("trade_app.services.pipeline._get_redis"),
        ):
            await SignalPipeline._run(db_session, signal.id)

        assert signal.status == SignalStatus.REJECTED.value
        assert "strategy gate rejected" in signal.reject_reason

    @pytest.mark.asyncio
    async def test_pipeline_passes_gate_and_proceeds_to_risk(self, db_session: AsyncSession):
        """Strategy Gate を通過した場合に RiskManager が呼ばれること"""
        from trade_app.services.pipeline import SignalPipeline

        # _NOW は固定過去時刻のため、パイプラインが使う datetime.now() との差が
        # SIGNAL_MAX_DECISION_AGE_SEC を超えると stale 判定されてしまう。
        # evaluation_time を現在時刻に設定して時刻依存を回避する。
        fresh_now = datetime.now(_UTC)
        signal = _make_signal()
        db_session.add(signal)
        await _insert_decision(db_session, _make_decision(ticker=None, evaluation_time=fresh_now))
        await _insert_decision(db_session, _make_decision(ticker="7203", evaluation_time=fresh_now))
        await db_session.flush()

        risk_called = []

        async def mock_risk_check(s, planned_qty=None):
            risk_called.append(s.id)

        with (
            patch("trade_app.services.pipeline._get_broker"),
            patch("trade_app.services.pipeline._get_redis"),
            patch(
                "trade_app.services.risk_manager.RiskManager.check",
                side_effect=mock_risk_check,
            ),
            patch("trade_app.services.order_router.OrderRouter.route") as mock_route,
        ):
            mock_route.return_value = AsyncMock()
            await SignalPipeline._run(db_session, signal.id)

        assert signal.id in risk_called


# ─── API テスト ────────────────────────────────────────────────────────────────

_AUTH = {"Authorization": "Bearer changeme_before_production"}
_BAD_AUTH = {"Authorization": "Bearer wrong"}


@pytest.fixture
def test_client(db_session: AsyncSession):
    from trade_app.main import app
    from trade_app.models.database import get_db

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


class TestStrategyDecisionAPI:

    def test_get_strategy_decision_returns_404_when_not_found(self, test_client):
        """存在しない signal_id → 404"""
        resp = test_client.get(
            f"/api/signals/{uuid.uuid4()}/strategy-decision",
            headers=_AUTH,
        )
        assert resp.status_code == 404

    def test_get_strategy_decision_returns_403_with_bad_auth(self, test_client, db_session):
        """不正なトークン → 403"""
        resp = test_client.get(
            f"/api/signals/{uuid.uuid4()}/strategy-decision",
            headers=_BAD_AUTH,
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_get_strategy_decision_returns_decision(self, test_client, db_session: AsyncSession):
        """strategy gate 判定結果が保存されていれば正しく返ること"""
        signal = _make_signal()
        db_session.add(signal)
        await _insert_decision(db_session, _make_decision(ticker=None))
        await _insert_decision(db_session, _make_decision(ticker="7203"))
        await db_session.flush()

        gate = SignalStrategyGate(db_session)
        await gate.check(signal, evaluation_time=_NOW)
        await db_session.flush()

        resp = test_client.get(
            f"/api/signals/{signal.id}/strategy-decision",
            headers=_AUTH,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["signal_id"] == signal.id
        assert data["ticker"] == "7203"
        assert data["signal_direction"] == "long"
        assert data["entry_allowed"] is True
        assert data["size_ratio"] == 1.0
        assert data["blocking_reasons"] == []

    @pytest.mark.asyncio
    async def test_get_strategy_decision_rejected_signal(self, test_client, db_session: AsyncSession):
        """reject された signal の判定結果が返ること"""
        signal = _make_signal()
        db_session.add(signal)
        # global のみ → reject
        await _insert_decision(db_session, _make_decision(ticker=None))
        await db_session.flush()

        gate = SignalStrategyGate(db_session)
        with pytest.raises(StrategyGateRejectedError):
            await gate.check(signal, evaluation_time=_NOW)
        await db_session.flush()

        resp = test_client.get(
            f"/api/signals/{signal.id}/strategy-decision",
            headers=_AUTH,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["entry_allowed"] is False
        assert "decision_missing:symbol" in data["blocking_reasons"]
