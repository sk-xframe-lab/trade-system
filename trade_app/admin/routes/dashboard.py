"""
ダッシュボードルーター (SCR-03)

仕様書: 管理画面仕様書 v0.3 §3(SCR-03)

【集計対象】
DashboardService は trade_db テーブルのみを集計対象とする:
  orders, positions, trading_halts, trade_results, audit_logs, strategy_definitions

admin_db テーブル（symbol_configs 等）は I-1 DB 分離設計確定後に追加予定。
  → TODO(I-1): watched_symbol_count を symbol_configs から取得

【DB セッション分離】
このルーターは get_trade_db を使用する（admin_db は不要）。
DashboardService が参照する全テーブルが trade_db に存在するため。
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.admin.database import get_trade_db
from trade_app.admin.schemas.dashboard import DashboardResponse
from trade_app.admin.services.auth_guard import RequireAdmin
from trade_app.admin.services.dashboard_service import DashboardService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["Admin Dashboard"])


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    current_user: RequireAdmin,
    db: AsyncSession = Depends(get_trade_db),
) -> DashboardResponse:
    """
    ダッシュボード全データを取得する。

    - 環境バナー（TODO: broker_connection_configs 実装後に更新）
    - システム稼働状況（halt 数・ポジション数・strategy 数）
    - 本日取引サマリー（JST 基準）
    - 直近アクティビティ（約定・監査ログ）
    """
    svc = DashboardService(db)
    return await svc.get_dashboard()
