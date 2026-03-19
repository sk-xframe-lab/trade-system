"""Phase 3 テーブル追加・変更:
  - trading_halts          : 取引停止状態の永続管理（DB正本）
  - position_exit_transitions: ポジション出口状態遷移履歴（APPEND ONLY）
  - orders.signal_id       : NULL 許容化（exit注文はシグナルなし）
  - orders.position_id     : FK to positions（exit注文で使用）
  - orders.is_exit_order   : exit注文フラグ

Revision ID: 003
Revises: 002
Create Date: 2026-03-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── trading_halts ────────────────────────────────────────────────────
    op.create_table(
        "trading_halts",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("halt_type", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_by", sa.String(32), nullable=False, server_default="system"),
        sa.Column("deactivated_by", sa.String(32), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trading_halts_is_active", "trading_halts", ["is_active"])
    op.create_index("ix_trading_halts_type_active", "trading_halts", ["halt_type", "is_active"])
    op.create_index("ix_trading_halts_activated_at", "trading_halts", ["activated_at"])

    # ─── position_exit_transitions ────────────────────────────────────────
    # ポジションのクローズ状態遷移を記録する（APPEND ONLY）
    op.create_table(
        "position_exit_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("positions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("exit_reason", sa.String(32), nullable=True),
        sa.Column("triggered_by", sa.String(32), nullable=False, server_default="system"),
        sa.Column("exit_order_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_position_exit_transitions_position_id",
        "position_exit_transitions",
        ["position_id"],
    )
    op.create_index(
        "ix_position_exit_transitions_position_created",
        "position_exit_transitions",
        ["position_id", "created_at"],
    )

    # ─── orders テーブル変更 ───────────────────────────────────────────────
    # signal_id を NULL 許容化（exit注文はシグナルなし）
    op.alter_column("orders", "signal_id", nullable=True)

    # position_id: exit注文がどのポジションに対するものかを示す FK
    op.add_column(
        "orders",
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("positions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index("ix_orders_position_id", "orders", ["position_id"])

    # is_exit_order: True の場合は exit 注文（OrderPoller が別フローで処理）
    op.add_column(
        "orders",
        sa.Column(
            "is_exit_order",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.create_index("ix_orders_is_exit_order", "orders", ["is_exit_order"])


def downgrade() -> None:
    op.drop_index("ix_orders_is_exit_order", "orders")
    op.drop_index("ix_orders_position_id", "orders")
    op.drop_column("orders", "is_exit_order")
    op.drop_column("orders", "position_id")
    op.alter_column("orders", "signal_id", nullable=False)

    op.drop_table("position_exit_transitions")
    op.drop_table("trading_halts")
