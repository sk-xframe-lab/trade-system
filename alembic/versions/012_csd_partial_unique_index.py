"""current_strategy_decisions に partial unique index を追加

  - uq_csd_null_ticker   : (strategy_id) UNIQUE WHERE ticker IS NULL
  - uq_csd_symbol_ticker : (strategy_id, ticker) UNIQUE WHERE ticker IS NOT NULL

二重起動（workers >= 2 等）や非同期 race condition による
同一 (strategy_id, ticker) への重複 INSERT を DB レベルで防止する。

Revision ID: 012
Revises: 011
Create Date: 2026-03-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_csd_null_ticker",
        "current_strategy_decisions",
        ["strategy_id"],
        unique=True,
        postgresql_where=sa.text("ticker IS NULL"),
    )
    op.create_index(
        "uq_csd_symbol_ticker",
        "current_strategy_decisions",
        ["strategy_id", "ticker"],
        unique=True,
        postgresql_where=sa.text("ticker IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_csd_null_ticker", table_name="current_strategy_decisions")
    op.drop_index("uq_csd_symbol_ticker", table_name="current_strategy_decisions")
