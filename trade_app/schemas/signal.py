"""
シグナル関連の Pydantic スキーマ（API リクエスト / レスポンス）

シグナル送信仕様:
  POST /api/signals
  Headers:
    Authorization: Bearer <token>         # 必須
    Idempotency-Key: <uuid4>              # 必須（重複防止）
    X-Source-System: stock-analysis-v1   # 必須（送信元識別）
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from trade_app.models.enums import OrderType, Side


class SignalRequest(BaseModel):
    """
    分析システムが送信するシグナルの JSON ボディ仕様。
    この仕様に合わせて分析システム側を実装すること。
    """

    # ─── 銘柄・注文内容 ────────────────────────────────────────────────────
    ticker: str = Field(
        ...,
        description="銘柄コード（4〜5桁、.T なし）",
        examples=["7203", "9432"],
        min_length=4,
        max_length=5,
    )
    signal_type: str = Field(
        ...,
        description="シグナル種別: 'entry' または 'exit'",
        examples=["entry"],
    )
    order_type: OrderType = Field(
        ...,
        description="注文種別: 'market'（成行）または 'limit'（指値）",
    )
    side: Side = Field(
        ...,
        description="売買区分: 'buy' または 'sell'",
    )
    quantity: int = Field(
        ...,
        description="注文数量（株数）",
        ge=1,
        examples=[100],
    )
    limit_price: float | None = Field(
        default=None,
        description="指値価格（order_type=limit の場合は必須）",
        examples=[2850.0],
    )
    stop_price: float | None = Field(
        default=None,
        description="逆指値価格（将来実装）",
    )

    # ─── 参考情報 ─────────────────────────────────────────────────────────
    strategy: str | None = Field(
        default=None,
        description="シグナル生成戦略名（ログ・分析用）",
        examples=["breakout", "scout_night"],
        max_length=64,
    )
    score: float | None = Field(
        default=None,
        description="デイトレスコア（参考値・リスク判断には使用しない）",
        examples=[82.5],
    )
    generated_at: datetime = Field(
        ...,
        description="分析システムがシグナルを生成した日時（タイムゾーン付き）",
        examples=["2026-03-12T08:30:00+09:00"],
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="任意の追加情報（grade, source_job 等）",
        examples=[{"grade": "S", "source_job": "scout_night_job"}],
    )

    # ─── バリデーション ────────────────────────────────────────────────────
    @field_validator("ticker")
    @classmethod
    def ticker_must_be_digits(cls, v: str) -> str:
        """銘柄コードは数字のみ（.T サフィックスは不可）"""
        if not v.isdigit():
            raise ValueError("ticker は数字のみ（例: '7203'）。'.T' を含めないこと")
        return v

    @field_validator("signal_type")
    @classmethod
    def signal_type_must_be_valid(cls, v: str) -> str:
        if v not in ("entry", "exit"):
            raise ValueError("signal_type は 'entry' または 'exit' のみ")
        return v

    @model_validator(mode="after")
    def limit_price_required_for_limit_order(self) -> "SignalRequest":
        """order_type=limit のとき limit_price は必須"""
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("order_type=limit の場合は limit_price を指定すること")
        return self


class SignalAcceptedResponse(BaseModel):
    """POST /api/signals の成功レスポンス（202 Accepted）"""

    signal_id: str = Field(description="シグナルのUUID（状態照会に使用）")
    status: str = Field(default="accepted", description="処理状態")
    idempotency_key: str = Field(description="受け取った冪等性キー")
    message: str = Field(default="Signal accepted for processing")


class SignalDuplicateResponse(BaseModel):
    """POST /api/signals の重複レスポンス（409 Conflict）"""

    signal_id: str = Field(description="既存シグナルのUUID")
    status: str = Field(default="duplicate")
    message: str = Field(default="Already processed with this Idempotency-Key")


class SignalStatusResponse(BaseModel):
    """GET /api/signals/{signal_id} のレスポンス"""

    signal_id: str
    ticker: str
    side: str
    order_type: str
    quantity: int
    limit_price: float | None
    strategy: str | None
    score: float | None
    status: str
    reject_reason: str | None
    generated_at: datetime
    received_at: datetime

    model_config = {"from_attributes": True}


class SignalStrategyDecisionResponse(BaseModel):
    """GET /api/signals/{signal_id}/strategy-decision のレスポンス"""

    id: str
    signal_id: str
    ticker: str
    signal_direction: str
    global_decision_id: str | None
    symbol_decision_id: str | None
    decision_time: datetime
    entry_allowed: bool
    size_ratio: float
    blocking_reasons: list[str]
    evidence: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}
