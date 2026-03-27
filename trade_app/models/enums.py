"""
システム全体で使用する列挙型定義
str ミックスインにより JSON シリアライズ・DB保存を統一
"""
import enum


class OrderType(str, enum.Enum):
    """注文種別"""
    MARKET = "market"   # 成行
    LIMIT = "limit"     # 指値


class Side(str, enum.Enum):
    """売買区分"""
    BUY = "buy"
    SELL = "sell"


class SignalStatus(str, enum.Enum):
    """シグナルの処理状態"""
    RECEIVED = "received"       # 受信済み・処理待ち
    PROCESSING = "processing"   # リスクチェック・発注処理中
    EXECUTED = "executed"       # 発注完了
    REJECTED = "rejected"       # リスクチェックで拒否
    FAILED = "failed"           # 発注失敗（ブローカーエラー等）


class OrderStatus(str, enum.Enum):
    """注文の状態"""
    PENDING = "pending"         # 発注待ち
    SUBMITTED = "submitted"     # ブローカーへ送信済み
    PARTIAL = "partial"         # 一部約定
    FILLED = "filled"           # 全量約定
    CANCELLED = "cancelled"     # キャンセル済み
    REJECTED = "rejected"       # ブローカーに拒否された
    FAILED = "failed"           # システムエラー
    UNKNOWN = "unknown"         # ブローカーから状態が取得できない（新規発注をブロック）


class PositionStatus(str, enum.Enum):
    """ポジションの状態"""
    OPEN = "open"               # 保有中
    CLOSING = "closing"         # クローズ処理中（決済注文送信済み）
    CLOSED = "closed"           # クローズ完了


class ExitReason(str, enum.Enum):
    """ポジションのクローズ理由"""
    TP_HIT = "tp_hit"           # 利確（Take Profit）達成
    SL_HIT = "sl_hit"           # 損切（Stop Loss）発動
    TIMEOUT = "timeout"         # 時間切れ（例: 大引け前強制決済）
    MANUAL = "manual"           # 手動クローズ
    SIGNAL = "signal"           # 分析システムからのexitシグナル


class SystemEventType(str, enum.Enum):
    """システムイベント種別（system_events テーブル用）"""
    STARTUP = "startup"                         # アプリケーション起動
    SHUTDOWN = "shutdown"                       # アプリケーションシャットダウン
    RECOVERY_START = "recovery_start"           # 起動時リカバリ開始
    RECOVERY_COMPLETE = "recovery_complete"     # 起動時リカバリ完了
    RECONCILE_START = "reconcile_start"         # 状態整合開始
    RECONCILE_COMPLETE = "reconcile_complete"   # 状態整合完了
    POLLER_START = "poller_start"               # OrderPoller 開始
    POLLER_ERROR = "poller_error"               # OrderPoller エラー
    ORDER_RECONCILED = "order_reconciled"       # 注文状態を整合済みに更新
    ORDER_STUCK = "order_stuck"                 # 長時間未解決注文を検出
    WATCHER_START = "watcher_start"             # ExitWatcher 開始
    WATCHER_ERROR = "watcher_error"             # ExitWatcher エラー
    HALT_ACTIVATED = "halt_activated"           # 取引停止発動
    HALT_DEACTIVATED = "halt_deactivated"       # 取引停止解除


class BrokerRequestType(str, enum.Enum):
    """ブローカーへのリクエスト種別（broker_requests テーブル用）"""
    PLACE = "place"                 # 新規発注
    CANCEL = "cancel"               # キャンセル
    STATUS_QUERY = "status_query"   # 注文状態照会


class HaltType(str, enum.Enum):
    """取引停止の種別（trading_halts テーブル用）"""
    DAILY_LOSS = "daily_loss"                   # 日次損失上限到達
    CONSECUTIVE_LOSSES = "consecutive_losses"   # 連続損失による停止
    MANUAL = "manual"                           # 手動停止


class StateLayer(str, enum.Enum):
    """Market State Engine の評価レイヤー"""
    MARKET = "market"           # 市場全体（TOPIX/日経など）
    SYMBOL = "symbol"           # 個別銘柄
    TIME_WINDOW = "time_window" # 時間帯


class StateSeverity(str, enum.Enum):
    """状態の重大度"""
    INFO = "info"       # 情報のみ（取引制限なし）
    WARNING = "warning" # 注意（発注サイズ縮小推奨）
    CRITICAL = "critical" # 重大（新規発注禁止推奨）


class StrategyDirection(str, enum.Enum):
    """Strategy の売買方向"""
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


class StrategyConditionType(str, enum.Enum):
    """Strategy 条件の種別"""
    REQUIRED_STATE = "required_state"    # この state が active なら条件成立
    FORBIDDEN_STATE = "forbidden_state"  # この state が active なら entry 禁止
    SIZE_MODIFIER = "size_modifier"      # この state が active なら size を縮小


class StrategyOperator(str, enum.Enum):
    """Strategy 条件の比較演算子（Phase 1 は exists のみ使用）"""
    EQUALS = "equals"
    IN = "in"
    GTE = "gte"
    LTE = "lte"
    EXISTS = "exists"  # state が active かどうかのみ（Phase 1 主力）


class AuditEventType(str, enum.Enum):
    """監査ログのイベント種別"""
    SIGNAL_RECEIVED = "signal_received"         # シグナル受信
    SIGNAL_DUPLICATE = "signal_duplicate"       # 重複シグナル検出
    SIGNAL_REJECTED = "signal_rejected"         # シグナル拒否（リスク）
    SIGNAL_PROCESSED = "signal_processed"       # シグナル処理完了
    RISK_REJECTED = "risk_rejected"             # リスクチェックで拒否
    STRATEGY_GATE_REJECTED = "strategy_gate_rejected"  # Strategy Gate で拒否
    ORDER_SUBMITTED = "order_submitted"         # 発注送信
    ORDER_FILLED = "order_filled"               # 約定完了
    ORDER_CANCELLED = "order_cancelled"         # キャンセル
    ORDER_FAILED = "order_failed"               # 発注失敗
    POSITION_OPENED = "position_opened"         # ポジション開設
    POSITION_CLOSING = "position_closing"       # ポジション決済中（exit注文送信済み）
    POSITION_CLOSED = "position_closed"         # ポジション決済完了
    HALT_ACTIVATED = "halt_activated"           # 取引停止発動
    HALT_DEACTIVATED = "halt_deactivated"       # 取引停止解除
    SYSTEM_ERROR = "system_error"               # システムエラー
    DUPLICATE_EXIT_ATTEMPT = "duplicate_exit_attempt"  # DB制約による二重exit検出
