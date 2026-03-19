"""
OrderPoller — 注文状態照会ジョブ

未解決注文（SUBMITTED / PARTIAL）をブローカーに照会し、
約定・キャンセル・失敗を検出してDB・ポジションを更新する。

SignalPipeline が SUBMITTED 状態まで処理したあと、
このポーラーが約定確認・ポジション開設を担当する。

Phase 3 追加:
  - exit注文（is_exit_order=True）の約定 → PositionManager.finalize_exit() 呼び出し
  - Execution の重複計上防止（broker_execution_id で事前チェック）

実行モデル:
  main.py の lifespan 内で asyncio.create_task() により
  バックグラウンドタスクとして起動する。
  1サイクルごとに POLL_INTERVAL_SEC 秒スリープする。
"""
import asyncio
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

# 照会間隔（秒）
POLL_INTERVAL_SEC = 5
# 1回のポーリングサイクルで処理する最大注文件数
MAX_ORDERS_PER_CYCLE = 50
# この秒数を超えて SUBMITTED のまま変化がない注文を UNKNOWN に変更する
STUCK_ORDER_SEC = 3600  # 1時間


def _get_broker():
    """設定に応じてブローカーアダプターを返す"""
    from trade_app.config import get_settings
    from trade_app.brokers.mock_broker import MockBrokerAdapter
    from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter

    settings = get_settings()
    if settings.BROKER_TYPE == "tachibana":
        return TachibanaBrokerAdapter()
    return MockBrokerAdapter()


class OrderPoller:
    """
    SUBMITTED / PARTIAL 状態の注文をブローカーに照会し続けるポーラー。

    主な責務:
      - 未約定注文の状態を定期的にブローカーへ照会
      - 通常注文 FILLED 検出 → Execution 記録 → PositionManager でポジション開設
      - exit注文 FILLED 検出 → PositionManager.finalize_exit() でポジションクローズ
      - PARTIAL 検出 → Execution 記録（部分約定分）
      - CANCELLED / REJECTED 検出 → Order を終端状態へ遷移
      - 長時間変化なし → UNKNOWN に遷移（新規発注をブロック）

    重複防止:
      - broker_execution_id が既存 Execution と重複する場合は新規作成しない
      - 同一 fill の二重反映を防ぐ
    """

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """バックグラウンドポーリングを開始する"""
        if self._running:
            logger.warning("OrderPoller は既に起動中です")
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="OrderPoller")
        logger.info("OrderPoller 起動: interval=%ds", POLL_INTERVAL_SEC)

    async def stop(self) -> None:
        """ポーリングを停止してタスクが完了するのを待つ"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("OrderPoller 停止完了")

    async def _poll_loop(self) -> None:
        """ポーリングループ（バックグラウンドで永続実行）"""
        await self._record_system_event(
            SystemEventType.POLLER_START,
            message=f"OrderPoller 起動 interval={POLL_INTERVAL_SEC}s",
        )

        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                logger.error("OrderPoller サイクルエラー: %s", e, exc_info=True)
                await self._record_system_event(
                    SystemEventType.POLLER_ERROR,
                    details={"error": str(e)},
                    message=f"OrderPoller エラー: {e}",
                )
            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _poll_once(self) -> None:
        """1サイクル分のポーリング処理"""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Order)
                .where(
                    Order.status.in_([
                        OrderStatus.SUBMITTED.value,
                        OrderStatus.PARTIAL.value,
                    ])
                )
                .order_by(Order.submitted_at.asc())
                .limit(MAX_ORDERS_PER_CYCLE)
            )
            orders = result.scalars().all()

            if not orders:
                return

            logger.debug("OrderPoller: %d 件の未解決注文を照会", len(orders))

            broker = _get_broker()
            broker_logger = BrokerCallLogger(db)
            audit = AuditLogger(db)

            for order in orders:
                try:
                    await self._process_order(db, order, broker, broker_logger, audit)
                except Exception as e:
                    logger.error(
                        "OrderPoller: 注文処理エラー order_id=%s error=%s",
                        order.id, e, exc_info=True,
                    )

    async def _process_order(
        self,
        db: AsyncSession,
        order: Order,
        broker,
        broker_logger: BrokerCallLogger,
        audit: AuditLogger,
    ) -> None:
        """1件の注文をブローカーに照会して状態を更新する。"""
        # ─── broker_order_id なし → BrokerAPIError タイムアウト由来の SUBMITTED ──
        # get_order_status(None) を呼ぶと broker の undefined behavior になるためガード。
        # 長時間経過後に UNKNOWN へ遷移させ手動確認に委ねる。
        if not order.broker_order_id:
            await self._handle_no_broker_id(db, order, audit)
            await db.commit()
            return

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
                "OrderPoller: 状態照会エラー order_id=%s broker_order_id=%s error=%s",
                order.id, order.broker_order_id, e,
            )
            await db.commit()
            return

        prev_status = order.status
        broker_status = status_resp.status

        # ─── 状態変化なし → 長時間チェック ──────────────────────────────
        if broker_status.value == prev_status:
            await self._check_stuck_order(db, order, audit)
            await db.commit()
            return

        now = datetime.now(timezone.utc)
        logger.info(
            "OrderPoller: 状態変化 order_id=%s is_exit=%s %s → %s",
            order.id, order.is_exit_order, prev_status, broker_status.value,
        )

        # ─── FILLED（全量約定）────────────────────────────────────────────
        if broker_status == OrderStatus.FILLED:
            await self._handle_filled(db, order, status_resp, audit, now)

        # ─── PARTIAL（部分約定）──────────────────────────────────────────
        elif broker_status == OrderStatus.PARTIAL:
            await self._handle_partial(db, order, status_resp, audit, now)

        # ─── CANCELLED ────────────────────────────────────────────────────
        elif broker_status == OrderStatus.CANCELLED:
            order.status = OrderStatus.CANCELLED.value
            order.updated_at = now
            await record_transition(
                db=db,
                order_id=order.id,
                from_status=prev_status,
                to_status=OrderStatus.CANCELLED.value,
                reason="ブローカーによりキャンセル",
                triggered_by="poller",
            )
            await audit.log(
                event_type=AuditEventType.ORDER_CANCELLED,
                entity_type="order",
                entity_id=order.id,
                details={"broker_order_id": order.broker_order_id,
                         "is_exit_order": order.is_exit_order},
                message=f"注文キャンセル: {order.ticker}",
            )
            # exit注文がキャンセルされた場合はポジションを OPEN に戻す
            if order.is_exit_order and order.position_id:
                await self._revert_closing_position(db, order.position_id, "exit注文キャンセル")

        # ─── REJECTED ─────────────────────────────────────────────────────
        elif broker_status == OrderStatus.REJECTED:
            order.status = OrderStatus.REJECTED.value
            order.updated_at = now
            await record_transition(
                db=db,
                order_id=order.id,
                from_status=prev_status,
                to_status=OrderStatus.REJECTED.value,
                reason=f"ブローカー拒否: {status_resp.message or ''}",
                triggered_by="poller",
            )
            await audit.log(
                event_type=AuditEventType.ORDER_FAILED,
                entity_type="order",
                entity_id=order.id,
                details={"message": status_resp.message,
                         "is_exit_order": order.is_exit_order},
                message=f"発注拒否: {order.ticker} {status_resp.message}",
            )
            # exit注文が拒否された場合はポジションを OPEN に戻す
            if order.is_exit_order and order.position_id:
                await self._revert_closing_position(db, order.position_id, "exit注文拒否")

        # ─── UNKNOWN（状態不明）──────────────────────────────────────────
        elif broker_status == OrderStatus.UNKNOWN:
            order.status = OrderStatus.UNKNOWN.value
            order.updated_at = now
            await record_transition(
                db=db,
                order_id=order.id,
                from_status=prev_status,
                to_status=OrderStatus.UNKNOWN.value,
                reason="ブローカーが状態を返さなかった",
                triggered_by="poller",
            )
            logger.warning(
                "OrderPoller: UNKNOWN 状態 order_id=%s ticker=%s — 手動確認が必要",
                order.id, order.ticker,
            )

        await db.commit()

    async def _handle_filled(
        self, db: AsyncSession, order: Order, status_resp, audit: AuditLogger, now: datetime
    ) -> None:
        """全量約定時の処理"""
        if order.is_exit_order:
            await self._handle_exit_filled(db, order, status_resp, audit, now)
        else:
            await self._handle_entry_filled(db, order, status_resp, audit, now)

    async def _handle_entry_filled(
        self, db: AsyncSession, order: Order, status_resp, audit: AuditLogger, now: datetime
    ) -> None:
        """通常注文（エントリー）の全量約定処理: Execution 記録 → Order 更新 → ポジション開設"""
        from trade_app.services.position_manager import PositionManager
        from sqlalchemy import select as sa_select
        from trade_app.models.signal import TradeSignal

        # ─── Execution 重複防止チェック ──────────────────────────────────
        broker_exec_id = status_resp.broker_execution_id
        if broker_exec_id:
            existing = await db.execute(
                sa_select(Execution).where(
                    Execution.broker_execution_id == broker_exec_id
                )
            )
            if existing.scalar_one_or_none():
                logger.warning(
                    "OrderPoller: Execution 重複スキップ broker_exec_id=%s order_id=%s",
                    broker_exec_id, order.id,
                )
                # Order が FILLED でない場合のみ更新（commit はする）
                if order.status != OrderStatus.FILLED.value:
                    order.status = OrderStatus.FILLED.value
                    order.filled_quantity = status_resp.filled_quantity or order.quantity
                    order.filled_price = status_resp.filled_price
                    order.filled_at = now
                    order.updated_at = now
                return

        # delta 計算（PARTIAL → FILLED の場合も正確に）
        total_filled = status_resp.filled_quantity or order.quantity
        delta_qty = total_filled - (order.filled_quantity or 0)

        # ─── Execution 記録（delta > 0 の場合のみ）───────────────────────
        if delta_qty > 0:
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

        # ─── Order を FILLED に更新 ───────────────────────────────────────
        prev_status = order.status
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
            reason="全量約定確認",
            triggered_by="poller",
        )
        await db.flush()

        # ─── ポジション開設（シグナルを参照）────────────────────────────
        signal = None
        if order.signal_id:
            signal_result = await db.execute(
                sa_select(TradeSignal).where(TradeSignal.id == order.signal_id)
            )
            signal = signal_result.scalar_one_or_none()
        if signal:
            pos_manager = PositionManager(db=db, audit=audit)
            await pos_manager.open_position(order=order, signal=signal)

        await audit.log(
            event_type=AuditEventType.ORDER_FILLED,
            entity_type="order",
            entity_id=order.id,
            details={
                "broker_order_id": order.broker_order_id,
                "ticker": order.ticker,
                "filled_qty": order.filled_quantity,
                "filled_price": order.filled_price,
            },
            message=(
                f"約定完了: {order.ticker} {order.side} "
                f"{order.filled_quantity}株 @ {order.filled_price}円"
            ),
        )
        logger.info(
            "OrderPoller: 約定完了 order_id=%s ticker=%s qty=%d price=%s",
            order.id, order.ticker, order.filled_quantity, order.filled_price,
        )

    async def _handle_exit_filled(
        self, db: AsyncSession, order: Order, status_resp, audit: AuditLogger, now: datetime
    ) -> None:
        """exit注文の全量約定処理: Execution 記録 → ポジションクローズ"""
        from trade_app.services.position_manager import PositionManager
        from trade_app.models.position import Position
        from sqlalchemy import select as sa_select

        # ─── Execution 重複防止チェック ──────────────────────────────────
        broker_exec_id = status_resp.broker_execution_id
        if broker_exec_id:
            existing = await db.execute(
                sa_select(Execution).where(
                    Execution.broker_execution_id == broker_exec_id
                )
            )
            if existing.scalar_one_or_none():
                logger.warning(
                    "OrderPoller: exit Execution 重複スキップ broker_exec_id=%s order_id=%s",
                    broker_exec_id, order.id,
                )
                if order.status != OrderStatus.FILLED.value:
                    order.status = OrderStatus.FILLED.value
                    order.filled_quantity = status_resp.filled_quantity or order.quantity
                    order.filled_price = status_resp.filled_price
                    order.filled_at = now
                    order.updated_at = now
                return

        # ─── delta 数量を計算（PARTIAL → FILLED の場合も正確に） ─────────
        total_filled = status_resp.filled_quantity or order.quantity
        delta_qty = total_filled - (order.filled_quantity or 0)

        # ─── Execution 記録（delta > 0 の場合のみ）───────────────────────
        if delta_qty > 0:
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

        # ─── Order を FILLED に更新 ───────────────────────────────────────
        prev_status = order.status
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
            reason="exit注文 全量約定確認",
            triggered_by="poller",
        )
        await db.flush()

        # ─── ポジションクローズ ───────────────────────────────────────────
        if not order.position_id:
            logger.error(
                "OrderPoller: exit注文に position_id がない order_id=%s", order.id
            )
            return

        pos_result = await db.execute(
            sa_select(Position).where(Position.id == order.position_id)
        )
        position = pos_result.scalar_one_or_none()
        if not position:
            logger.error(
                "OrderPoller: position が見つかりません position_id=%s", order.position_id
            )
            return

        from trade_app.models.enums import PositionStatus
        if position.status != PositionStatus.CLOSING.value:
            logger.warning(
                "OrderPoller: position が CLOSING でない pos=%s status=%s — スキップ",
                position.id[:8], position.status,
            )
            return

        pos_manager = PositionManager(db=db, audit=audit)

        # apply_exit_execution で remaining_qty を更新（過剰約定防御込み）
        if delta_qty > 0:
            is_flat = await pos_manager.apply_exit_execution(
                position=position,
                executed_qty=delta_qty,
                executed_price=status_resp.filled_price or 0.0,
            )
        else:
            # delta == 0 (重複 FILLED 通知など): remaining_qty を確認するだけ
            is_flat = (position.remaining_qty is not None and position.remaining_qty == 0)

        if is_flat:
            await pos_manager.finalize_exit(position=position, exit_order=order)
            logger.info(
                "OrderPoller: exit約定完了 → ポジションクローズ pos=%s ticker=%s price=%.0f",
                position.id[:8], position.ticker, order.filled_price or 0,
            )
        else:
            logger.info(
                "OrderPoller: exit約定完了（フラットではない） pos=%s remaining=%s",
                position.id[:8], position.remaining_qty,
            )

    async def _handle_partial(
        self, db: AsyncSession, order: Order, status_resp, audit: AuditLogger, now: datetime
    ) -> None:
        """部分約定時の処理: Execution 記録 → Order を PARTIAL に更新"""
        broker_exec_id = status_resp.broker_execution_id
        filled_qty = status_resp.filled_quantity or 0
        prev_filled = order.filled_quantity or 0
        new_qty = filled_qty - prev_filled  # 今回新たに約定した数量

        if new_qty > 0:
            # ─── Execution 重複防止チェック ──────────────────────────────
            if broker_exec_id:
                from sqlalchemy import select as sa_select
                existing = await db.execute(
                    sa_select(Execution).where(
                        Execution.broker_execution_id == broker_exec_id
                    )
                )
                if existing.scalar_one_or_none():
                    logger.warning(
                        "OrderPoller: partial Execution 重複スキップ broker_exec_id=%s",
                        broker_exec_id,
                    )
                    new_qty = 0  # 新規 Execution を作成しない

            if new_qty > 0:
                execution = Execution(
                    order_id=order.id,
                    broker_execution_id=broker_exec_id,
                    ticker=order.ticker,
                    side=order.side,
                    quantity=new_qty,
                    price=status_resp.filled_price or 0.0,
                    executed_at=now,
                    created_at=now,
                )
                db.add(execution)

        prev_status = order.status
        order.status = OrderStatus.PARTIAL.value
        order.filled_quantity = filled_qty
        order.filled_price = status_resp.filled_price
        order.updated_at = now

        if prev_status != OrderStatus.PARTIAL.value:
            await record_transition(
                db=db,
                order_id=order.id,
                from_status=prev_status,
                to_status=OrderStatus.PARTIAL.value,
                reason=f"部分約定: {filled_qty}/{order.quantity}株",
                triggered_by="poller",
            )
        logger.info(
            "OrderPoller: 部分約定 order_id=%s %d/%d株",
            order.id, filled_qty, order.quantity,
        )

        # ─── exit 注文の PARTIAL: remaining_qty を更新 ────────────────────
        if new_qty > 0 and order.is_exit_order and order.position_id:
            from trade_app.models.position import Position as _Position
            from trade_app.models.enums import PositionStatus as _PS
            from trade_app.services.position_manager import PositionManager
            from sqlalchemy import select as sa_select

            pos_result = await db.execute(
                sa_select(_Position).where(_Position.id == order.position_id)
            )
            position = pos_result.scalar_one_or_none()
            if position and position.status == _PS.CLOSING.value:
                audit = AuditLogger(db)
                pos_mgr = PositionManager(db=db, audit=audit)
                await pos_mgr.apply_exit_execution(
                    position=position,
                    executed_qty=new_qty,
                    executed_price=status_resp.filled_price or 0.0,
                )
                # remaining_qty > 0 なので CLOSING 維持（finalize_exit は呼ばない）
                logger.info(
                    "OrderPoller: exit PARTIAL → remaining_qty=%d pos=%s",
                    position.remaining_qty, position.id[:8],
                )

    async def _revert_closing_position(
        self, db: AsyncSession, position_id: str, reason: str
    ) -> None:
        """CLOSING 状態のポジションを OPEN に戻す（exit注文キャンセル・拒否時）。
        Position の更新は PositionManager.revert_to_open() に委譲する。"""
        from trade_app.models.position import Position
        from trade_app.models.enums import PositionStatus
        from trade_app.services.position_manager import PositionManager
        from trade_app.services.audit_logger import AuditLogger
        from sqlalchemy import select as sa_select

        result = await db.execute(
            sa_select(Position).where(Position.id == position_id)
        )
        position = result.scalar_one_or_none()
        if not position:
            return

        if position.status != PositionStatus.CLOSING.value:
            return

        audit = AuditLogger(db)
        pos_manager = PositionManager(db=db, audit=audit)
        await pos_manager.revert_to_open(
            position=position,
            reason=reason,
            triggered_by="poller",
        )

    async def _handle_no_broker_id(
        self, db: AsyncSession, order: Order, audit: AuditLogger
    ) -> None:
        """
        broker_order_id が NULL のまま SUBMITTED の注文を処理する。

        BrokerAPIError（タイムアウト等）で発注到達可否が不確定なまま
        SUBMITTED に残った注文。broker に照会できないため:
          - STUCK_ORDER_SEC 未満: 何もしない（次サイクルで再確認）
          - STUCK_ORDER_SEC 超過: UNKNOWN へ遷移（手動確認扱い）
        """
        ref_time = order.submitted_at or order.created_at
        elapsed = (datetime.now(timezone.utc) - ref_time).total_seconds()

        if elapsed < STUCK_ORDER_SEC:
            logger.debug(
                "OrderPoller: broker_order_id なし SUBMITTED をスキップ "
                "order_id=%s elapsed=%.0fs (limit=%ds)",
                order.id, elapsed, STUCK_ORDER_SEC,
            )
            return

        logger.warning(
            "OrderPoller: broker_order_id なし SUBMITTED が長時間経過 "
            "order_id=%s ticker=%s elapsed=%.0fs → UNKNOWN",
            order.id, order.ticker, elapsed,
        )
        prev_status = order.status
        order.status = OrderStatus.UNKNOWN.value
        order.updated_at = datetime.now(timezone.utc)

        await record_transition(
            db=db,
            order_id=order.id,
            from_status=prev_status,
            to_status=OrderStatus.UNKNOWN.value,
            reason=f"broker_order_id 不明のまま {elapsed:.0f}秒経過（タイムアウト由来の SUBMITTED）",
            triggered_by="poller",
        )
        await audit.log(
            event_type=AuditEventType.ORDER_FAILED,
            entity_type="order",
            entity_id=order.id,
            details={
                "ticker": order.ticker,
                "elapsed_sec": elapsed,
                "reason": "broker_order_id 不明の SUBMITTED が長時間未解決",
            },
            message=(
                f"broker_order_id 不明の SUBMITTED 注文を UNKNOWN に遷移: "
                f"{order.ticker} order={order.id[:8]}"
            ),
        )
        await self._record_system_event(
            SystemEventType.ORDER_STUCK,
            details={
                "order_id": order.id,
                "ticker": order.ticker,
                "elapsed_sec": elapsed,
                "reason": "no_broker_order_id",
            },
            message=(
                f"broker_order_id なし SUBMITTED を UNKNOWN に遷移: "
                f"{order.ticker} order={order.id[:8]}"
            ),
        )

    async def _check_stuck_order(
        self, db: AsyncSession, order: Order, audit: AuditLogger
    ) -> None:
        """長時間 SUBMITTED のまま変化がない注文を UNKNOWN に遷移させる。"""
        if order.submitted_at is None:
            return
        elapsed = (datetime.now(timezone.utc) - order.submitted_at).total_seconds()
        if elapsed < STUCK_ORDER_SEC:
            return

        logger.warning(
            "OrderPoller: 長時間未解決注文を検出 order_id=%s ticker=%s elapsed=%.0fs",
            order.id, order.ticker, elapsed,
        )
        prev_status = order.status
        order.status = OrderStatus.UNKNOWN.value
        order.updated_at = datetime.now(timezone.utc)

        await record_transition(
            db=db,
            order_id=order.id,
            from_status=prev_status,
            to_status=OrderStatus.UNKNOWN.value,
            reason=f"長時間未解決: {elapsed:.0f}秒経過",
            triggered_by="poller",
        )
        await self._record_system_event(
            SystemEventType.ORDER_STUCK,
            details={"order_id": order.id, "ticker": order.ticker, "elapsed_sec": elapsed},
            message=f"長時間未解決注文 UNKNOWN へ遷移: {order.ticker} order={order.id[:8]}",
        )

    @staticmethod
    async def _record_system_event(
        event_type: SystemEventType,
        details: dict | None = None,
        message: str = "",
    ) -> None:
        """システムイベントを DB に記録する（独立セッション）"""
        try:
            async with AsyncSessionLocal() as db:
                event = SystemEvent(
                    event_type=event_type.value,
                    details=details,
                    message=message,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(event)
                await db.commit()
        except Exception as e:
            logger.error("システムイベント記録エラー: %s", e)
