"""Phase 2 テーブル追加: executions / broker_requests / broker_responses / system_events / order_state_transitions
OrderStatus に UNKNOWN を追加（既存カラムの CHECK 制約なし のため DDL 変更不要）

Revision ID: 002
Revises: 001
Create Date: 2026-03-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── executions ───────────────────────────────────────────────────────
    op.create_table(
        "executions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("broker_execution_id", sa.String(64), nullable=True),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("broker_execution_id"),
    )
    op.create_index("ix_executions_order_id", "executions", ["order_id"])
    op.create_index("ix_executions_broker_exec_id", "executions", ["broker_execution_id"])

    # ─── broker_requests ──────────────────────────────────────────────────
    op.create_table(
        "broker_requests",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("request_type", sa.String(16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_broker_requests_order_id", "broker_requests", ["order_id"])
    op.create_index("ix_broker_requests_type_sent", "broker_requests", ["request_type", "sent_at"])

    # ─── broker_responses ─────────────────────────────────────────────────
    op.create_table(
        "broker_responses",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "broker_request_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("broker_requests.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status_code", sa.String(16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_error", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_broker_responses_request_id", "broker_responses", ["broker_request_id"])
    op.create_index("ix_broker_responses_order_id", "broker_responses", ["order_id"])

    # ─── system_events ────────────────────────────────────────────────────
    op.create_table(
        "system_events",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_system_events_event_type", "system_events", ["event_type"])
    op.create_index("ix_system_events_type_created", "system_events", ["event_type", "created_at"])

    # ─── order_state_transitions ──────────────────────────────────────────
    op.create_table(
        "order_state_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(16), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_transitions_order_id", "order_state_transitions", ["order_id"])
    op.create_index(
        "ix_order_transitions_order_created",
        "order_state_transitions",
        ["order_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("order_state_transitions")
    op.drop_table("system_events")
    op.drop_table("broker_responses")
    op.drop_table("broker_requests")
    op.drop_table("executions")
