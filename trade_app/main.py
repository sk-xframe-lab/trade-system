"""
FastAPI アプリケーションエントリーポイント
起動時に Redis 接続を初期化し、全ルートを登録する。
"""
import logging
import logging.config
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from trade_app.config import get_settings
from trade_app.services.exit_watcher import ExitWatcher
from trade_app.services.market_state.runner import MarketStateRunner
from trade_app.services.order_poller import OrderPoller
from trade_app.services.recovery_manager import RecoveryManager
from trade_app.services.strategy.runner import StrategyRunner

settings = get_settings()

# ─── ロギング設定 ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── シングルトン ─────────────────────────────────────────────────────────────
_redis_client: redis.Redis | None = None
_order_poller: OrderPoller | None = None
_exit_watcher: ExitWatcher | None = None
_market_state_runner: MarketStateRunner | None = None
_strategy_runner: StrategyRunner | None = None


def get_redis_client() -> redis.Redis:
    """Redis クライアントを返す（lifespan で初期化済みであること）"""
    if _redis_client is None:
        raise RuntimeError("Redis クライアントが初期化されていません")
    return _redis_client


# ─── アプリケーションライフサイクル ──────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """起動・シャットダウン時の初期化・クリーンアップ処理"""
    global _redis_client, _order_poller, _exit_watcher, _market_state_runner, _strategy_runner

    # ─── 起動時 ──────────────────────────────────────────────────────────
    logger.info("自動売買システム 起動開始")
    logger.info("ブローカー種別: %s", settings.BROKER_TYPE)

    # 管理画面 OAuth / TOTP 設定の有無をログ（値は出力しない）
    logger.info(
        "管理画面設定: Google OAuth=%s TOTP暗号化=%s",
        "設定済み" if (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET) else "未設定(GOOGLE_CLIENT_ID/SECRET を .env に設定してください)",
        "設定済み" if settings.TOTP_ENCRYPTION_KEY else "未設定(TOTP_ENCRYPTION_KEY を .env に設定してください)",
    )

    # Redis 接続を確立
    try:
        _redis_client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=False,
        )
        await _redis_client.ping()
        logger.info("Redis 接続: OK (%s)", settings.REDIS_URL.split("@")[-1])
    except Exception as e:
        logger.error("Redis 接続エラー: %s — 冪等性チェックは DB フォールバックで動作", e)

    # 起動時リカバリ: 前回プロセス停止時の未解決注文を整合
    try:
        recovery = RecoveryManager()
        await recovery.recover_on_startup()
        logger.info("起動時リカバリ: 完了")
    except Exception as e:
        logger.error("起動時リカバリエラー: %s — サーバーは起動継続", e, exc_info=True)

    # OrderPoller をバックグラウンドタスクとして起動
    _order_poller = OrderPoller()
    _order_poller.start()

    # ExitWatcher をバックグラウンドタスクとして起動
    _exit_watcher = ExitWatcher()
    _exit_watcher.start()

    # MarketStateRunner をバックグラウンドタスクとして起動（60秒周期）
    _market_state_runner = MarketStateRunner()
    _market_state_runner.start()

    # StrategyRunner をバックグラウンドタスクとして起動（60秒周期）
    _strategy_runner = StrategyRunner()
    _strategy_runner.start()

    logger.info("自動売買システム 起動完了")

    yield

    # ─── シャットダウン時 ─────────────────────────────────────────────────
    logger.info("自動売買システム シャットダウン中...")
    if _strategy_runner:
        await _strategy_runner.stop()
    if _market_state_runner:
        await _market_state_runner.stop()
    if _exit_watcher:
        await _exit_watcher.stop()
    if _order_poller:
        await _order_poller.stop()
    if _redis_client:
        await _redis_client.aclose()
    logger.info("自動売買システム シャットダウン完了")


# ─── FastAPI アプリ生成 ───────────────────────────────────────────────────────

app = FastAPI(
    title="日本株自動売買システム",
    description=(
        "分析システムからのシグナルを受信し、"
        "リスクチェック・発注・ポジション管理を行う自動売買エンジン"
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# ─── CORS 設定 ────────────────────────────────────────────────────────────────
# 管理画面 SPA フロントエンドとのクロスオリジンリクエストを許可する。
# allow_credentials=True を設定する場合、allow_origins に "*" は使えない（明示的なオリジンが必要）。
# SameSite=Lax の Cookie が正しく送受信されるために credentials: 'include' と合わせて設定する。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.ADMIN_FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# ─── ルート登録 ───────────────────────────────────────────────────────────────
from trade_app.routes import health, signals, orders, positions  # noqa: E402
from trade_app.routes import admin  # noqa: E402
from trade_app.routes import market_state  # noqa: E402
from trade_app.routes import strategy  # noqa: E402
from trade_app.admin.router import admin_router  # noqa: E402

app.include_router(health.router)
app.include_router(signals.router)
app.include_router(orders.router)
app.include_router(positions.router)
app.include_router(admin.router)
app.include_router(market_state.router)
app.include_router(strategy.router)
app.include_router(admin_router)  # 管理画面 UI 向け (セッション認証)


# ─── グローバル例外ハンドラ ───────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc: Exception):
    """未捕捉の例外を500エラーとして返す（スタックトレースは本番では非公開）"""
    logger.error("未捕捉エラー: %s", exc, exc_info=True)
    detail = str(exc) if settings.DEBUG else "内部エラーが発生しました"
    return JSONResponse(
        status_code=500,
        content={"detail": detail},
    )
