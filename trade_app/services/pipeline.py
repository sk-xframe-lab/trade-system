"""
SignalPipeline — シグナル処理パイプライン

FastAPI の BackgroundTasks から完全に切り離した独立モジュール。
将来 Celery / ARQ / Cloud Tasks に移行する際はここだけ差し替える。

処理範囲: シグナル受信 → Strategy Gate → Planning Layer → リスクチェック → 発注（SUBMITTED 状態まで）
約定確認以降は OrderPoller が担当する。

処理フロー:
  1. SignalReceiver: 受信・冪等性チェック・DB保存
  2. SignalStrategyGate: strategy decision による前段ゲート（entry のみ）
  3. SignalPlanningService: サイズ調整・執行パラメータ計画
  4. RiskManager: 市場時間・残高・halt などのリスクチェック
  5. OrderRouter: ブローカーへの発注
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.database import AsyncSessionLocal
from trade_app.models.enums import AuditEventType, SignalStatus
from trade_app.models.order_state_transition import record_transition
from trade_app.models.signal import TradeSignal
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.broker_call_logger import BrokerCallLogger
from trade_app.services.order_router import OrderAlreadyInProgressError, OrderRouter
from trade_app.services.planning.context import PlannerContextBuilder
from trade_app.services.planning.service import SignalPlanRejectedError, SignalPlanningService
from trade_app.services.risk_manager import RiskManager, RiskRejectedError
from trade_app.services.signal_strategy_gate import SignalStrategyGate, StrategyGateRejectedError

logger = logging.getLogger(__name__)


def _get_broker():
    """設定に応じてブローカーアダプターを返す（シングルトンではなく呼び出し毎に生成）"""
    from trade_app.config import get_settings
    from trade_app.brokers.mock_broker import MockBrokerAdapter
    from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter

    settings = get_settings()
    if settings.BROKER_TYPE == "tachibana":
        return TachibanaBrokerAdapter()
    return MockBrokerAdapter()


def _get_redis():
    """Redis クライアントを返す"""
    from trade_app.main import get_redis_client
    return get_redis_client()


class SignalPipeline:
    """
    シグナル処理パイプライン。

    FastAPI 依存がなく、どのコンテキストからでも呼び出せる。
    内部で AsyncSessionLocal から DB セッションを生成するので
    引数に DB セッションを渡す必要がない。

    将来の Queue 対応:
        Celery タスク: @app.task def process_signal(signal_id): asyncio.run(SignalPipeline.process(signal_id))
        ARQ タスク: async def process_signal(ctx, signal_id): await SignalPipeline.process(signal_id)
    """

    @staticmethod
    async def process(signal_id: str) -> None:
        """
        シグナルの発注処理を実行する（SUBMITTED 状態まで）。

        以降の約定確認・ポジション開設は OrderPoller に委譲する。

        Args:
            signal_id: 処理対象のシグナル UUID
        """
        try:
            async with AsyncSessionLocal() as db:
                await SignalPipeline._run(db, signal_id)
        except Exception as e:
            logger.error(
                "SignalPipeline 未補足エラー: signal_id=%s error=%s",
                signal_id, e, exc_info=True,
            )

    @staticmethod
    async def _run(db: AsyncSession, signal_id: str) -> None:
        """パイプライン本体（DB セッション内で実行）"""
        audit = AuditLogger(db)

        # ─── シグナル取得 ──────────────────────────────────────────────
        result = await db.execute(
            select(TradeSignal).where(TradeSignal.id == signal_id)
        )
        signal = result.scalar_one_or_none()
        if signal is None:
            logger.error("SignalPipeline: シグナルが見つかりません: %s", signal_id)
            return

        # 既に PROCESSING 以降の状態なら重複実行を無視
        if signal.status not in (SignalStatus.RECEIVED.value, SignalStatus.PROCESSING.value):
            logger.info(
                "SignalPipeline: スキップ（既に処理済み）: signal_id=%s status=%s",
                signal_id, signal.status,
            )
            return

        broker = _get_broker()
        redis_client = _get_redis()
        broker_logger = BrokerCallLogger(db)

        # ─── Strategy Gate（RiskManager より前）──────────────────────────
        gate = SignalStrategyGate(db)
        try:
            gate_result = await gate.check(signal)
        except StrategyGateRejectedError as e:
            signal.status = SignalStatus.REJECTED.value
            signal.reject_reason = str(e)
            await audit.log(
                event_type=AuditEventType.STRATEGY_GATE_REJECTED,
                entity_type="signal",
                entity_id=signal_id,
                details={"reason": e.reason, "blocking_reasons": e.blocking_reasons},
                message=f"Strategy Gate 拒否: {e.reason}",
            )
            await db.commit()
            logger.warning(
                "Strategy Gate 拒否: signal_id=%s reasons=%s",
                signal_id, e.blocking_reasons,
            )
            return

        # ─── Signal Planning Layer ─────────────────────────────────────
        planning_builder = PlannerContextBuilder(db=db)
        ctx = await planning_builder.build(
            signal=signal,
            size_ratio=gate_result.size_ratio,
        )
        planning_service = SignalPlanningService(db=db, audit=audit)
        try:
            plan = await planning_service.plan(signal, ctx)
        except SignalPlanRejectedError as e:
            signal.status = SignalStatus.REJECTED.value
            signal.reject_reason = str(e)
            await audit.log(
                event_type=AuditEventType.SIGNAL_REJECTED,
                entity_type="signal",
                entity_id=signal_id,
                details={"reason_code": e.reason_code.value, "detail": e.detail,
                         "plan_id": e.plan_id},
                message=f"Planning 拒否: {e.reason_code.value}: {e.detail}",
            )
            await db.commit()
            logger.warning(
                "Planning 拒否: signal_id=%s reason=%s detail=%s",
                signal_id, e.reason_code.value, e.detail,
            )
            return

        # ─── リスクチェック ────────────────────────────────────────────
        risk_manager = RiskManager(db=db, broker=broker, audit=audit)
        try:
            await risk_manager.check(signal, planned_qty=plan.planned_order_qty)
        except RiskRejectedError as e:
            signal.status = SignalStatus.REJECTED.value
            signal.reject_reason = str(e)
            await audit.log(
                event_type=AuditEventType.RISK_REJECTED,
                entity_type="signal",
                entity_id=signal_id,
                details={"reason": e.reason},
                message=f"リスク拒否: {e.reason}",
            )
            await db.commit()
            logger.warning("リスク拒否: signal_id=%s reason=%s", signal_id, e.reason)
            return

        # ─── 発注 ─────────────────────────────────────────────────────
        order_router = OrderRouter(
            db=db,
            broker=broker,
            redis_client=redis_client,
            audit=audit,
            broker_logger=broker_logger,
        )
        try:
            order = await order_router.route(signal, planned_qty=plan.planned_order_qty)
            logger.info(
                "SignalPipeline 完了: signal_id=%s order_id=%s broker_order_id=%s",
                signal_id, order.id, order.broker_order_id,
            )
        except OrderAlreadyInProgressError:
            logger.info("SignalPipeline: 別タスクが処理中のためスキップ: %s", signal_id)
        except Exception as e:
            logger.error(
                "SignalPipeline 発注エラー: signal_id=%s error=%s",
                signal_id, e, exc_info=True,
            )
