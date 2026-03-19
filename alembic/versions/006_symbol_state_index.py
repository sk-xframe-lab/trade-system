"""Symbol State Engine Phase 1 — state_evaluations index 追加

既存の 005 migration で作成済みのインデックス:
  ix_state_evaluations_target_time : (target_type, target_code, evaluation_time DESC)
  ix_state_evaluations_state_active: (state_code, is_active)
  ix_state_evaluations_layer_time  : (layer, evaluation_time DESC)

今回追加するインデックス:
  ix_state_eval_layer_target_time  : (layer, target_code, evaluation_time DESC)
    → GET /api/v1/market-state/symbols/{ticker} が
      WHERE layer='symbol' AND target_code=ticker ORDER BY evaluation_time DESC
      で使うクエリパターンに最適化。
    → 既存の target_time インデックスは target_type を先頭に持つため、
      layer で絞る場合は利用できない。重複なし。

Revision ID: 006
Revises: 005
Create Date: 2026-03-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # (layer, target_code, evaluation_time DESC)
    # 銘柄別の最新評価ログ取得 (GET /symbols/{ticker}) を高速化
    op.create_index(
        "ix_state_eval_layer_target_time",
        "state_evaluations",
        ["layer", "target_code", sa.text("evaluation_time DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_state_eval_layer_target_time", table_name="state_evaluations")
