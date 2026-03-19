"""
RecoveryManager — 起動時リカバリ・状態整合サービス

アプリケーション再起動時に「宙ぶらりん」な注文を検出し、
ブローカーへ再照会して DB を整合した状態に戻す。

対象となる注文:
  - SUBMITTED / PARTIAL  : 前回プロセス停止時に約定確認が未完了
  - PENDING              : 発注処理が途中で停止した可能性（FAILED に変更）
  - UNKNOWN              : 前回から継続して状態不明のまま残っている

処理フロー（recover_on_startup）:
  1. 未解決注文を DB から取得
  2. ブローカーへ状態照会
  3. 結果に応じて DB 更新（FILLED → ポジション開設、UNKNOWN → 継続マーク）
  4. システムイベントとして記録
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.database import AsyncSessionLocal
from trade_app.models.enums import (
    AuditEventType,
    OrderStatus,
    SystemEventType,
)
from trade_app.models.execution import Execution
from trade_app.models.order import Order
from trade_app.models.order_state_transition import record_transition
from trade_app.models.system_event import SystemEvent
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.broker_call_logger import BrokerCallLogger

logger = logging.getLogger(__name__)


def _get_broker():
    """設定に応じてブローカーアダプターを返す"""
    from trade_app.config import get_settings
    from trade_app.brokers.mock_broker import MockBrokerAdapter
    from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter

    settings = get_settings()
    if settings.BROKER_TYPE == "tachibana":
        return TachibanaBrokerAdapter()
    return MockBrokerAdapter()


class RecoveryManager:
    """
    起動時リカバリサービス。
    前回プロセス停止時に未完了のまま残った注文を整合させる。
    """

    async def recover_on_startup(self) -> None:
        """
        起動時リカバリを実行する。
        main.py の lifespan 内（サーバー起動前）に呼び出す。
        """
        async with AsyncSessionLocal() as db:
            await self._run_recovery(db)

    async def _run_recovery(self, db: AsyncSession) -> None:
        """リカバリ処理の本体"""
        await self._log_system_event(
            db=db,
            event_type=SystemEventType.RECOVERY_START,
            message="起動時リカバリ開始",
        )

        # 未解決注文（SUBMITTED / PARTIAL）と孤立注文（PENDING）を取得
        result = await db.execute(
            select(Order).where(
                Order.status.in_([
                    OrderStatus.SUBMITTED.value,
                    OrderStatus.PARTIAL.value,
                    OrderStatus.PENDING.value,
                    OrderStatus.UNKNOWN.value,
                ])
            )
        )
        orders = result.scalars().all()

        if not orders:
            logger.info("起動時リカバリ: 未解決注文なし（正常）")
            await self._log_system_event(
                db=db,
                event_type=SystemEventType.RECOVERY_COMPLETE,
                details={"recovered": 0, "skipped": 0},
                message="起動時リカバリ完了: 未解決注文なし",
            )
            await db.commit()
            return

        logger.warning("起動時リカバリ: %d 件の未解決注文を検出", len(orders))

        broker = _get_broker()
        broker_logger = BrokerCallLogger(db)
        audit = AuditLogger(db)
        recovered = 0
        skipped = 0

        for order in orders:
            try:
                result = await self._reconcile_order(
                    db=db,
                    order=order,
                    broker=broker,
                    broker_logger=broker_logger,
                    audit=audit,
                )
                if result:
                    recovered += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(
                    "リカバリエラー order_id=%s error=%s", order.id, e, exc_info=True
                )
                skipped += 1

        await self._log_system_event(
            db=db,
            event_type=SystemEventType.RECOVERY_COMPLETE,
            details={"recovered": recovered, "skipped": skipped, "total": len(orders)},
            message=f"起動時リカバリ完了: {recovered}件回復 / {skipped}件スキップ",
        )
        await db.commit()
        logger.info(
            "起動時リカバリ完了: recovered=%d skipped=%d total=%d",
            recovered, skipped, len(orders),
        )

    async def _reconcile_order(
        self,
        db: AsyncSession,
        order: Order,
        broker,
        broker_logger: BrokerCallLogger,
        audit: AuditLogger,
    ) -> bool:
        """
        1件の注文をブローカーへ照会して整合させる。

        Returns:
            True: 状態を更新した
            False: スキップ（broker_order_id がない、照会失敗など）
        """
        now = datetime.now(timezone.utc)

        # ─── PENDING は発注が途中で止まったもの → FAILED に変更 ───────────
        if order.status == OrderStatus.PENDING.value:
            logger.warning(
                "リカバリ: PENDING のまま残っている注文を FAILED に変更: order_id=%s",
                order.id,
            )
            order.status = OrderStatus.FAILED.value
            order.updated_at = now
            await record_transition(
                db=db,
                order_id=order.id,
                from_status=OrderStatus.PENDING.value,
                to_status=OrderStatus.FAILED.value,
                reason="起動時リカバリ: PENDING のまま残存（発注処理が途中で停止）",
                triggered_by="recovery",
            )
            await db.flush()
            return True

        # ─── broker_order_id がない場合は照会不可 ──────────────────────────
        if not order.broker_order_id:
            logger.warning(
                "リカバリ: broker_order_id がない注文をスキップ: order_id=%s status=%s",
                order.id, order.status,
            )
            return False

        # ─── ブローカーへ状態照会 ──────────────────────────────────────────
        br_log = await broker_logger.before_status_query(
            order_id=order.id,
            broker_order_id=order.broker_order_id,
        )
        await db.flush()

        try:
            status_resp = await broker.get_order_status(order.broker_order_id)
            await broker_logger.after_status_query(
                broker_request=br_log,
                order_id=order.id,
                response=status_resp,
            )
        except Exception as e:
            await broker_logger.on_error(
                broker_request=br_log, order_id=order.id, error=e
            )
            logger.error(
                "リカバリ: 状態照会失敗 order_id=%s error=%s", order.id, e
            )
            await db.flush()
            return False

        broker_status = status_resp.status
        prev_status = order.status

        # ─── 状態変化なし → UNKNOWN は継続マーク ────────────────────────
        if broker_status.value == prev_status:
            logger.info(
                "リカバリ: 状態変化なし order_id=%s status=%s", order.id, prev_status
            )
            return False

        logger.info(
            "リカバリ: 状態変化 order_id=%s %s → %s",
            order.id, prev_status, broker_status.value,
        )

        # ─── FILLED ────────────────────────────────────────────────────────
        if broker_status == OrderStatus.FILLED:
            from sqlalchemy import select as sa_select
            from trade_app.services.position_manager import PositionManager

            broker_exec_id = status_resp.broker_execution_id
            total_filled = status_resp.filled_quantity or order.quantity
            delta_qty = total_filled - (order.filled_quantity or 0)

            # ─── Execution 重複防止チェック ──────────────────────────────
            skip_execution = False
            if broker_exec_id:
                ex_check = await db.execute(
                    sa_select(Execution).where(
                        Execution.broker_execution_id == broker_exec_id
                    )
                )
                if ex_check.scalar_one_or_none():
                    logger.warning(
                        "リカバリ: Execution 重複スキップ broker_exec_id=%s order_id=%s",
                        broker_exec_id, order.id,
                    )
                    skip_execution = True
                    delta_qty = 0  # 新規 Execution 不要

            # ─── Execution 記録（delta > 0 の場合のみ）───────────────────
            if not skip_execution and delta_qty > 0:
                execution = Execution(
                    order_id=order.id,
                    broker_execution_id=broker_exec_id,
                    ticker=order.ticker,
                    side=order.side,
                    quantity=delta_qty,
                    price=status_resp.filled_price or 0.0,
                    executed_at=now,
                    created_at=now,
                )
                db.add(execution)

            order.status = OrderStatus.FILLED.value
            order.filled_quantity = total_filled
            order.filled_price = status_resp.filled_price
            order.filled_at = now
            order.updated_at = now

            await record_transition(
                db=db,
                order_id=order.id,
                from_status=prev_status,
                to_status=OrderStatus.FILLED.value,
                reason="起動時リカバリ: 約定確認",
                triggered_by="recovery",
            )
            await db.flush()

            # ─── exit 注文 vs entry 注文で処理を分岐 ─────────────────────
            if order.is_exit_order and order.position_id:
                # exit 注文: ポジションクローズ
                from trade_app.models.position import Position as _Pos
                from trade_app.models.enums import PositionStatus as _PS

                pos_result = await db.execute(
                    sa_select(_Pos).where(_Pos.id == order.position_id)
                )
                position = pos_result.scalar_one_or_none()

                if position and position.status == _PS.CLOSING.value:
                    pos_manager = PositionManager(db=db, audit=audit)
                    if delta_qty > 0:
                        is_flat = await pos_manager.apply_exit_execution(
                            position=position,
                            executed_qty=delta_qty,
                            executed_price=status_resp.filled_price or 0.0,
                        )
                    else:
                        # delta == 0 (重複 Execution スキップ): 残数量を確認
                        is_flat = (
                            position.remaining_qty is not None
                            and position.remaining_qty == 0
                        )
                    if is_flat:
                        await pos_manager.finalize_exit(
                            position=position, exit_order=order
                        )
                        logger.info(
                            "リカバリ: exit注文約定 → ポジションクローズ pos=%s",
                            position.id[:8],
                        )
                    else:
                        logger.info(
                            "リカバリ: exit注文約定 remaining_qty=%s pos=%s — CLOSING 維持",
                            position.remaining_qty, position.id[:8] if position else "N/A",
                        )
                else:
                    logger.warning(
                        "リカバリ: exit注文 FILLED だがポジションが CLOSING でない"
                        " pos=%s status=%s",
                        order.position_id,
                        position.status if position else "N/A",
                    )
            else:
                # entry 注文: ポジション開設
                from trade_app.models.signal import TradeSignal

                signal_result = await db.execute(
                    sa_select(TradeSignal).where(TradeSignal.id == order.signal_id)
                )
                signal = signal_result.scalar_one_or_none()
                if signal:
                    pos_manager = PositionManager(db=db, audit=audit)
                    await pos_manager.open_position(order=order, signal=signal)

            await self._log_system_event(
                db=db,
                event_type=SystemEventType.ORDER_RECONCILED,
                details={
                    "order_id": order.id,
                    "ticker": order.ticker,
                    "is_exit_order": order.is_exit_order,
                    "prev_status": prev_status,
                    "new_status": OrderStatus.FILLED.value,
                },
                message=f"リカバリ: 約定確認 {order.ticker} order={order.id[:8]}",
            )

        # ─── PARTIAL ───────────────────────────────────────────────────────
        elif broker_status == OrderStatus.PARTIAL:
            broker_exec_id = status_resp.broker_execution_id
            total_filled = status_resp.filled_quantity or 0
            # delta: 今回新たに約定した分のみを記録（cumulative ではない）
            delta_qty = total_filled - (order.filled_quantity or 0)

            # 重複防止チェック
            skip_partial_exec = False
            if broker_exec_id:
                from sqlalchemy import select as sa_select
                dup_check = await db.execute(
                    sa_select(Execution).where(
                        Execution.broker_execution_id == broker_exec_id
                    )
                )
                if dup_check.scalar_one_or_none():
                    logger.warning(
                        "リカバリ: partial Execution 重複スキップ broker_exec_id=%s",
                        broker_exec_id,
                    )
                    skip_partial_exec = True
                    delta_qty = 0

            if not skip_partial_exec and delta_qty > 0:
                execution = Execution(
                    order_id=order.id,
                    broker_execution_id=broker_exec_id,
                    ticker=order.ticker,
                    side=order.side,
                    quantity=delta_qty,
                    price=status_resp.filled_price or 0.0,
                    executed_at=now,
                    created_at=now,
                )
                db.add(execution)

            order.status = OrderStatus.PARTIAL.value
            order.filled_quantity = total_filled
            order.filled_price = status_resp.filled_price
            order.updated_at = now

            await record_transition(
                db=db,
                order_id=order.id,
                from_status=prev_status,
                to_status=OrderStatus.PARTIAL.value,
                reason=f"起動時リカバリ: 部分約定確認 delta={delta_qty}",
                triggered_by="recovery",
            )

            # exit 注文の PARTIAL: remaining_qty を更新
            if delta_qty > 0 and order.is_exit_order and order.position_id:
                from sqlalchemy import select as sa_select
                from trade_app.models.position import Position as _Pos
                from trade_app.models.enums import PositionStatus as _PS
                from trade_app.services.position_manager import PositionManager

                pos_result = await db.execute(
                    sa_select(_Pos).where(_Pos.id == order.position_id)
                )
                position = pos_result.scalar_one_or_none()
                if position and position.status == _PS.CLOSING.value:
                    pos_manager = PositionManager(db=db, audit=audit)
                    await pos_manager.apply_exit_execution(
                        position=position,
                        executed_qty=delta_qty,
                        executed_price=status_resp.filled_price or 0.0,
                    )

        # ─── CANCELLED / REJECTED ──────────────────────────────────────────
        elif broker_status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            order.status = broker_status.value
            order.updated_at = now
            await record_transition(
                db=db,
                order_id=order.id,
                from_status=prev_status,
                to_status=broker_status.value,
                reason="起動時リカバリ: ブローカーから終端状態を取得",
                triggered_by="recovery",
            )

            # exit 注文が CANCELLED/REJECTED → CLOSING のポジションを OPEN に戻す
            if order.is_exit_order and order.position_id:
                from sqlalchemy import select as sa_select
                from trade_app.models.position import Position as _Pos
                from trade_app.models.enums import PositionStatus as _PS
                from trade_app.services.position_manager import PositionManager

                pos_result = await db.execute(
                    sa_select(_Pos).where(_Pos.id == order.position_id)
                )
                position = pos_result.scalar_one_or_none()
                if position and position.status == _PS.CLOSING.value:
                    pos_manager = PositionManager(db=db, audit=audit)
                    await pos_manager.revert_to_open(
                        position=position,
                        reason=f"起動時リカバリ: exit注文が {broker_status.value}",
                        triggered_by="recovery",
                    )
                    logger.warning(
                        "リカバリ: exit注文 %s → ポジションを OPEN に戻す pos=%s",
                        broker_status.value, position.id[:8],
                    )

        # ─── UNKNOWN ───────────────────────────────────────────────────────
        elif broker_status == OrderStatus.UNKNOWN:
            order.status = OrderStatus.UNKNOWN.value
            order.updated_at = now
            await record_transition(
                db=db,
                order_id=order.id,
                from_status=prev_status,
                to_status=OrderStatus.UNKNOWN.value,
                reason="起動時リカバリ: ブローカーが状態を返さなかった",
                triggered_by="recovery",
            )
            logger.warning(
                "リカバリ: UNKNOWN 状態 order_id=%s ticker=%s — 手動確認が必要",
                order.id, order.ticker,
            )

        await db.flush()
        return True

    @staticmethod
    async def _log_system_event(
        db: AsyncSession,
        event_type: SystemEventType,
        details: dict | None = None,
        message: str = "",
    ) -> None:
        """システムイベントを DB に追記する"""
        event = SystemEvent(
            event_type=event_type.value,
            details=details,
            message=message,
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        await db.flush()
