"""初回スキーマ作成（全テーブル）

Revision ID: 001
Revises:
Create Date: 2026-03-12

テーブル一覧:
  - trade_signals  : 受信シグナル
  - orders         : 発注記録
  - positions      : ポジション（建玉）
  - trade_results  : 確定損益
  - audit_logs     : 監査ログ（APPEND ONLY）
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── trade_signals ────────────────────────────────────────────────────
    op.create_table(
        "trade_signals",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("source_system", sa.String(64), nullable=False),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("signal_type", sa.String(16), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("strategy", sa.String(64), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="received"),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_trade_signals_idempotency_key", "trade_signals", ["idempotency_key"])
    op.create_index("ix_trade_signals_status", "trade_signals", ["status"])
    op.create_index(
        "ix_trade_signals_ticker_received",
        "trade_signals",
        ["ticker", "received_at"],
    )

    # ─── orders ───────────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "signal_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("trade_signals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("broker_order_id", sa.String(64), nullable=True),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("filled_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filled_price", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("broker_order_id"),
    )
    op.create_index("ix_orders_signal_id", "orders", ["signal_id"])
    op.create_index("ix_orders_broker_order_id", "orders", ["broker_order_id"])
    op.create_index("ix_orders_ticker_status", "orders", ["ticker", "status"])

    # ─── positions ────────────────────────────────────────────────────────
    op.create_table(
        "positions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("tp_price", sa.Float(), nullable=True),
        sa.Column("sl_price", sa.Float(), nullable=True),
        sa.Column("exit_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unrealized_pnl", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.String(16), nullable=True),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index("ix_positions_order_id", "positions", ["order_id"])
    op.create_index("ix_positions_ticker_status", "positions", ["ticker", "status"])
    op.create_index(
        "ix_positions_status_deadline", "positions", ["status", "exit_deadline"]
    )

    # ─── trade_results ────────────────────────────────────────────────────
    op.create_table(
        "trade_results",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("positions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=False),
        sa.Column("pnl_pct", sa.Float(), nullable=False),
        sa.Column("holding_minutes", sa.Integer(), nullable=True),
        sa.Column("exit_reason", sa.String(16), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("position_id"),
    )
    op.create_index("ix_trade_results_position_id", "trade_results", ["position_id"])
    op.create_index("ix_trade_results_ticker", "trade_results", ["ticker"])
    op.create_index(
        "ix_trade_results_ticker_created", "trade_results", ["ticker", "created_at"]
    )

    # ─── audit_logs ───────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("actor", sa.String(16), nullable=False, server_default="system"),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"])
    op.create_index(
        "ix_audit_logs_event_created", "audit_logs", ["event_type", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("trade_results")
    op.drop_table("positions")
    op.drop_table("orders")
    op.drop_table("trade_signals")
