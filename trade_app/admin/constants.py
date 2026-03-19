"""
管理画面モジュール定数定義

仕様書: 管理画面仕様書 v0.3 §3(SCR-08接続テスト結果区分), §3(SCR-09通知対象イベント一覧)

【設計方針】
- NotificationEventCode: UIはラベル表示、DBはコード保存。自由文字列不可。
- ConnectionTestResultCode: 接続テストのUI結果区分。6種類。DBには永続化しない。
- ApiConnectionStatus: api_status カラムの値。ConnectionTestResultCode とは別概念。
  - api_status = 現在の接続状態（システムが継続監視・自動更新）
  - 接続テスト結果 = ユーザーがボタンを押したときの診断結果（UI表示のみ）

【通知イベントコード追加ルール】
新しいイベントを追加する際は以下を同時に更新すること:
1. NotificationEventCode enum
2. NOTIFICATION_EVENT_LABELS dict
3. 管理画面仕様書 v0.3 §3(SCR-09) の通知対象イベント一覧
4. バックエンドの通知送信ロジック
"""
import enum


# ─── 通知イベントコード ──────────────────────────────────────────────────────


class NotificationEventCode(str, enum.Enum):
    """
    通知対象イベントの定義済みコード一覧。
    notification_configs.events_json はこのコードの配列を保存する。
    自由文字列は不可。
    """
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_ERROR = "ORDER_ERROR"
    HALT_TRIGGERED = "HALT_TRIGGERED"
    HALT_RELEASED = "HALT_RELEASED"
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    DAILY_PNL_REPORT = "DAILY_PNL_REPORT"
    SYSTEM_ERROR = "SYSTEM_ERROR"


# UIでの表示名（フロントエンド未確定のため参考値として定義）
NOTIFICATION_EVENT_LABELS: dict[str, str] = {
    NotificationEventCode.ORDER_FILLED: "約定発生",
    NotificationEventCode.ORDER_ERROR: "注文エラー",
    NotificationEventCode.HALT_TRIGGERED: "緊急停止（halt発動）",
    NotificationEventCode.HALT_RELEASED: "halt解除",
    NotificationEventCode.BROKER_DISCONNECTED: "証券接続断",
    NotificationEventCode.DAILY_PNL_REPORT: "日次損益レポート",
    NotificationEventCode.SYSTEM_ERROR: "システムエラー",
}

# 有効なイベントコードのセット（バリデーション用）
VALID_EVENT_CODES: frozenset[str] = frozenset(e.value for e in NotificationEventCode)


# ─── 接続テスト結果区分 ──────────────────────────────────────────────────────


class ConnectionTestResultCode(str, enum.Enum):
    """
    立花証券接続テスト（SCR-08）のUI表示結果区分。
    6種類。DB永続化しない（UIフィードバックのみ）。

    テスト実行順序:
    NOT_CONFIGURED チェック → NETWORK_OK → AUTH_OK
    失敗した段階で以降をスキップする。

    NOTE: 具体的なAPIエンドポイントはT-3(未確定)。
    このコードはUI結果区分のみを定義する。
    """
    NOT_CONFIGURED = "NOT_CONFIGURED"               # 必須項目が未設定
    NETWORK_OK = "NETWORK_OK"                       # ネットワーク疎通のみ成功
    AUTH_OK = "AUTH_OK"                             # 認証成功
    AUTH_FAILED = "AUTH_FAILED"                     # 認証情報不正・アカウントロック等
    MAINTENANCE_OR_UNAVAILABLE = "MAINTENANCE_OR_UNAVAILABLE"  # サービス停止中
    UNKNOWN_ERROR = "UNKNOWN_ERROR"                 # 上記以外の例外


# UIラベル
CONNECTION_TEST_RESULT_LABELS: dict[str, str] = {
    ConnectionTestResultCode.NOT_CONFIGURED: "接続設定が未入力です",
    ConnectionTestResultCode.NETWORK_OK: "ネットワーク到達確認: OK",
    ConnectionTestResultCode.AUTH_OK: "認証確認: OK",
    ConnectionTestResultCode.AUTH_FAILED: "認証失敗: IDまたはパスワードを確認してください",
    ConnectionTestResultCode.MAINTENANCE_OR_UNAVAILABLE: "サービス停止中またはメンテナンス中の可能性があります",
    ConnectionTestResultCode.UNKNOWN_ERROR: "テスト中に予期しないエラーが発生しました",
}

# 接続テストが「成功」とみなす結果コード
CONNECTION_TEST_SUCCESS_CODES: frozenset[str] = frozenset({
    ConnectionTestResultCode.AUTH_OK,
})


# ─── API 接続状態（api_status カラム） ──────────────────────────────────────
# 接続テスト結果区分とは別概念。
# api_status = broker_connection_configs テーブルで管理する「現在の接続状態」
# システムが継続監視して自動更新する。ユーザー操作とは独立。


class ApiConnectionStatus(str, enum.Enum):
    """
    broker_connection_configs.api_status の値。
    システムが自動更新する現在の接続状態を表す。

    【ConnectionTestResultCode との違い】
    - api_status: システムが継続監視する「現在状態」（DB永続化）
    - ConnectionTestResultCode: ユーザー操作による「診断結果」（UI表示のみ）
    """
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


# ─── 管理画面ロール ────────────────────────────────────────────────────────────


class AdminRole(str, enum.Enum):
    """
    管理画面ユーザーのロール。
    Phase 1 では admin のみ実効ロールとして使用する。
    operator / viewer は Phase 2 以降で UI 分岐を実装する。
    """
    ADMIN = "admin"
    OPERATOR = "operator"   # Phase 2以降
    VIEWER = "viewer"       # Phase 2以降


# Phase 1 で実効ロールとして使用するロールセット
PHASE1_EFFECTIVE_ROLES: frozenset[str] = frozenset({AdminRole.ADMIN})


# ─── 電話認証状態 ──────────────────────────────────────────────────────────────


class PhoneAuthStatus(str, enum.Enum):
    """
    立花証券の電話認証状態（SCR-08）。
    システムが自動変更しない。手動入力のみ。
    """
    CONFIRMED = "CONFIRMED"
    UNCONFIRMED = "UNCONFIRMED"
    UNKNOWN = "UNKNOWN"


# ─── 通知チャンネル種別 ────────────────────────────────────────────────────────


class NotificationChannelType(str, enum.Enum):
    """通知先の種別。"""
    EMAIL = "email"
    TELEGRAM = "telegram"
    # Slack 等は Phase 2 以降


# ─── 管理画面監査イベント種別 ──────────────────────────────────────────────────


class AdminAuditEventType(str, enum.Enum):
    """
    ui_audit_logs テーブルのイベント種別。
    trade_db の AuditEventType とは別テーブル・別用途。
    """
    # 認証
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    TWO_FA_SUCCESS = "TWO_FA_SUCCESS"
    TWO_FA_FAILURE = "TWO_FA_FAILURE"
    LOGOUT = "LOGOUT"
    SESSION_INVALIDATED = "SESSION_INVALIDATED"
    # 期限切れセッションによるアクセス（I-5 確定: Absolute Timeout 採用・記録必須）
    # auth_guard が expires_at 超過を検出した時点で記録する。
    # user_id: セッションの user_id（セッションが存在する限り取得可能）
    # ip_address / user_agent: リクエストから取得（USER_INITIATED_EVENTS に分類）
    SESSION_EXPIRED_ACCESS = "SESSION_EXPIRED_ACCESS"

    # 銘柄管理
    SYMBOL_CREATED = "SYMBOL_CREATED"
    SYMBOL_UPDATED = "SYMBOL_UPDATED"
    SYMBOL_ENABLED = "SYMBOL_ENABLED"
    SYMBOL_DISABLED = "SYMBOL_DISABLED"
    SYMBOL_DELETED = "SYMBOL_DELETED"

    # 戦略管理（Phase 1: 承認フローなし）
    STRATEGY_CREATED = "STRATEGY_CREATED"
    STRATEGY_UPDATED = "STRATEGY_UPDATED"
    STRATEGY_ENABLED = "STRATEGY_ENABLED"
    STRATEGY_DISABLED = "STRATEGY_DISABLED"

    # 証券接続設定（秘密情報はログに含めない）
    BROKER_CONFIG_UPDATED = "BROKER_CONFIG_UPDATED"
    BROKER_CONNECTION_TEST = "BROKER_CONNECTION_TEST"
    PHONE_AUTH_STATUS_UPDATED = "PHONE_AUTH_STATUS_UPDATED"

    # 通知設定
    NOTIFICATION_CONFIG_CREATED = "NOTIFICATION_CONFIG_CREATED"
    NOTIFICATION_CONFIG_UPDATED = "NOTIFICATION_CONFIG_UPDATED"
    NOTIFICATION_CONFIG_DELETED = "NOTIFICATION_CONFIG_DELETED"
    NOTIFICATION_TEST_SENT = "NOTIFICATION_TEST_SENT"
    NOTIFICATION_TEST_FAILED = "NOTIFICATION_TEST_FAILED"

    # システム制御
    HALT_TRIGGERED_MANUAL = "HALT_TRIGGERED_MANUAL"
    HALT_RELEASED = "HALT_RELEASED"

    # システム設定
    SYSTEM_SETTINGS_UPDATED = "SYSTEM_SETTINGS_UPDATED"

    # 監査ログ操作（機密情報ダウンロードのため記録対象）
    AUDIT_LOG_EXPORTED = "AUDIT_LOG_EXPORTED"

    # システム自動（IP/UA は null 可）
    HALT_TRIGGERED_AUTO = "HALT_TRIGGERED_AUTO"
    SYSTEM_ERROR_DETECTED = "SYSTEM_ERROR_DETECTED"


# 「ユーザー起点」イベント: IP / UA 記録必須
USER_INITIATED_EVENTS: frozenset[str] = frozenset({
    AdminAuditEventType.LOGIN_SUCCESS,
    AdminAuditEventType.LOGIN_FAILURE,
    AdminAuditEventType.TWO_FA_SUCCESS,
    AdminAuditEventType.TWO_FA_FAILURE,
    AdminAuditEventType.LOGOUT,
    AdminAuditEventType.SESSION_INVALIDATED,
    AdminAuditEventType.SESSION_EXPIRED_ACCESS,
    AdminAuditEventType.SYMBOL_CREATED,
    AdminAuditEventType.SYMBOL_UPDATED,
    AdminAuditEventType.SYMBOL_ENABLED,
    AdminAuditEventType.SYMBOL_DISABLED,
    AdminAuditEventType.SYMBOL_DELETED,
    AdminAuditEventType.STRATEGY_CREATED,
    AdminAuditEventType.STRATEGY_UPDATED,
    AdminAuditEventType.STRATEGY_ENABLED,
    AdminAuditEventType.STRATEGY_DISABLED,
    AdminAuditEventType.BROKER_CONFIG_UPDATED,
    AdminAuditEventType.BROKER_CONNECTION_TEST,
    AdminAuditEventType.PHONE_AUTH_STATUS_UPDATED,
    AdminAuditEventType.NOTIFICATION_CONFIG_CREATED,
    AdminAuditEventType.NOTIFICATION_CONFIG_UPDATED,
    AdminAuditEventType.NOTIFICATION_CONFIG_DELETED,
    AdminAuditEventType.NOTIFICATION_TEST_SENT,
    AdminAuditEventType.NOTIFICATION_TEST_FAILED,
    AdminAuditEventType.HALT_TRIGGERED_MANUAL,
    AdminAuditEventType.HALT_RELEASED,
    AdminAuditEventType.SYSTEM_SETTINGS_UPDATED,
    AdminAuditEventType.AUDIT_LOG_EXPORTED,
})

# 「システム自動」イベント: IP / UA は null 可
SYSTEM_INITIATED_EVENTS: frozenset[str] = frozenset({
    AdminAuditEventType.HALT_TRIGGERED_AUTO,
    AdminAuditEventType.SYSTEM_ERROR_DETECTED,
})
