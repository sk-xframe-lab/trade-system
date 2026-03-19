"""Strategy Engine Phase 1 テーブル追加:
  - strategy_definitions  : strategy コード定義マスタ
  - strategy_conditions   : strategy 条件（required/forbidden/size_modifier）
  - strategy_evaluations  : strategy 判定ログ（APPEND ONLY 時系列）

Revision ID: 007
Revises: 006
Create Date: 2026-03-16

設計制約:
  Strategy Engine は発注しない。StrategyDecisionResult を返すのみ。
  BrokerAdapter / OrderRouter への依存は持たない。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── strategy_definitions ─────────────────────────────────────────────
    op.create_table(
        "strategy_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("strategy_code", sa.String(64), nullable=False),
        sa.Column("strategy_name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # "long" | "short" | "both" — 将来の競合解決で使用
        sa.Column("direction", sa.String(16), nullable=False, server_default="both"),
        # 優先度: 将来 strategy 競合解決に使用（高い方が優先）
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        # False の場合 entry_allowed は常に False
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        # 最大ポジションサイズ比率（size_modifier と組み合わせて使用）
        sa.Column("max_size_ratio", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_code", name="uq_strategy_definitions_code"),
    )
    op.create_index(
        "ix_strategy_definitions_code", "strategy_definitions", ["strategy_code"]
    )
    op.create_index(
        "ix_strategy_definitions_enabled", "strategy_definitions", ["is_enabled"]
    )

    # ─── strategy_conditions ──────────────────────────────────────────────
    op.create_table(
        "strategy_conditions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("strategy_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # "required_state" | "forbidden_state" | "size_modifier"
        sa.Column("condition_type", sa.String(32), nullable=False),
        # "market" | "symbol" | "time_window"
        sa.Column("layer", sa.String(32), nullable=False),
        sa.Column("state_code", sa.String(64), nullable=False),
        # Phase 1: "exists" 固定。Phase 2+ で "gte" / "lte" を使用予定
        sa.Column("operator", sa.String(16), nullable=False, server_default="exists"),
        sa.Column("threshold_value", sa.Float(), nullable=True),
        sa.Column("size_modifier", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_strategy_conditions_strategy_id",
        "strategy_conditions",
        ["strategy_id"],
    )

    # ─── strategy_evaluations ─────────────────────────────────────────────
    op.create_table(
        "strategy_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        # strategy_definitions.id への論理参照（FK 制約なし: ログテーブル）
        sa.Column("strategy_id", postgresql.UUID(as_uuid=False), nullable=False),
        # None = 銘柄横断評価（market + time_window のみ）
        sa.Column("ticker", sa.String(64), nullable=True),
        sa.Column("evaluation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("entry_allowed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("size_ratio", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "matched_required_states_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "matched_forbidden_states_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "missing_required_states_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "blocking_reasons_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_strategy_evaluations_strategy_time",
        "strategy_evaluations",
        ["strategy_id", sa.text("evaluation_time DESC")],
    )
    op.create_index(
        "ix_strategy_evaluations_ticker_time",
        "strategy_evaluations",
        ["ticker", sa.text("evaluation_time DESC")],
    )


def downgrade() -> None:
    op.drop_table("strategy_evaluations")
    op.drop_table("strategy_conditions")
    op.drop_table("strategy_definitions")
