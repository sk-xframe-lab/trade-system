"""Market State Engine Phase 1 テーブル追加:
  - state_definitions       : 状態コード定義マスタ
  - state_evaluations       : 状態評価ログ（時系列 APPEND ONLY）
  - current_state_snapshots : 現在状態スナップショット（layer × target ごとに 1 行）

Revision ID: 005
Revises: 004
Create Date: 2026-03-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── state_definitions ───────────────────────────────────────────────
    op.create_table(
        "state_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("state_code", sa.String(64), nullable=False),
        sa.Column("state_name", sa.String(128), nullable=False),
        sa.Column("layer", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("block_entry", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("block_exit", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("reduce_size_ratio", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_code", name="uq_state_definitions_state_code"),
    )
    op.create_index("ix_state_definitions_layer", "state_definitions", ["layer"])
    op.create_index("ix_state_definitions_is_active", "state_definitions", ["is_active"])

    # ─── state_evaluations ───────────────────────────────────────────────
    op.create_table(
        "state_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("layer", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_code", sa.String(64), nullable=True),
        sa.Column("evaluation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_code", sa.String(64), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_state_evaluations_target_time",
        "state_evaluations",
        ["target_type", "target_code", sa.text("evaluation_time DESC")],
    )
    op.create_index(
        "ix_state_evaluations_state_active",
        "state_evaluations",
        ["state_code", "is_active"],
    )
    op.create_index(
        "ix_state_evaluations_layer_time",
        "state_evaluations",
        ["layer", sa.text("evaluation_time DESC")],
    )

    # ─── current_state_snapshots ─────────────────────────────────────────
    op.create_table(
        "current_state_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("layer", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_code", sa.String(64), nullable=True),
        sa.Column("active_states_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state_summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_current_state_snapshots_layer_target",
        "current_state_snapshots",
        ["layer", "target_type", "target_code"],
    )


def downgrade() -> None:
    op.drop_table("current_state_snapshots")
    op.drop_table("state_evaluations")
    op.drop_table("state_definitions")
