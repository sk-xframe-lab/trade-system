"""Signal Planning Layer Phase 9: signal_plans / signal_plan_reasons テーブル追加

  - signal_plans: Signal ごとの planning 結果（サイズ・執行パラメータ案）APPEND ONLY
  - signal_plan_reasons: 縮小・拒否理由の履歴（段階ごとに複数レコード）APPEND ONLY

Revision ID: 010
Revises: 009
Create Date: 2026-03-16

設計制約:
  Planning Layer は発注しない。planned_order_qty / planned_execution_params を返すのみ。
  RiskManager / BrokerAdapter を置き換えない（前段計画層として動く）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── signal_plans ─────────────────────────────────────────────────────
    op.create_table(
        "signal_plans",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        # trade_signals.id への論理参照（FK 制約なし: 監査ログテーブル）
        sa.Column("signal_id", sa.String(36), nullable=False),
        # signal_strategy_decisions.id への論理参照（nullable: bypass時）
        sa.Column("signal_strategy_decision_id", sa.String(36), nullable=True),
        # "accepted" / "reduced" / "rejected"
        sa.Column("planning_status", sa.String(16), nullable=False),
        # 計画発注数量（lot 丸め済み）
        sa.Column("planned_order_qty", sa.Integer(), nullable=False, server_default="0"),
        # 計画発注想定金額（planned_order_qty × limit_price）
        sa.Column("planned_notional", sa.Float(), nullable=True),
        # ─── 執行パラメータ候補 ─────────────────────────────────────────
        sa.Column("order_type_candidate", sa.String(16), nullable=True),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("max_slippage_bps", sa.Float(), nullable=True),
        sa.Column("participation_rate_cap", sa.Float(), nullable=True),
        sa.Column("entry_timeout_seconds", sa.Integer(), nullable=True),
        # ─── 計画詳細 ───────────────────────────────────────────────────
        # strategy gate から受け取った size_ratio（縮小前の比率）
        sa.Column("applied_size_ratio", sa.Float(), nullable=True),
        # 拒否時の最終理由コード（rejected 時のみ）
        sa.Column("rejection_reason_code", sa.String(64), nullable=True),
        # 各 stage の詳細トレース JSON
        sa.Column(
            "planning_trace_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # signal_id: signal ごとの plan 取得
    op.create_index("ix_sp_signal_id", "signal_plans", ["signal_id"])
    # planning_status: status 別集計クエリ用
    op.create_index("ix_sp_status_created", "signal_plans", ["planning_status", sa.text("created_at DESC")])

    # ─── signal_plan_reasons ──────────────────────────────────────────────
    op.create_table(
        "signal_plan_reasons",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        # signal_plans.id への論理参照（FK 制約なし: 監査ログテーブル）
        sa.Column("signal_plan_id", sa.String(36), nullable=False),
        # 理由コード（PlanningReasonCode enum の値）
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason_detail", sa.Text(), nullable=True),
        # その stage の入力スナップショット（デバッグ用）
        sa.Column(
            "input_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        # 調整前後の値（サイズ縮小 trace 用）
        sa.Column("adjustment_before", sa.Float(), nullable=True),
        sa.Column("adjustment_after", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # signal_plan_id: plan ごとの全理由取得
    op.create_index("ix_spr_plan_id", "signal_plan_reasons", ["signal_plan_id"])


def downgrade() -> None:
    op.drop_table("signal_plan_reasons")
    op.drop_table("signal_plans")
