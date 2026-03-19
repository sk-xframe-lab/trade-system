"""add remaining_qty to positions

Revision ID: 004
Revises: 003
Create Date: 2026-03-16

positions.remaining_qty:
  CLOSING 中の exit 未決済残数量。
  initiate_exit() 時に quantity で初期化し、
  exit Execution が届くたびに apply_exit_execution() で減算する。
  remaining_qty == 0 になった時点で finalize_exit() を呼び出す。
  NULL の場合は未初期化（CLOSING 前のレコード互換）。
"""
from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("remaining_qty", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "remaining_qty")
