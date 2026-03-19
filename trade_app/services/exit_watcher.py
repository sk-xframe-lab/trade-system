"""
ExitWatcher — ポジション出口監視ジョブ

OPEN 状態のポジションを定期的に監視し、
TP/SL/TimeStop のいずれかの条件を満たした場合に exit を開始する。

設計原則:
  - BrokerAdapter への依存は get_market_price() のみ
  - ExitPolicy リストは差し替え可能（DEFAULT_EXIT_POLICIES がデフォルト）
  - 将来、PriceSource を WebSocket フィードに差し替えても ExitWatcher 本体は変更不要
  - CLOSING 状態のポジションは再評価しない（exit注文が既に送信済みのため）

実行モデル:
  main.py の lifespan 内で asyncio.create_task() により
  バックグラウンドタスクとして起動する。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.brokers.base import BrokerAdapter
from trade_app.config import get_settings
from trade_app.models.database import AsyncSessionLocal
from trade_app.models.enums import ExitReason, PositionStatus, SystemEventType
from trade_app.models.position import Position
from trade_app.models.system_event import SystemEvent
from trade_app.services.audit_logger import AuditLogger
from trade_app.services.exit_policies import DEFAULT_EXIT_POLICIES, ExitPolicy
from trade_app.services.position_manager import PositionManager

logger = logging.getLogger(__name__)


def _get_broker() -> BrokerAdapter:
    """設定に応じてブローカーアダプターを返す"""
    from trade_app.brokers.mock_broker import MockBrokerAdapter
    from trade_app.brokers.tachibana.adapter import TachibanaBrokerAdapter
    settings = get_settings()
    if settings.BROKER_TYPE == "tachibana":
        return TachibanaBrokerAdapter()
    return MockBrokerAdapter()


class ExitWatcher:
    """
    OPEN ポジションを監視し、TP/SL/TimeStop 条件が満たされたら exit を開始する。

    主な責務:
      1. OPEN ポジション一覧を取得
      2. 各ポジションの現在価格をブローカーから取得
      3. unrealized_pnl を更新
      4. ExitPolicy を順番に評価
      5. 条件成立 → PositionManager.initiate_exit() を呼び出し CLOSING に遷移

    ExitWatcher はポジションを CLOSING まで遷移させる。
    CLOSING → CLOSED の遷移は OrderPoller が exit注文の約定確認後に行う。
    """

    def __init__(
        self,
        policies: list[ExitPolicy] | None = None,
    ) -> None:
        self._policies = policies or DEFAULT_EXIT_POLICIES
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """バックグラウンド監視を開始する"""
        if self._running:
            logger.warning("ExitWatcher は既に起動中です")
            return
        self._running = True
        self._task = asyncio.create_task(self._watch_loop(), name="ExitWatcher")
        settings = get_settings()
        logger.info(
            "ExitWatcher 起動: interval=%ds policies=%s",
            settings.EXIT_WATCHER_INTERVAL_SEC,
            [p.name for p in self._policies],
        )

    async def stop(self) -> None:
        """監視を停止してタスクが完了するのを待つ"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ExitWatcher 停止完了")

    async def _watch_loop(self) -> None:
        """監視ループ（バックグラウンドで永続実行）"""
        settings = get_settings()

        await self._record_system_event(
            SystemEventType.WATCHER_START,
            message=f"ExitWatcher 起動 interval={settings.EXIT_WATCHER_INTERVAL_SEC}s",
        )

        while self._running:
            try:
                await self._watch_once()
            except Exception as e:
                logger.error("ExitWatcher サイクルエラー: %s", e, exc_info=True)
                await self._record_system_event(
                    SystemEventType.WATCHER_ERROR,
                    details={"error": str(e)},
                    message=f"ExitWatcher エラー: {e}",
                )
            await asyncio.sleep(settings.EXIT_WATCHER_INTERVAL_SEC)

    async def _watch_once(self) -> None:
        """1サイクル分の監視処理"""
        async with AsyncSessionLocal() as db:
            # OPEN 状態のポジションのみを対象（CLOSING は exit注文送信済みのためスキップ）
            result = await db.execute(
                select(Position)
                .where(Position.status == PositionStatus.OPEN.value)
                .order_by(Position.opened_at.asc())
            )
            positions = result.scalars().all()

            if not positions:
                return

            logger.debug("ExitWatcher: %d 件の OPEN ポジションを監視", len(positions))

            broker = _get_broker()
            audit = AuditLogger(db)

            for position in positions:
                try:
                    await self._evaluate_position(db, position, broker, audit)
                except Exception as e:
                    logger.error(
                        "ExitWatcher: ポジション評価エラー pos=%s ticker=%s error=%s",
                        position.id[:8], position.ticker, e, exc_info=True,
                    )

            # unrealized_pnl 更新分をまとめてコミット
            try:
                await db.commit()
            except Exception as e:
                logger.error("ExitWatcher: unrealized_pnl コミットエラー: %s", e)

    async def _evaluate_position(
        self,
        db: AsyncSession,
        position: Position,
        broker: BrokerAdapter,
        audit: AuditLogger,
    ) -> None:
        """
        1件のポジションを評価する。
          1. 現在価格を取得
          2. unrealized_pnl を更新
          3. ポリシーを順番に評価
          4. 条件成立 → initiate_exit()
        """
        # ─── 現在価格を取得 ────────────────────────────────────────────────
        current_price: Optional[float] = None
        try:
            current_price = await broker.get_market_price(position.ticker)
        except Exception as e:
            logger.warning(
                "ExitWatcher: 価格取得失敗 ticker=%s error=%s",
                position.ticker, e,
            )

        # ─── unrealized_pnl 更新 ──────────────────────────────────────────
        if current_price is not None:
            pos_manager = PositionManager(db=db, audit=audit)
            await pos_manager.update_unrealized_pnl(position, current_price)

        # ─── ポリシー評価 ─────────────────────────────────────────────────
        triggered_policy: Optional[ExitPolicy] = None
        for policy in self._policies:
            try:
                if policy.should_exit(position, current_price):
                    triggered_policy = policy
                    break
            except Exception as e:
                logger.error(
                    "ExitWatcher: ポリシー評価エラー policy=%s pos=%s error=%s",
                    policy.name, position.id[:8], e,
                )

        if triggered_policy is None:
            return

        # ─── exit を開始 ──────────────────────────────────────────────────
        logger.info(
            "ExitWatcher: exit 開始 pos=%s ticker=%s policy=%s reason=%s price=%s",
            position.id[:8], position.ticker,
            triggered_policy.name, triggered_policy.exit_reason.value,
            f"{current_price:.0f}" if current_price else "N/A",
        )

        try:
            pos_manager = PositionManager(db=db, audit=audit)
            await pos_manager.initiate_exit(
                position=position,
                exit_reason=triggered_policy.exit_reason,
                broker=broker,
                triggered_by="watcher",
            )
        except Exception as e:
            logger.error(
                "ExitWatcher: initiate_exit 失敗 pos=%s error=%s",
                position.id[:8], e, exc_info=True,
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
            logger.error("ExitWatcher: システムイベント記録エラー: %s", e)
