"""exit order の二重発注を DB 制約で防止する partial unique index を追加

同一 position_id に対して active な exit 注文
（pending / submitted / partial / unknown）が複数作成されるのを DB レベルで防ぐ。

事前条件: orders テーブルに該当する重複行がゼロであること
（migration 前チェック SQL は設計ドキュメント参照）

Revision ID: 014
Revises: 013
Create Date: 2026-03-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_exit_order_active_per_position",
        "orders",
        ["position_id"],
        unique=True,
        postgresql_where=sa.text(
            "is_exit_order = true"
            " AND status IN ('pending', 'submitted', 'partial', 'unknown')"
            " AND position_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_exit_order_active_per_position", table_name="orders")
