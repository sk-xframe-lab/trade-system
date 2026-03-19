"""
SymbolConfig モデル — 銘柄設定

仕様書: 管理画面仕様書 v0.3 §3(SCR-04/05), §7(DBエンティティ案 > symbol_configs)

【migration】
alembic_admin/ チェーンで管理。alembic_admin/versions/001_admin_initial.py を参照。
strategy_id の外部キー参照先 (strategy_configs) は O-4 確定待ちのため FK なし。
created_by / updated_by の FK（ui_users.id）は admin_db 内で完結するため技術的に
追加可能だが、I-4（OAuth）完了前は実ユーザーが存在しないため追加不可。

【symbol_code の扱い】
- 正本。主キー的役割。新規作成後は変更不可。
- コード: NOT NULL UNIQUE。

【symbol_name の扱い】
- 表示補助項目。nullable。後から同期可能。
- 空欄時は UI が symbol_code で代替表示する。
- 立花 API からの自動取得可否は T-2 (未確定)。Phase 1 は手動入力。

【strategy_id の外部キー】
- strategy_configs テーブルへの参照だが、strategy_configs は migration 保留中。
- ForeignKey 定義はコメントアウトし、文字列カラムとして保持する。
- I-1 / O-4 確定後に FK 制約を追加すること。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Numeric, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from trade_app.admin.database import AdminBase


class SymbolConfig(AdminBase):
    """銘柄設定テーブル"""

    __tablename__ = "symbol_configs"

    # ─── 主キー ──────────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ─── 銘柄識別 ─────────────────────────────────────────────────────────────
    # 正本。新規作成後は変更不可。
    symbol_code: Mapped[str] = mapped_column(String(8), nullable=False, unique=True)
    # 表示補助。nullable。後から同期可能。
    # TODO(T-2): 立花API から自動取得できるか確定後に自動同期実装
    symbol_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # ─── 基本設定 ─────────────────────────────────────────────────────────────
    # "daytrading" / "swing"
    trade_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # strategy_configs.id への参照
    # TODO(O-4): strategy_configs テーブル確定後に ForeignKey 制約を追加すること
    strategy_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ─── 取引設定 ─────────────────────────────────────────────────────────────
    # "opening_only" / "avoid" / "normal"
    open_behavior: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    # 取引時間帯（null = 戦略デフォルト使用）
    trading_start_time: Mapped[object | None] = mapped_column(Time, nullable=True)
    trading_end_time: Mapped[object | None] = mapped_column(Time, nullable=True)
    # 資金上限（円）
    max_single_investment_jpy: Mapped[int] = mapped_column(nullable=False)
    max_daily_investment_jpy: Mapped[int] = mapped_column(nullable=False)

    # ─── エグジット設定 ──────────────────────────────────────────────────────
    # エントリー価格からの上昇率（利確）
    take_profit_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    # エントリー価格からの下落率（損切）。正値で入力。
    stop_loss_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    # TimeStop 用。最大保有分数。
    max_hold_minutes: Mapped[int] = mapped_column(nullable=False)

    # ─── 操作者（監査用） ─────────────────────────────────────────────────────
    # TODO(I-4): admin_db 内完結の FK（→ ui_users.id）。技術的に Phase 1 追加可能だが
    # 実ユーザーが存在しない I-4（OAuth）完了前に制約を追加すると新規作成が失敗する。
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # ─── タイムスタンプ ───────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    # 論理削除
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_symbol_configs_code", "symbol_code"),
        Index("ix_symbol_configs_enabled", "is_enabled"),
        Index("ix_symbol_configs_trade_type", "trade_type"),
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def display_name(self) -> str:
        """UI表示用名称。symbol_name が未設定なら symbol_code を返す"""
        return self.symbol_name or self.symbol_code

    def __repr__(self) -> str:
        return (
            f"<SymbolConfig code={self.symbol_code} "
            f"name={self.symbol_name!r} enabled={self.is_enabled}>"
        )
