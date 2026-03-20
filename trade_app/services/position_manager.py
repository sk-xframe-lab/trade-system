"""
PositionManager サービス
約定済み注文からポジションを生成し、クローズまで管理する。

Phase 3 で追加されたクローズフロー:
  open_position()         : 約定済み注文からポジションを生成（既存・変更なし）
  initiate_exit()         : ExitWatcher が呼び出す。CLOSING 状態へ遷移 + exit注文を発行
  apply_exit_execution()  : exit Execution ごとに remaining_qty を減算。0 になれば True を返す
  finalize_exit()         : remaining_qty == 0 確認後に CLOSED へ遷移
  close_position()        : 即時クローズ（価格が既知の場合のみ。既存）
  revert_to_open()        : exit 注文キャンセル・拒否時に CLOSING → OPEN へ巻き戻し

状態遷移:
  OPEN → CLOSING（initiate_exit: exit注文送信済み、remaining_qty = quantity で初期化）
  CLOSING (remaining_qty > 0) → apply_exit_execution → CLOSING 維持
  CLOSING (remaining_qty == 0) → finalize_exit → CLOSED
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.enums import (
    AuditEventType,
    ExitReason,
    OrderStatus,
    OrderType,
    PositionStatus,
    SignalStatus,
    Side,
)
from trade_app.models.execution import Execution
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.position_exit_transition import record_exit_transition
from trade_app.models.signal import TradeSignal
from trade_app.models.trade_result import TradeResult
from trade_app.services.audit_logger import AuditLogger

logger = logging.getLogger(__name__)


class PositionManager:
    """
    ポジション生成・更新・クローズを管理するサービス。
    """

    def __init__(self, db: AsyncSession, audit: AuditLogger) -> None:
        self._db = db
        self._audit = audit

    # ─── ポジション開設 ────────────────────────────────────────────────────

    async def open_position(
        self,
        order: Order,
        signal: TradeSignal,
        tp_price: float | None = None,
        sl_price: float | None = None,
        exit_deadline: datetime | None = None,
    ) -> Position:
        """
        約定済み注文からポジションを生成する。

        Args:
            order        : 約定済み Order オブジェクト（status=FILLED）
            signal       : 元になったシグナル（TP/SL情報を取得する場合がある）
            tp_price     : 利確価格（None の場合は ExitWatcher が判断）
            sl_price     : 損切価格（None の場合は ExitWatcher が判断）
            exit_deadline: 強制決済期限（None の場合は当日大引け前）

        Returns:
            生成した Position オブジェクト

        Raises:
            ValueError: order が約定済みでない場合
        """
        if order.status != OrderStatus.FILLED.value:
            raise ValueError(
                f"Order {order.id} は未約定（status={order.status}）のためポジション生成不可"
            )

        entry_price = order.filled_price or 0.0

        position = Position(
            order_id=order.id,
            ticker=order.ticker,
            side=order.side,
            quantity=order.filled_quantity,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
            exit_deadline=exit_deadline,
            status=PositionStatus.OPEN.value,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        self._db.add(position)

        # シグナルを EXECUTED に更新
        signal.status = SignalStatus.EXECUTED.value

        await self._db.flush()

        await self._audit.log(
            event_type=AuditEventType.POSITION_OPENED,
            entity_type="position",
            entity_id=position.id,
            details={
                "ticker": position.ticker,
                "side": position.side,
                "quantity": position.quantity,
                "entry_price": entry_price,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "order_id": order.id,
            },
            message=(
                f"ポジション開設: {position.ticker} {position.side} "
                f"{position.quantity}株 @ {entry_price:.0f}円"
            ),
        )

        await self._db.commit()
        await self._db.refresh(position)

        logger.info(
            "ポジション開設: id=%s ticker=%s side=%s qty=%d entry=%.0f",
            position.id, position.ticker, position.side,
            position.quantity, entry_price,
        )
        return position

    # ─── exit 開始（OPEN → CLOSING）───────────────────────────────────────

    async def initiate_exit(
        self,
        position: Position,
        exit_reason: ExitReason,
        broker,
        triggered_by: str = "watcher",
    ) -> "Order | None":
        """
        ExitWatcher が呼び出す。exit注文を発行し、ポジションを CLOSING 状態に遷移させる。

        フロー:
          1. Atomic UPDATE: status='OPEN' の行のみ CLOSING に遷移（競合時は None を返す）
          2. PositionExitTransition を OPEN→CLOSING で記録
          3. 逆方向の exit 注文（成行）を作成・ブローカーへ送信
          4. exit Order を DB に保存（position_id FK + is_exit_order=True）

        Args:
            position    : 対象ポジション（status=OPEN）
            exit_reason : クローズ理由
            broker      : BrokerAdapter（exit注文送信に使用）
            triggered_by: 操作者識別（watcher / manual）

        Returns:
            送信した exit Order。CLOSING への遷移が競合した場合は None。

        Raises:
            ValueError: position が OPEN 状態でない場合（事前チェック）
        """
        if position.status != PositionStatus.OPEN.value:
            raise ValueError(
                f"Position {position.id} は OPEN でない（status={position.status}）"
            )

        from trade_app.brokers.base import OrderRequest
        from trade_app.services.broker_call_logger import BrokerCallLogger

        now = datetime.now(timezone.utc)

        # ─── Atomic OPEN→CLOSING 遷移（競合検出）────────────────────────
        # WHERE status='OPEN' を原子的にチェック＆更新する。
        # workers >= 2 や非同期 race condition で同一ポジションへの
        # 二重 initiate_exit を DB レベルで防止する。
        update_stmt = (
            update(Position)
            .where(Position.id == position.id, Position.status == PositionStatus.OPEN.value)
            .values(
                status=PositionStatus.CLOSING.value,
                exit_reason=exit_reason.value,
                remaining_qty=position.quantity,
                updated_at=now,
            )
            .returning(Position.id)
        )
        update_result = await self._db.execute(update_stmt)
        updated_row = update_result.fetchone()
        if updated_row is None:
            logger.warning(
                "initiate_exit: 状態変更競合（既に CLOSING/CLOSED）pos=%s — スキップ",
                position.id[:8],
            )
            return None

        # Python オブジェクトを DB の状態に同期（stale 回避）
        prev_status = position.status
        position.status = PositionStatus.CLOSING.value
        position.exit_reason = exit_reason.value
        position.remaining_qty = position.quantity   # Execution 駆動で減算していく
        position.updated_at = now

        # ─── exit 注文を作成（逆方向の成行注文）───────────────────────────
        exit_side = Side.SELL if position.side == "buy" else Side.BUY
        exit_order = Order(
            signal_id=None,           # exit注文はシグナルなし
            position_id=position.id,  # どのポジションを閉じるかを記録
            is_exit_order=True,
            ticker=position.ticker,
            order_type=OrderType.MARKET.value,
            side=exit_side.value,
            quantity=position.quantity,
            limit_price=None,
            status=OrderStatus.PENDING.value,
            filled_quantity=0,
            created_at=now,
            updated_at=now,
        )
        self._db.add(exit_order)

        # ─── PositionExitTransition を記録 ────────────────────────────────
        await record_exit_transition(
            db=self._db,
            position_id=position.id,
            from_status=prev_status,
            to_status=PositionStatus.CLOSING.value,
            exit_reason=exit_reason.value,
            triggered_by=triggered_by,
            details={"exit_order_pending": True},
        )

        await self._db.flush()  # exit_order.id を確定

        # ─── ブローカーへ exit 注文を送信 ──────────────────────────────────
        broker_logger = BrokerCallLogger(self._db)

        request = OrderRequest(
            client_order_id=exit_order.id,
            ticker=exit_order.ticker,
            order_type=OrderType.MARKET,
            side=exit_side,
            quantity=exit_order.quantity,
        )

        br_log = await broker_logger.before_place_order(
            order_id=exit_order.id,
            request=request,
        )
        await self._db.flush()

        try:
            response = await broker.place_order(request)
            await broker_logger.after_place_order(
                broker_request=br_log,
                order_id=exit_order.id,
                response=response,
            )

            exit_order.broker_order_id = response.broker_order_id
            exit_order.status = response.status.value
            exit_order.submitted_at = now
            exit_order.updated_at = now

        except Exception as e:
            await broker_logger.on_error(
                broker_request=br_log, order_id=exit_order.id, error=e
            )
            logger.error(
                "initiate_exit: ブローカー送信失敗 pos=%s error=%s",
                position.id[:8], e, exc_info=True,
            )
            # ポジションを OPEN に戻す（再試行できるようにする）
            position.status = PositionStatus.OPEN.value
            position.exit_reason = None
            position.updated_at = datetime.now(timezone.utc)
            raise

        await self._audit.log(
            event_type=AuditEventType.POSITION_CLOSING,
            entity_type="position",
            entity_id=position.id,
            details={
                "ticker": position.ticker,
                "exit_reason": exit_reason.value,
                "exit_order_id": exit_order.id,
                "broker_order_id": exit_order.broker_order_id,
                "triggered_by": triggered_by,
            },
            message=(
                f"ポジション決済開始: {position.ticker} "
                f"reason={exit_reason.value} by={triggered_by}"
            ),
        )

        await self._db.commit()
        await self._db.refresh(exit_order)

        logger.info(
            "initiate_exit: 完了 pos=%s ticker=%s reason=%s exit_order=%s",
            position.id[:8], position.ticker,
            exit_reason.value, exit_order.id[:8],
        )
        return exit_order

    # ─── exit 確定（CLOSING → CLOSED）────────────────────────────────────

    async def finalize_exit(
        self,
        position: Position,
        exit_order: Order,
    ) -> TradeResult:
        """
        OrderPoller が exit注文の約定を検出後に呼び出す。
        ポジションを CLOSED に遷移させ、確定損益を TradeResult に記録する。

        Args:
            position  : 対象ポジション（status=CLOSING）
            exit_order: 約定済みの exit Order（status=FILLED、is_exit_order=True）

        Returns:
            生成した TradeResult

        Raises:
            ValueError: position が CLOSING でない、または exit_order が FILLED でない場合
        """
        if position.status != PositionStatus.CLOSING.value:
            raise ValueError(
                f"Position {position.id} は CLOSING でない（status={position.status}）"
            )
        if exit_order.status != OrderStatus.FILLED.value:
            raise ValueError(
                f"Exit order {exit_order.id} は FILLED でない（status={exit_order.status}）"
            )

        exit_reason_str = position.exit_reason or ExitReason.MANUAL.value
        now = datetime.now(timezone.utc)

        # ─── exit_price: Execution 加重平均 → fallback: order.filled_price ─
        #
        # ブローカーが返す order.filled_price は仕様依存:
        #   - 累積加重平均を返すブローカー（立花証券 e_api は未確認）
        #   - 最終約定価格のみ返すブローカー
        # そのため、Execution レコードが存在する場合は
        # executions テーブルの加重平均を正本として使用する。
        # Execution が存在しない（broker_execution_id なし等）場合は
        # order.filled_price にフォールバックする。
        exit_price = await self._calc_weighted_exit_price(exit_order)

        # ─── 損益計算 ─────────────────────────────────────────────────────
        if position.side == "buy":
            pnl = (exit_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - exit_price) * position.quantity

        pnl_pct = (pnl / (position.entry_price * position.quantity)) * 100

        holding_minutes = None
        if position.opened_at:
            opened_at = position.opened_at
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            holding_minutes = int((now - opened_at).total_seconds() / 60)

        # ─── Position を CLOSED に更新 ────────────────────────────────────
        prev_status = position.status
        position.status = PositionStatus.CLOSED.value
        position.exit_price = exit_price
        position.realized_pnl = pnl
        position.closed_at = now
        position.updated_at = now

        # ─── PositionExitTransition を記録 ────────────────────────────────
        await record_exit_transition(
            db=self._db,
            position_id=position.id,
            from_status=prev_status,
            to_status=PositionStatus.CLOSED.value,
            exit_reason=exit_reason_str,
            triggered_by="poller",
            exit_order_id=exit_order.id,
            details={
                "exit_price": exit_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            },
        )

        # ─── TradeResult を保存 ───────────────────────────────────────────
        result = TradeResult(
            position_id=position.id,
            ticker=position.ticker,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_minutes=holding_minutes,
            exit_reason=exit_reason_str,
            created_at=now,
        )
        self._db.add(result)

        await self._db.flush()

        await self._audit.log(
            event_type=AuditEventType.POSITION_CLOSED,
            entity_type="position",
            entity_id=position.id,
            details={
                "ticker": position.ticker,
                "side": position.side,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "exit_reason": exit_reason_str,
                "holding_minutes": holding_minutes,
            },
            message=(
                f"ポジション決済完了: {position.ticker} {exit_reason_str} "
                f"PnL={pnl:+.0f}円 ({pnl_pct:+.1f}%)"
            ),
        )

        await self._db.commit()
        await self._db.refresh(result)

        logger.info(
            "finalize_exit: 完了 pos=%s ticker=%s pnl=%+.0f reason=%s",
            position.id[:8], position.ticker, pnl, exit_reason_str,
        )

        # ─── halt チェック（損失確定後）──────────────────────────────────
        from trade_app.services.halt_manager import HaltManager
        halt_mgr = HaltManager()
        new_halts = await halt_mgr.check_and_halt_if_needed(self._db)
        if new_halts:
            for h in new_halts:
                logger.warning(
                    "finalize_exit: halt 発動 type=%s reason=%s",
                    h.halt_type, h.reason,
                )

        return result

    # ─── 内部ヘルパー ─────────────────────────────────────────────────────

    async def _calc_weighted_exit_price(self, exit_order: Order) -> float:
        """
        exit 注文の約定価格（加重平均）を Execution テーブルから計算する。

        Execution レコードが存在する場合:
            Σ(price_i × qty_i) / Σ(qty_i) を返す
        Execution が存在しない場合:
            exit_order.filled_price にフォールバック（ブローカー報告値）

        設計背景:
            ブローカーが返す order.filled_price が累積加重平均なのか
            最終約定価格なのかは仕様依存。Execution を正本とすることで
            PARTIAL 2段階約定の場合も正確な加重平均 PnL 計算が可能。
        """
        result = await self._db.execute(
            select(Execution).where(Execution.order_id == exit_order.id)
        )
        executions = result.scalars().all()

        if not executions:
            # Execution 未記録（broker_execution_id なし等）はブローカー報告値にフォールバック
            return exit_order.filled_price or 0.0

        total_qty = sum(e.quantity for e in executions)
        if total_qty == 0:
            return exit_order.filled_price or 0.0

        weighted_price = sum(e.price * e.quantity for e in executions) / total_qty
        return weighted_price

    # ─── exit Execution 適用（PARTIAL 対応）────────────────────────────────

    async def apply_exit_execution(
        self,
        position: Position,
        executed_qty: int,
        executed_price: float,
    ) -> bool:
        """
        exit Execution の約定数量を Position.remaining_qty に反映する。

        PARTIAL 約定が積み重なって remaining_qty == 0 になった時点で True を返す。
        呼び出し元は戻り値が True の場合に finalize_exit() を呼び出すこと。

        Args:
            position      : 対象ポジション（status=CLOSING）
            executed_qty  : 今回新たに約定した数量（delta、累積ではない）
            executed_price: 今回の約定価格

        Returns:
            True : remaining_qty == 0（フラット → finalize_exit() を呼ぶべき）
            False: remaining_qty > 0（CLOSING 維持）

        Raises:
            ValueError: position が CLOSING でない場合
        """
        if position.status != PositionStatus.CLOSING.value:
            raise ValueError(
                f"Position {position.id} は CLOSING でない（status={position.status}）"
                " — apply_exit_execution は CLOSING 時のみ呼び出し可"
            )

        # remaining_qty が未初期化（initiate_exit 前に作られた既存 CLOSING ポジション）
        if position.remaining_qty is None:
            position.remaining_qty = position.quantity

        new_remaining = position.remaining_qty - executed_qty

        if new_remaining < 0:
            logger.error(
                "apply_exit_execution: 過剰約定検出 pos=%s remaining=%d executed_qty=%d"
                " — 0 にクランプ",
                position.id[:8], position.remaining_qty, executed_qty,
            )
            await self._audit.log(
                event_type=AuditEventType.POSITION_CLOSED,
                entity_type="position",
                entity_id=position.id,
                details={
                    "overfill": True,
                    "remaining_qty_before": position.remaining_qty,
                    "executed_qty": executed_qty,
                    "clamped_to": 0,
                    "executed_price": executed_price,
                },
                message=(
                    f"過剰約定検出: {position.ticker} "
                    f"remaining={position.remaining_qty} executed={executed_qty}"
                    " → 0 にクランプ"
                ),
            )
            new_remaining = 0

        position.remaining_qty = new_remaining
        position.updated_at = datetime.now(timezone.utc)
        await self._db.flush()

        logger.debug(
            "apply_exit_execution: pos=%s ticker=%s executed_qty=%d remaining=%d",
            position.id[:8], position.ticker, executed_qty, new_remaining,
        )
        return new_remaining == 0

    # ─── 即時クローズ（後方互換・価格既知の場合のみ）─────────────────────

    async def close_position(
        self,
        position: Position,
        exit_price: float,
        exit_reason: ExitReason,
    ) -> TradeResult:
        """
        ポジションをクローズし、確定損益を TradeResult に記録する。

        注意: Phase 3 以降は通常 initiate_exit → finalize_exit フローを使用する。
        このメソッドは価格が既知の場合（例: ブローカー外での決済）に限定して使用する。

        Args:
            position   : クローズ対象のポジション（status=OPEN）
            exit_price : 決済価格
            exit_reason: クローズ理由

        Returns:
            生成した TradeResult オブジェクト

        Raises:
            ValueError: position が OPEN 状態でない場合
        """
        if position.status != PositionStatus.OPEN.value:
            raise ValueError(
                f"Position {position.id} は OPEN でない（status={position.status}）"
            )

        # ─── 損益計算 ─────────────────────────────────────────────────────
        if position.side == "buy":
            pnl = (exit_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - exit_price) * position.quantity

        pnl_pct = (pnl / (position.entry_price * position.quantity)) * 100

        now = datetime.now(timezone.utc)
        holding_minutes = None
        if position.opened_at:
            opened_at = position.opened_at
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            holding_minutes = int((now - opened_at).total_seconds() / 60)

        # ─── Position を CLOSED に更新 ────────────────────────────────────
        prev_status = position.status
        position.status = PositionStatus.CLOSED.value
        position.exit_price = exit_price
        position.exit_reason = exit_reason.value
        position.realized_pnl = pnl
        position.closed_at = now
        position.updated_at = now

        # ─── PositionExitTransition を記録 ────────────────────────────────
        await record_exit_transition(
            db=self._db,
            position_id=position.id,
            from_status=prev_status,
            to_status=PositionStatus.CLOSED.value,
            exit_reason=exit_reason.value,
            triggered_by="system",
            details={"exit_price": exit_price, "pnl": pnl, "direct_close": True},
        )

        # ─── TradeResult を保存 ───────────────────────────────────────────
        result = TradeResult(
            position_id=position.id,
            ticker=position.ticker,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            holding_minutes=holding_minutes,
            exit_reason=exit_reason.value,
            created_at=now,
        )
        self._db.add(result)

        await self._db.flush()

        await self._audit.log(
            event_type=AuditEventType.POSITION_CLOSED,
            entity_type="position",
            entity_id=position.id,
            details={
                "ticker": position.ticker,
                "side": position.side,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "exit_reason": exit_reason.value,
                "holding_minutes": holding_minutes,
            },
            message=(
                f"ポジション決済: {position.ticker} {exit_reason.value} "
                f"PnL={pnl:+.0f}円 ({pnl_pct:+.1f}%)"
            ),
        )

        await self._db.commit()
        await self._db.refresh(result)

        logger.info(
            "close_position: 完了 id=%s ticker=%s pnl=%+.0f reason=%s",
            position.id[:8], position.ticker, pnl, exit_reason.value,
        )

        # ─── halt チェック（損失確定後）──────────────────────────────────
        from trade_app.services.halt_manager import HaltManager
        halt_mgr = HaltManager()
        await halt_mgr.check_and_halt_if_needed(self._db)

        return result

    # ─── CLOSING → OPEN 巻き戻し（exit注文キャンセル・拒否時）──────────────

    async def revert_to_open(
        self,
        position: Position,
        reason: str,
        triggered_by: str = "poller",
    ) -> None:
        """
        CLOSING 状態のポジションを OPEN に戻す。
        exit 注文が CANCELLED / REJECTED になった場合に OrderPoller が呼び出す。

        Args:
            position    : 対象ポジション（status=CLOSING）
            reason      : 巻き戻し理由（ログ・遷移記録用）
            triggered_by: 操作者識別

        Raises:
            ValueError: position が CLOSING でない場合
        """
        if position.status != PositionStatus.CLOSING.value:
            raise ValueError(
                f"Position {position.id} は CLOSING でない（status={position.status}）"
                " — revert_to_open は CLOSING 時のみ呼び出し可"
            )

        logger.warning(
            "revert_to_open: CLOSING → OPEN に巻き戻し pos=%s reason=%s",
            position.id[:8], reason,
        )

        prev_status = position.status
        position.status = PositionStatus.OPEN.value
        position.exit_reason = None
        position.updated_at = datetime.now(timezone.utc)

        await record_exit_transition(
            db=self._db,
            position_id=position.id,
            from_status=prev_status,
            to_status=PositionStatus.OPEN.value,
            exit_reason=None,
            triggered_by=triggered_by,
            details={"revert_reason": reason},
        )

    async def get_open_positions(self) -> list[Position]:
        """全オープンポジションを返す（ExitWatcher が定期的に呼び出す）"""
        result = await self._db.execute(
            select(Position).where(Position.status == PositionStatus.OPEN.value)
        )
        return list(result.scalars().all())

    async def update_unrealized_pnl(
        self, position: Position, current_price: float
    ) -> None:
        """
        現在価格を更新し、評価損益を再計算する。
        ExitWatcher が各サイクルで呼び出す。flush のみ（commit は呼び出し元が管理）。
        """
        position.current_price = current_price
        position.unrealized_pnl = position.calc_unrealized_pnl(current_price)
        position.updated_at = datetime.now(timezone.utc)
        await self._db.flush()
