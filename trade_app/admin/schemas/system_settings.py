"""
システム設定 スキーマ (SCR-14)

仕様書: 管理画面仕様書 v0.3 §3(SCR-14)

【永続化の扱い — 重要】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
現在の実装は「ランタイム上書き」のみ。

  - PATCH /system-settings は lru_cache で保持されている Settings シングルトンを
    object.__setattr__() で直接書き換える。
  - この変更はプロセスが生きている間だけ有効。
  - プロセス再起動（Docker restart / deploy）後は .env に戻る。
  - .env への永続化は TODO(Phase 2)。

レスポンスに含まれる persistence_mode: "runtime_only" でこの挙動を明示する。
フロントエンドは「再起動で設定が戻る」旨を画面に表示すること。

【変更時の必須要件】
- 全項目変更時に変更前後の差分確認ダイアログを表示する（フロントエンド責務）
- 保存後に監査ログへ記録する（サービス層責務 ✅ 実装済み）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from typing import Literal

from pydantic import BaseModel, Field

# 永続化モードの固定値。Phase 2 で "env_file" / "db" 等に変更する。
PERSISTENCE_MODE: Literal["runtime_only"] = "runtime_only"
PERSISTENCE_NOTE = (
    "この設定はプロセス実行中のみ有効です。"
    "再起動後は .env の値に戻ります（TODO Phase 2: 永続化未実装）。"
)


class SystemSettingsResponse(BaseModel):
    """
    現在のシステム設定値。

    persistence_mode == "runtime_only" の場合、変更はプロセス再起動で失われる。
    """
    # リスク管理
    daily_loss_limit_jpy: float = Field(..., description="日次損失上限（円）")
    max_concurrent_positions: int = Field(..., description="最大同時保有ポジション数")
    consecutive_losses_stop: int = Field(..., description="最大連続損失停止件数（0=無効）")

    # ポーリング間隔
    exit_watcher_interval_sec: int = Field(..., description="ExitWatcherポーリング間隔（秒）")
    order_poller_interval_sec: int | None = Field(
        default=None,
        description="OrderPollerポーリング間隔（秒）。現在は設定ファイル固定のため null の場合あり。"
    )
    strategy_runner_interval_sec: int = Field(..., description="Strategy Runner間隔（秒）")
    market_state_interval_sec: int = Field(..., description="MarketState更新間隔（秒）")

    # 許容古さ閾値
    strategy_max_state_age_sec: int = Field(..., description="Strategy最大State許容年齢（秒）")
    signal_max_decision_age_sec: int = Field(..., description="Signal最大Decision許容年齢（秒）")

    # 監視対象
    watched_symbols: list[str] = Field(..., description="監視銘柄コードリスト")

    # 永続化メタ情報
    persistence_mode: str = Field(
        default=PERSISTENCE_MODE,
        description=(
            "設定の永続化モード。"
            "'runtime_only' = プロセス再起動後に .env の値に戻る（暫定実装）。"
            "TODO(Phase 2): 'env_file' または 'db' に変更予定。"
        ),
    )
    persistence_note: str = Field(
        default=PERSISTENCE_NOTE,
        description="永続化に関する注意事項。フロントエンドで表示すること。",
    )


class SystemSettingsUpdateRequest(BaseModel):
    """
    システム設定更新リクエスト。
    変更したい項目のみ指定する（PATCH 相当）。
    変更は監査ログに記録される。

    ⚠️ 変更はランタイム上書きのみ。プロセス再起動後は .env に戻る。
    """
    daily_loss_limit_jpy: float | None = Field(default=None, gt=0)
    max_concurrent_positions: int | None = Field(default=None, ge=1)
    consecutive_losses_stop: int | None = Field(default=None, ge=0, description="0=無効")
    exit_watcher_interval_sec: int | None = Field(default=None, ge=1)
    strategy_runner_interval_sec: int | None = Field(default=None, ge=1)
    market_state_interval_sec: int | None = Field(default=None, ge=1)
    strategy_max_state_age_sec: int | None = Field(default=None, ge=1)
    signal_max_decision_age_sec: int | None = Field(default=None, ge=1)
    watched_symbols: list[str] | None = Field(
        default=None,
        description="監視銘柄コードリスト（空リスト=監視なし）"
    )


class SystemSettingsUpdateResponse(BaseModel):
    """
    システム設定更新レスポンス。

    ⚠️ 変更はランタイムのみ有効。再起動後は .env に戻る（TODO Phase 2）。
    persistence_mode を確認すること。
    """
    updated_fields: list[str] = Field(..., description="変更されたフィールド名のリスト")
    before: dict = Field(..., description="変更前の値（監査用）")
    after: dict = Field(..., description="変更後の値（監査用）")
    message: str
    persistence_mode: str = Field(
        default=PERSISTENCE_MODE,
        description="設定の永続化モード（'runtime_only' = 再起動で .env に戻る）",
    )
    persistence_note: str = Field(
        default=PERSISTENCE_NOTE,
        description="永続化に関する注意事項。",
    )
