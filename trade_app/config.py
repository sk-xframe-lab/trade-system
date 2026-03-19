"""
設定管理モジュール
すべての環境変数はここ経由で参照する。コードへのハードコード禁止。
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """アプリケーション設定（pydantic-settings で .env / 環境変数から自動ロード）"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ─── DB (trade_db) ─────────────────────────────────────────────────────
    DATABASE_URL: str = (
        "postgresql+asyncpg://trade:trade_secret@postgres:5432/trade_db"
    )
    # Alembicマイグレーション専用（同期ドライバ）
    DATABASE_URL_SYNC: str = (
        "postgresql+psycopg2://trade:trade_secret@postgres:5432/trade_db"
    )

    # ─── DB (admin_db) ─────────────────────────────────────────────────────
    # 管理画面専用 DB 接続。
    # Phase 1 単一コンテナ前提: デフォルト値は trade_db と同一 PostgreSQL を使用する。
    # 論理分離（別 Base クラス + 別 Alembic チェーン）は実装済み。
    # 物理配置（別サーバー / 別 DB 名への分離）は未確定。
    # ADMIN_DATABASE_URL を変更するだけでコード変更なしに対応可能。
    ADMIN_DATABASE_URL: str = (
        "postgresql+asyncpg://trade:trade_secret@postgres:5432/trade_db"
    )
    # Alembic admin マイグレーション専用（同期ドライバ）
    ADMIN_DATABASE_URL_SYNC: str = (
        "postgresql+psycopg2://trade:trade_secret@postgres:5432/trade_db"
    )

    # ─── Redis ─────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://:redis_secret@redis:6379/0"

    # ─── 認証 ───────────────────────────────────────────────────────────────
    # 分析システムが Authorization: Bearer <token> で送信するトークン
    API_TOKEN: str = "changeme_before_production"

    # ─── ブローカー ─────────────────────────────────────────────────────────
    # "mock" または "tachibana"
    BROKER_TYPE: str = "mock"

    # 立花証券 e_api 認証情報（Phase 10+ で設定）
    TACHIBANA_BASE_URL: str = ""          # ログイン URL（固定。仮想 URL はログイン後に取得）
    TACHIBANA_USER_ID: str = ""
    TACHIBANA_PASSWORD: str = ""
    TACHIBANA_SECOND_PASSWORD: str = ""   # 第二パスワード（取引パスワード）。注文・取消に必須
    TACHIBANA_REQUEST_TIMEOUT_SEC: float = 10.0   # e_api リクエストタイムアウト（秒）
    # NOTE: 以下 3 項目はコード値が仕様書未確認の推定値。Adapter 本体実装前に確認すること。
    TACHIBANA_DEFAULT_TAX_TYPE: str = "3"   # 推定: "1"=一般 "2"=特定 "3"=NISA。TODO: 仕様書確認
    TACHIBANA_DEFAULT_MARKET: str = "00"    # 推定: "00"=東証。TODO: 仕様書で市場コード表を確認
    TACHIBANA_MAX_RETRIES: int = 3          # ネットワークエラー時の最大リトライ回数

    # ─── リスク管理 ─────────────────────────────────────────────────────────
    # 1ポジションの最大サイズ（口座残高に対する割合 %）
    MAX_POSITION_SIZE_PCT: float = 10.0
    # 同時保有ポジションの上限件数
    MAX_CONCURRENT_POSITIONS: int = 5
    # 1日の損失上限（円）。この金額を超えたら halt を記録して新規発注を停止
    DAILY_LOSS_LIMIT_JPY: float = 50000.0
    # この件数以上連続して損失が出たら halt を記録して発注停止（0 = 無効）
    CONSECUTIVE_LOSSES_STOP: int = 3

    # ─── ExitWatcher ─────────────────────────────────────────────────────────
    # ExitWatcher がポジションをチェックする間隔（秒）
    EXIT_WATCHER_INTERVAL_SEC: int = 10

    # ─── Market State Engine ──────────────────────────────────────────────────
    # MarketStateEngine が評価を実行する間隔（秒）。ExitWatcher(10秒)・OrderPoller(5秒)とは独立した周期。
    MARKET_STATE_INTERVAL_SEC: int = 60
    # 監視対象銘柄コード（カンマ区切り）。空文字の場合は銘柄評価をスキップ。
    # 例: "7203,9984,6758"
    WATCHED_SYMBOLS: str = ""

    # ─── Signal Router Integration Gate ──────────────────────────────────────
    # latest strategy decision の最大許容古さ（秒）。この秒数を超えた decision は stale 扱い。
    # stale の場合 entry_allowed=False, blocking_reasons に "decision_stale:{layer}:{strategy_code}" を追加。
    SIGNAL_MAX_DECISION_AGE_SEC: int = 180

    # ─── Strategy Engine ──────────────────────────────────────────────────────
    # state snapshot の最大許容古さ（秒）。この秒数を超えた snapshot は stale 扱い。
    # stale の場合 entry_allowed=False, blocking_reasons に "state_snapshot_stale" を追加。
    STRATEGY_MAX_STATE_AGE_SEC: int = 180
    # StrategyRunner の実行間隔（秒）。MarketStateRunner(60秒)と同じ周期。
    STRATEGY_RUNNER_INTERVAL_SEC: int = 60

    # ─── 管理画面 OAuth / Session ─────────────────────────────────────────────
    # Google Cloud Console で取得した OAuth 2.0 クライアント認証情報
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    # Google Console に登録した redirect_uri（フロントエンドの /auth/callback URL）
    # 例: https://admin.example.com/auth/callback
    OAUTH_REDIRECT_URI: str = ""
    # 管理画面フロントエンドのオリジン（CORS allow_origins 設定用）
    # 例: https://admin.example.com
    ADMIN_FRONTEND_ORIGIN: str = "http://localhost:5173"

    # セッション有効期限（秒）— I-5 確定値
    SESSION_TTL_SEC: int = 28800          # 8 時間（TOTP 完了後の完全認証セッション）
    PRE_2FA_SESSION_TTL_SEC: int = 600    # 10 分（OAuth 後・TOTP 前の仮セッション）

    # Cookie Secure フラグ（明示設定）
    # True（デフォルト）: 本番環境で Secure 属性を付与（HTTPS 必須）
    # False: ローカル開発・HTTP 環境での動作確認時に使用
    # DEBUG=true に依存せず、このフラグで本番 Secure を強制できる設計
    COOKIE_SECURE: bool = True

    # TOTP シークレット暗号化鍵（AES-256-GCM / 32 バイト）
    # 生成: python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
    # 未設定の場合、TOTP setup/verify が 503 を返す
    TOTP_ENCRYPTION_KEY: str = ""

    # TOTP issuer 名称（Google Authenticator 等の QR コードに表示される）
    # 設計書: docs/admin/design_i4_auth_gaps.md Q7 確定値: "TradeSystem Admin"
    TOTP_ISSUER: str = "TradeSystem Admin"

    # ─── アプリケーション ────────────────────────────────────────────────────
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """設定シングルトンを返す（テスト時は dependency_overrides で差し替え可能）"""
    return Settings()
