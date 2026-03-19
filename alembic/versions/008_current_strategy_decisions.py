"""Strategy Runner Phase 1: current_strategy_decisions テーブル追加

  - current_strategy_decisions: (strategy_id, ticker) ごとの最新 decision 正本
    APPEND ONLY の strategy_evaluations とは別に保持するマテリアライズドビュー相当。
    StrategyRunner が評価サイクルごとに UPSERT する。

Revision ID: 008
Revises: 007
Create Date: 2026-03-16

設計制約:
  このテーブルは読み取り専用キャッシュ。発注には使用しない。
  StrategyRunner → StrategyEngine → DecisionRepository が書き込む唯一の経路。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── current_strategy_decisions ───────────────────────────────────────
    op.create_table(
        "current_strategy_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        # strategy_definitions.id への論理参照
        sa.Column("strategy_id", postgresql.UUID(as_uuid=False), nullable=False),
        # 非正規化: strategy_code を重複保持（JOIN 不要にするため）
        sa.Column("strategy_code", sa.String(64), nullable=False),
        # None = 銘柄横断評価（market + time_window のみ）
        sa.Column("ticker", sa.String(64), nullable=True),
        # ─── 判定結果 ────────────────────────────────────────────────────
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("entry_allowed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("size_ratio", sa.Float(), nullable=False, server_default="0.0"),
        # ─── 説明可能性 ──────────────────────────────────────────────────
        sa.Column(
            "blocking_reasons_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "matched_required_states_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "missing_required_states_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "matched_forbidden_states_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        # ─── タイムスタンプ ──────────────────────────────────────────────
        # 元になった strategy_evaluations の evaluation_time
        sa.Column("evaluation_time", sa.DateTime(timezone=True), nullable=False),
        # このレコードが最後に upsert された時刻
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # ticker × updated_at: 最新の ticker 別 decision 取得に使用
    op.create_index(
        "ix_csd_ticker_updated",
        "current_strategy_decisions",
        ["ticker", sa.text("updated_at DESC")],
    )
    # strategy_id: upsert 対象行の特定に使用
    op.create_index(
        "ix_csd_strategy_id",
        "current_strategy_decisions",
        ["strategy_id"],
    )
    # evaluation_time: 時系列クエリに使用
    op.create_index(
        "ix_csd_evaluation_time",
        "current_strategy_decisions",
        [sa.text("evaluation_time DESC")],
    )


def downgrade() -> None:
    op.drop_table("current_strategy_decisions")
