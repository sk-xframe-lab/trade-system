"""
DashboardService — ダッシュボードデータ集計サービス

仕様書: 管理画面仕様書 v0.3 §3(SCR-03)

【集計対象】
trade_db の既存テーブル（orders, positions, trading_halts 等）から集計する。
管理画面専用テーブル（symbol_configs 等）は I-1 確定後に migration を行うため、
現状は trade_db テーブルのみを集計対象とする。

【環境バナー】
broker_connection_configs は T-1/I-3 未確定のため、
broker_connection テーブルが存在しない間は "not_configured" を返す。
TODO(I-1, T-1, I-3): broker_connection_configs 実装後に環境情報を取得する

【JST 時刻処理方針】
Python 3.9+ stdlib の `zoneinfo.ZoneInfo("Asia/Tokyo")` を第一選択とする。
系 tzdata パッケージ (requirements.txt 追加済み) でデータを補完する。
tzdata 未整備のフォールバック: UTC+9 固定オフセット算術計算。
サマータイムは存在しないため固定オフセットでも JST 0:00 は正確に求まる。
"""
import logging
from datetime import datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.schemas.dashboard import (
    DashboardResponse,
    EnvironmentBanner,
    HaltStatusItem,
    RecentActivity,
    SystemStatusSummary,
    TodaySummary,
)
from trade_app.models.enums import OrderStatus, PositionStatus
from trade_app.models.order import Order
from trade_app.models.position import Position
from trade_app.models.trading_halt import TradingHalt
from trade_app.services.halt_manager import HaltManager

logger = logging.getLogger(__name__)

# JST オフセット（UTC+9）
_JST_OFFSET_HOURS = 9


def _today_jst_range() -> tuple[datetime, datetime]:
    """
    本日 JST 0:00 を UTC datetime として返す。
    第二要素は現在時刻 (UTC)。

    【優先順位】
    1. zoneinfo.ZoneInfo("Asia/Tokyo") — Python 3.9+ stdlib + tzdata パッケージ
    2. UTC+9 固定オフセット算術 — tzdata 未整備環境のフォールバック
    """
    from datetime import timedelta

    now_utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        jst = ZoneInfo("Asia/Tokyo")
        now_jst = now_utc.astimezone(jst)
        today_start_jst = now_jst.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_utc = today_start_jst.astimezone(timezone.utc)
    except Exception:
        # tzdata 未整備環境: UTC+9 固定オフセット算術（JST にサマータイムなし）
        jst_now = now_utc + timedelta(hours=_JST_OFFSET_HOURS)
        jst_today_start = jst_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_utc = (jst_today_start - timedelta(hours=_JST_OFFSET_HOURS)).replace(
            tzinfo=timezone.utc
        )

    return today_start_utc, now_utc


class DashboardService:
    """ダッシュボードデータの集計サービス"""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def get_dashboard(self) -> DashboardResponse:
        """ダッシュボード全データを取得して返す"""
        now = datetime.now(timezone.utc)
        today_start, today_end = _today_jst_range()

        environment_banner = await self._get_environment_banner()
        system_status = await self._get_system_status()
        today_summary = await self._get_today_summary(today_start, today_end)
        recent_activity = await self._get_recent_activity(today_start)

        return DashboardResponse(
            environment_banner=environment_banner,
            system_status=system_status,
            today_summary=today_summary,
            recent_activity=recent_activity,
            retrieved_at=now,
        )

    async def _get_environment_banner(self) -> EnvironmentBanner:
        """
        環境バナー情報を返す。
        TODO(I-1, T-1, I-3): broker_connection_configs 実装後に実際の環境を取得する。
        現在は未設定（not_configured）を返す。
        """
        # TODO: broker_connection_configs から environment を取得する
        # config = await self._get_broker_config()
        # if config: return EnvironmentBanner(environment=config.environment, ...)
        return EnvironmentBanner(
            environment="not_configured",
            label="証券接続未設定",
            style="muted",
        )

    async def _get_system_status(self) -> SystemStatusSummary:
        """システム稼働状況を取得する"""
        halt_mgr = HaltManager()
        active_halts = await halt_mgr.get_active_halts(self._db)

        open_count = await self._db.execute(
            select(func.count(Position.id)).where(
                Position.status == PositionStatus.OPEN.value
            )
        )
        closing_count = await self._db.execute(
            select(func.count(Position.id)).where(
                Position.status == PositionStatus.CLOSING.value
            )
        )

        halt_items = [
            HaltStatusItem(
                id=h.id,
                halt_type=h.halt_type,
                reason=h.reason,
                activated_at=h.activated_at,
                activated_by=h.activated_by,
            )
            for h in active_halts
        ]

        # TODO(I-1): symbol_configs テーブル実装後に watched_symbol_count を実装
        # TODO(trade_db): strategy_definitions から enabled_strategy_count を取得
        enabled_strategy_count = await self._get_enabled_strategy_count()

        return SystemStatusSummary(
            is_running=True,
            is_halted=len(active_halts) > 0,
            halt_count=len(active_halts),
            active_halts=halt_items,
            open_position_count=open_count.scalar() or 0,
            closing_position_count=closing_count.scalar() or 0,
            enabled_strategy_count=enabled_strategy_count,
            watched_symbol_count=0,  # TODO(I-1): symbol_configs 実装後に更新
            broker_api_status="NOT_CONFIGURED",  # TODO(I-3): broker_connection_configs 実装後
            phone_auth_status="NOT_CONFIGURED",  # TODO(I-3): broker_connection_configs 実装後
        )

    async def _get_today_summary(
        self, today_start: datetime, today_end: datetime
    ) -> TodaySummary:
        """本日の取引サマリーを集計する"""
        # 発注要求件数 = 本日作成された全 Order 数
        order_req = await self._db.execute(
            select(func.count(Order.id)).where(
                Order.created_at >= today_start,
                Order.created_at <= today_end,
            )
        )

        # ブローカー受付件数 = SUBMITTED 以上に遷移した Order 数
        submitted_statuses = [
            OrderStatus.SUBMITTED.value,
            OrderStatus.PARTIAL.value,
            OrderStatus.FILLED.value,
            OrderStatus.CANCELLED.value,
        ]
        broker_accepted = await self._db.execute(
            select(func.count(Order.id)).where(
                Order.created_at >= today_start,
                Order.created_at <= today_end,
                Order.status.in_(submitted_statuses),
            )
        )

        # 約定件数（FILLED）
        filled_total = await self._db.execute(
            select(func.count(Order.id)).where(
                Order.created_at >= today_start,
                Order.created_at <= today_end,
                Order.status == OrderStatus.FILLED.value,
            )
        )
        # エントリー約定
        filled_entry = await self._db.execute(
            select(func.count(Order.id)).where(
                Order.created_at >= today_start,
                Order.created_at <= today_end,
                Order.status == OrderStatus.FILLED.value,
                Order.is_exit_order.is_(False),
            )
        )
        # エグジット約定
        filled_exit = await self._db.execute(
            select(func.count(Order.id)).where(
                Order.created_at >= today_start,
                Order.created_at <= today_end,
                Order.status == OrderStatus.FILLED.value,
                Order.is_exit_order.is_(True),
            )
        )

        # 失敗件数
        failed = await self._db.execute(
            select(func.count(Order.id)).where(
                Order.created_at >= today_start,
                Order.created_at <= today_end,
                Order.status.in_([OrderStatus.REJECTED.value, OrderStatus.FAILED.value]),
            )
        )

        # 本日の実現損益
        from trade_app.models.trade_result import TradeResult
        pnl_result = await self._db.execute(
            select(func.coalesce(func.sum(TradeResult.pnl), 0)).where(
                TradeResult.created_at >= today_start,
                TradeResult.created_at <= today_end,
            )
        )

        return TodaySummary(
            order_request_count=order_req.scalar() or 0,
            broker_accepted_count=broker_accepted.scalar() or 0,
            filled_count=filled_total.scalar() or 0,
            filled_entry_count=filled_entry.scalar() or 0,
            filled_exit_count=filled_exit.scalar() or 0,
            failed_count=failed.scalar() or 0,
            realized_pnl_jpy=float(pnl_result.scalar() or 0),
        )

    async def _get_recent_activity(self, today_start: datetime) -> RecentActivity:
        """直近のアクティビティを取得する"""
        from trade_app.models.audit_log import AuditLog

        # 直近10件の約定（Orders FILLED）
        recent_fills_result = await self._db.execute(
            select(Order)
            .where(Order.status == OrderStatus.FILLED.value)
            .order_by(Order.updated_at.desc())
            .limit(10)
        )
        recent_fills = [
            {
                "order_id": o.id,
                "symbol_code": o.symbol_code if hasattr(o, "symbol_code") else None,
                "side": o.side,
                "filled_quantity": o.filled_quantity,
                "filled_price": o.filled_price,
                "is_exit_order": o.is_exit_order,
                "updated_at": o.updated_at.isoformat() if o.updated_at else None,
            }
            for o in recent_fills_result.scalars().all()
        ]

        # 直近5件の監査ログ（trade_db の audit_logs）
        recent_audit_result = await self._db.execute(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .limit(5)
        )
        recent_audit = [
            {
                "event_type": a.event_type,
                "entity_type": a.entity_type,
                "actor": a.actor,
                "message": a.message,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in recent_audit_result.scalars().all()
        ]

        return RecentActivity(
            recent_fills=recent_fills,
            recent_audit_logs=recent_audit,
        )

    async def _get_enabled_strategy_count(self) -> int:
        """有効な strategy 数を trade_db から取得する"""
        from trade_app.models.strategy_definition import StrategyDefinition
        result = await self._db.execute(
            select(func.count(StrategyDefinition.id)).where(
                StrategyDefinition.is_enabled.is_(True)
            )
        )
        return result.scalar() or 0
