"""add cancel_requested_at to orders

Revision ID: 011
Revises: 010
Create Date: 2026-03-16

orders.cancel_requested_at:
  キャンセル要求を broker に送信した時刻。
  ExitWatcher 等が cancel_order() を呼ぶ際に設定し、
  二重キャンセル要求の防止判断に使用する。
  NULL = キャンセル未要求。
"""
from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "cancel_requested_at")
