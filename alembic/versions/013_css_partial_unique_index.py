"""current_state_snapshots に partial unique index を追加

  - uq_css_null_target   : (layer, target_type) UNIQUE WHERE target_code IS NULL
  - uq_css_symbol_target : (layer, target_type, target_code) UNIQUE WHERE target_code IS NOT NULL

事前条件: current_state_snapshots に重複行がゼロであること

Revision ID: 013
Revises: 012
Create Date: 2026-03-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_css_null_target",
        "current_state_snapshots",
        ["layer", "target_type"],
        unique=True,
        postgresql_where=sa.text("target_code IS NULL"),
    )
    op.create_index(
        "uq_css_symbol_target",
        "current_state_snapshots",
        ["layer", "target_type", "target_code"],
        unique=True,
        postgresql_where=sa.text("target_code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_css_null_target", table_name="current_state_snapshots")
    op.drop_index("uq_css_symbol_target", table_name="current_state_snapshots")
