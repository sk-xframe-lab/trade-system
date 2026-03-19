"""
OrderRouter サービス
シグナルをブローカーへの発注に変換し、二重発注を防止する。

二重発注防止の仕組み:
  - Redis の分散ロック（TTL=60秒）を使用
  - 同一シグナルに対する並行発注リクエストをブロック
  - ロック取得失敗 = 別プロセスが既に処理中 → 無視して返る
"""
import logging
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.brokers.base import BrokerAdapter, BrokerAPIError, BrokerOrderError, OrderRequest
from trade_app.models.enums import (
    AuditEventType,
    OrderStatus,
    OrderType,
    Side,
    SignalStatus,
)
from trade_app.models.order import Order
from trade_app.models.order_state_transition import record_transition
from trade_app.models.signal import TradeSignal
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.broker_call_logger import BrokerCallLogger

logger = logging.getLogger(__name__)

# 発注ロックの TTL（秒）。この時間が経過すればロックは自動解放
_ORDER_LOCK_TTL_SEC = 60
# ロックキーのプレフィックス
_LOCK_KEY_PREFIX = "order_lock:"


class OrderAlreadyInProgressError(Exception):
    """同一シグナルに対して既に発注処理が進行中の場合に送出"""
    pass


class OrderRouter:
    """
    発注ルーティングサービス。
    シグナル → Order 作成 → ブローカー送信 → 結果を DB に反映する。
    """

    def __init__(
        self,
        db: AsyncSession,
        broker: BrokerAdapter,
        redis_client: redis.Redis,
        audit: AuditLogger,
        broker_logger: BrokerCallLogger | None = None,
    ) -> None:
        self._db = db
        self._broker = broker
        self._redis = redis_client
        self._audit = audit
        self._broker_logger = broker_logger

    async def route(self, signal: TradeSignal, planned_qty: int | None = None) -> Order:
        """
        シグナルをブローカーへ発注する。

        処理フロー:
          1. Redis 分散ロックを取得（二重発注防止）
          2. Order レコードを PENDING 状態で DB に保存
          3. シグナルを PROCESSING 状態に更新
          4. BrokerAdapter.place_order() を呼び出し
          5. 結果（broker_order_id / status）を Order に反映
          6. 監査ログを記録
          7. ロックを解放

        Args:
            signal: 発注対象のシグナル（RECEIVED 状態のもの）
            planned_qty: Planning Layer が算出した発注数量（None の場合は signal.quantity を使用）

        Returns:
            作成・更新した Order オブジェクト

        Raises:
            OrderAlreadyInProgressError: 同一シグナルが処理中
            BrokerAPIError: ブローカーとの通信エラー
            BrokerOrderError: ブローカーに発注拒否された
        """
        lock_key = f"{_LOCK_KEY_PREFIX}{signal.id}"

        # ─── Step 1: 分散ロック取得 ───────────────────────────────────────
        acquired = await self._acquire_lock(lock_key)
        if not acquired:
            logger.warning("発注ロック取得失敗: signal_id=%s（別プロセスが処理中）", signal.id)
            raise OrderAlreadyInProgressError(
                f"シグナル {signal.id} の発注処理が既に進行中です"
            )

        try:
            return await self._do_route(signal, planned_qty=planned_qty)
        finally:
            # 成功・失敗に関わらずロックを解放
            await self._release_lock(lock_key)

    async def _do_route(self, signal: TradeSignal, planned_qty: int | None = None) -> Order:
        """実際の発注処理（ロック取得後に呼ばれる）"""
        qty = planned_qty if planned_qty is not None else signal.quantity

        # ─── Step 2: Order を PENDING で DB に保存 ───────────────────────
        order = Order(
            signal_id=signal.id,
            ticker=signal.ticker,
            order_type=signal.order_type,
            side=signal.side,
            quantity=qty,
            limit_price=signal.limit_price,
            status=OrderStatus.PENDING.value,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self._db.add(order)
        await self._db.flush()  # order.id を確定

        # PENDING 遷移を記録（from_status=None = 初回作成）
        await record_transition(
            db=self._db,
            order_id=order.id,
            from_status=None,
            to_status=OrderStatus.PENDING.value,
            reason="新規注文作成",
            triggered_by="pipeline",
        )

        # ─── Step 3: シグナルを PROCESSING に更新 ────────────────────────
        signal.status = SignalStatus.PROCESSING.value
        await self._db.flush()

        # ─── Step 4: ブローカーへ発注 ─────────────────────────────────────
        broker_request_obj = OrderRequest(
            client_order_id=order.id,
            ticker=order.ticker,
            order_type=OrderType(order.order_type),
            side=Side(order.side),
            quantity=order.quantity,
            limit_price=order.limit_price,
        )

        # 発注前にログを記録（BrokerCallLogger が設定されている場合）
        br_log = None
        if self._broker_logger:
            br_log = await self._broker_logger.before_place_order(
                order_id=order.id,
                request=broker_request_obj,
            )
            await self._db.flush()

        try:
            logger.info(
                "発注送信: order_id=%s ticker=%s side=%s qty=%d type=%s",
                order.id, order.ticker, order.side, order.quantity, order.order_type,
            )
            broker_response = await self._broker.place_order(broker_request_obj)

            # 発注成功レスポンスを記録
            if self._broker_logger and br_log:
                await self._broker_logger.after_place_order(
                    broker_request=br_log,
                    order_id=order.id,
                    response=broker_response,
                )

        except BrokerAPIError as e:
            # 通信エラー → 発注の到達可否が不確定。Order を SUBMITTED のまま残し
            # RecoveryManager の起動時リカバリで再照会させる。
            # broker_order_id は不明（None のまま）。
            if self._broker_logger and br_log:
                await self._broker_logger.on_error(
                    broker_request=br_log, order_id=order.id, error=e
                )
            order.status = OrderStatus.SUBMITTED.value
            order.updated_at = datetime.now(timezone.utc)
            # signal は PROCESSING のまま維持（OrderPoller/RecoveryManager が完了させる）
            await record_transition(
                db=self._db,
                order_id=order.id,
                from_status=OrderStatus.PENDING.value,
                to_status=OrderStatus.SUBMITTED.value,
                reason=f"ブローカー通信エラー（不確定）: {e}",
                triggered_by="pipeline",
            )
            await self._audit.log(
                event_type=AuditEventType.ORDER_FAILED,
                entity_type="order",
                entity_id=order.id,
                details={"error": str(e), "ticker": order.ticker},
                message=f"ブローカー通信エラー（不確定・RecoveryManager で再照会）: {e}",
            )
            await self._db.commit()
            raise

        except BrokerOrderError as e:
            # 発注拒否 → Order を REJECTED に、Signal を FAILED に
            if self._broker_logger and br_log:
                await self._broker_logger.on_error(
                    broker_request=br_log, order_id=order.id, error=e
                )
            order.status = OrderStatus.REJECTED.value
            order.updated_at = datetime.now(timezone.utc)
            signal.status = SignalStatus.FAILED.value
            signal.reject_reason = str(e)
            await record_transition(
                db=self._db,
                order_id=order.id,
                from_status=OrderStatus.PENDING.value,
                to_status=OrderStatus.REJECTED.value,
                reason=f"発注拒否: {e}",
                triggered_by="pipeline",
            )
            await self._audit.log(
                event_type=AuditEventType.ORDER_FAILED,
                entity_type="order",
                entity_id=order.id,
                details={"error": str(e), "ticker": order.ticker},
                message=f"発注拒否: {e}",
            )
            await self._db.commit()
            raise

        # ─── Step 5: ブローカーレスポンスを Order に反映 ──────────────────
        order.broker_order_id = broker_response.broker_order_id
        order.status = broker_response.status.value
        order.submitted_at = datetime.now(timezone.utc)
        order.updated_at = datetime.now(timezone.utc)

        # PENDING → SUBMITTED 遷移を記録
        await record_transition(
            db=self._db,
            order_id=order.id,
            from_status=OrderStatus.PENDING.value,
            to_status=broker_response.status.value,
            reason="ブローカーへの発注送信完了",
            triggered_by="pipeline",
        )

        # ─── Step 6: 監査ログ ─────────────────────────────────────────────
        await self._audit.log(
            event_type=AuditEventType.ORDER_SUBMITTED,
            entity_type="order",
            entity_id=order.id,
            details={
                "broker_order_id": order.broker_order_id,
                "ticker": order.ticker,
                "side": order.side,
                "quantity": order.quantity,
                "limit_price": order.limit_price,
                "broker": self._broker.name,
            },
            message=(
                f"発注完了: {order.ticker} {order.side} {order.quantity}株 "
                f"broker_order_id={order.broker_order_id}"
            ),
        )

        await self._db.commit()
        await self._db.refresh(order)

        logger.info(
            "発注完了: order_id=%s broker_order_id=%s status=%s",
            order.id, order.broker_order_id, order.status,
        )
        return order

    # ─── Redis 分散ロック ──────────────────────────────────────────────────

    async def _acquire_lock(self, lock_key: str) -> bool:
        """
        Redis の SET NX EX を使って分散ロックを取得する。
        Returns:
            True: ロック取得成功
            False: 既にロックされている
        """
        try:
            result = await self._redis.set(
                lock_key, "1", nx=True, ex=_ORDER_LOCK_TTL_SEC
            )
            return result is not None
        except Exception as e:
            logger.error("Redis ロック取得エラー（発注続行）: %s", e)
            # Redis 障害時は楽観的に続行（DB の冪等性で保護）
            return True

    async def _release_lock(self, lock_key: str) -> None:
        """Redis のロックを解放する"""
        try:
            await self._redis.delete(lock_key)
        except Exception as e:
            logger.error("Redis ロック解放エラー: %s", e)
