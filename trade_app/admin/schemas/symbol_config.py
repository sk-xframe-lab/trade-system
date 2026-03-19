"""
SymbolConfig スキーマ (SCR-04 / SCR-05)

仕様書: 管理画面仕様書 v0.3 §3(SCR-04, SCR-05)
"""
from datetime import datetime, time
from pydantic import BaseModel, Field, field_validator, model_validator


# ─── 基底 / 共通フィールド ─────────────────────────────────────────────────


class SymbolConfigBase(BaseModel):
    """銘柄設定の編集可能フィールド（作成・更新で共通）"""

    # symbol_name: 表示補助・nullable
    symbol_name: str | None = Field(
        default=None,
        max_length=128,
        description="銘柄名（表示補助・任意）。未設定時は symbol_code で表示。"
    )
    trade_type: str = Field(..., description="'daytrading' または 'swing'")
    strategy_id: str | None = Field(default=None, description="適用する strategy_configs.id")
    is_enabled: bool = Field(default=False, description="有効フラグ。デフォルト無効。")
    notes: str | None = Field(default=None, max_length=2000)

    # 取引設定
    open_behavior: str = Field(
        default="normal",
        description="'opening_only' / 'avoid' / 'normal'"
    )
    trading_start_time: time | None = Field(default=None, description="取引開始時刻（HH:MM）。null=戦略デフォルト")
    trading_end_time: time | None = Field(default=None, description="取引終了時刻（HH:MM）。null=戦略デフォルト")
    max_single_investment_jpy: int = Field(..., gt=0, description="1回投入上限（円）")
    max_daily_investment_jpy: int = Field(..., gt=0, description="1日最大投入額（円）")

    # エグジット設定
    take_profit_pct: float = Field(..., gt=0, description="利確条件（%）。エントリー価格からの上昇率")
    stop_loss_pct: float = Field(..., gt=0, description="損切条件（%）。エントリー価格からの下落率。正値で入力。")
    max_hold_minutes: int = Field(..., gt=0, description="最大保有時間（分）。TimeStop用。")

    @field_validator("trade_type")
    @classmethod
    def validate_trade_type(cls, v: str) -> str:
        allowed = {"daytrading", "swing"}
        if v not in allowed:
            raise ValueError(f"trade_type は {allowed} のいずれかを指定してください")
        return v

    @field_validator("open_behavior")
    @classmethod
    def validate_open_behavior(cls, v: str) -> str:
        allowed = {"opening_only", "avoid", "normal"}
        if v not in allowed:
            raise ValueError(f"open_behavior は {allowed} のいずれかを指定してください")
        return v

    @model_validator(mode="after")
    def validate_investment_limits(self) -> "SymbolConfigBase":
        if self.max_single_investment_jpy > self.max_daily_investment_jpy:
            raise ValueError("max_single_investment_jpy は max_daily_investment_jpy 以下にしてください")
        return self

    @model_validator(mode="after")
    def validate_trading_time(self) -> "SymbolConfigBase":
        if (
            self.trading_start_time is not None
            and self.trading_end_time is not None
            and self.trading_start_time >= self.trading_end_time
        ):
            raise ValueError("trading_start_time は trading_end_time より前である必要があります")
        return self


# ─── 作成 ──────────────────────────────────────────────────────────────────


class SymbolConfigCreate(SymbolConfigBase):
    """銘柄設定作成リクエスト。symbol_code は作成後変更不可。"""
    symbol_code: str = Field(
        ...,
        min_length=1,
        max_length=8,
        description="銘柄コード（正本）。作成後は変更不可。"
    )

    @field_validator("symbol_code")
    @classmethod
    def validate_symbol_code(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol_code は空にできません")
        return v


# ─── 更新 ──────────────────────────────────────────────────────────────────


class SymbolConfigUpdate(SymbolConfigBase):
    """
    銘柄設定更新リクエスト。symbol_code は含めない（変更不可）。
    全フィールドが省略可能（PATCH 相当）。
    """
    symbol_name: str | None = None
    trade_type: str | None = None  # type: ignore[assignment]
    strategy_id: str | None = None
    is_enabled: bool | None = None
    notes: str | None = None
    open_behavior: str | None = None  # type: ignore[assignment]
    trading_start_time: time | None = None
    trading_end_time: time | None = None
    max_single_investment_jpy: int | None = Field(default=None, gt=0)  # type: ignore[assignment]
    max_daily_investment_jpy: int | None = Field(default=None, gt=0)  # type: ignore[assignment]
    take_profit_pct: float | None = Field(default=None, gt=0)  # type: ignore[assignment]
    stop_loss_pct: float | None = Field(default=None, gt=0)  # type: ignore[assignment]
    max_hold_minutes: int | None = Field(default=None, gt=0)  # type: ignore[assignment]

    @field_validator("trade_type")
    @classmethod
    def validate_trade_type(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"daytrading", "swing"}
        if v not in allowed:
            raise ValueError(f"trade_type は {allowed} のいずれかを指定してください")
        return v

    @field_validator("open_behavior")
    @classmethod
    def validate_open_behavior(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"opening_only", "avoid", "normal"}
        if v not in allowed:
            raise ValueError(f"open_behavior は {allowed} のいずれかを指定してください")
        return v

    @model_validator(mode="after")
    def validate_investment_limits(self) -> "SymbolConfigUpdate":
        s = self.max_single_investment_jpy
        d = self.max_daily_investment_jpy
        if s is not None and d is not None and s > d:
            raise ValueError("max_single_investment_jpy は max_daily_investment_jpy 以下にしてください")
        return self

    @model_validator(mode="after")
    def validate_trading_time(self) -> "SymbolConfigUpdate":
        s = self.trading_start_time
        e = self.trading_end_time
        if s is not None and e is not None and s >= e:
            raise ValueError("trading_start_time は trading_end_time より前である必要があります")
        return self


# ─── レスポンス ────────────────────────────────────────────────────────────


class SymbolConfigResponse(BaseModel):
    """銘柄設定レスポンス"""
    id: str
    symbol_code: str
    symbol_name: str | None
    display_name: str  # symbol_name が null なら symbol_code を返す
    trade_type: str
    strategy_id: str | None
    is_enabled: bool
    open_behavior: str
    trading_start_time: time | None
    trading_end_time: time | None
    max_single_investment_jpy: int
    max_daily_investment_jpy: int
    take_profit_pct: float
    stop_loss_pct: float
    max_hold_minutes: int
    notes: str | None
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SymbolConfigListResponse(BaseModel):
    """銘柄設定一覧レスポンス"""
    id: str
    symbol_code: str
    display_name: str
    trade_type: str
    is_enabled: bool
    strategy_id: str | None
    updated_by: str | None
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── フィルタ ─────────────────────────────────────────────────────────────


class SymbolConfigFilter(BaseModel):
    """銘柄一覧フィルタ条件"""
    trade_type: str | None = None
    is_enabled: bool | None = None
    strategy_id: str | None = None
    search: str | None = Field(default=None, description="symbol_code / symbol_name の部分一致検索")
    include_deleted: bool = Field(default=False, description="論理削除済みを含めるか")
