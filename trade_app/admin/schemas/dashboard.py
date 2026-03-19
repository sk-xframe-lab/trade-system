"""
ダッシュボード スキーマ (SCR-03)

仕様書: 管理画面仕様書 v0.3 §3(SCR-03)

【指標の定義】
- order_request_count: システムが取引エンジンに発注を要求した件数（Gate/RiskCheck通過後）
- broker_accepted_count: 立花証券がOrderを受け付けた件数（SUBMITTED以上に遷移した件数）
- filled_count: FILLEDになった件数（entry/exit合算）
- failed_count: REJECTED / ERRORになった件数

集計期間: 本日 JST 0:00 〜 現在時刻
"""
from datetime import datetime
from pydantic import BaseModel, Field


class HaltStatusItem(BaseModel):
    """アクティブな halt の情報"""
    id: str
    halt_type: str
    reason: str
    activated_at: datetime
    activated_by: str

    model_config = {"from_attributes": True}


class EnvironmentBanner(BaseModel):
    """環境バナー情報（デモ/本番混同防止）"""
    environment: str = Field(..., description="'demo' または 'production' または 'not_configured'")
    label: str = Field(..., description="バナー表示テキスト")
    style: str = Field(..., description="'warning'(デモ) / 'danger'(本番) / 'muted'(未設定)")


class TodaySummary(BaseModel):
    """本日（JST）の取引サマリー"""
    order_request_count: int = Field(..., description="発注要求件数（Gate/RiskCheck通過後）")
    broker_accepted_count: int = Field(..., description="ブローカー受付件数（SUBMITTED以上）")
    filled_count: int = Field(..., description="約定件数（entry+exit合算）")
    filled_entry_count: int = Field(..., description="うちエントリー約定件数")
    filled_exit_count: int = Field(..., description="うちエグジット約定件数")
    failed_count: int = Field(..., description="発注失敗件数（REJECTED/ERROR）")
    realized_pnl_jpy: float = Field(..., description="本日の実現損益（円）。クローズ済みポジションのみ。")


class SystemStatusSummary(BaseModel):
    """システム稼働状況"""
    is_running: bool
    is_halted: bool
    halt_count: int
    active_halts: list[HaltStatusItem]
    open_position_count: int
    closing_position_count: int
    enabled_strategy_count: int
    watched_symbol_count: int
    # 立花証券接続状態
    broker_api_status: str = Field(
        ...,
        description="'CONNECTED' / 'DISCONNECTED' / 'ERROR' / 'NOT_CONFIGURED'"
    )
    phone_auth_status: str = Field(
        ...,
        description="'CONFIRMED' / 'UNCONFIRMED' / 'UNKNOWN' / 'NOT_CONFIGURED'"
    )


class RecentActivity(BaseModel):
    """最近のアクティビティ（ダッシュボード下部）"""
    recent_fills: list[dict] = Field(
        default_factory=list,
        description="直近10件の約定情報"
    )
    recent_audit_logs: list[dict] = Field(
        default_factory=list,
        description="直近5件の監査ログ"
    )


class DashboardResponse(BaseModel):
    """ダッシュボード全体レスポンス"""
    environment_banner: EnvironmentBanner
    system_status: SystemStatusSummary
    today_summary: TodaySummary
    recent_activity: RecentActivity
    retrieved_at: datetime = Field(..., description="取得日時（UTC）")
