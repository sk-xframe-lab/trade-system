"""Signal Router Integration Gate Phase 1: signal_strategy_decisions テーブル追加

  - signal_strategy_decisions: Signal ごとの Strategy Gate 判定結果監査テーブル
    Signal が Strategy Gate を通過するたびに APPEND ONLY で追記する。
    strategy_gate_rejected 時の理由追跡・監査に使用。

Revision ID: 009
Revises: 008
Create Date: 2026-03-16

設計制約:
  Strategy Gate は発注しない。entry_allowed の判定を返すのみ。
  RiskManager を置き換えない（前段ゲートとして動く）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── signal_strategy_decisions ────────────────────────────────────────
    op.create_table(
        "signal_strategy_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        # trade_signals.id への論理参照（FK 制約なし: 監査ログテーブル）
        sa.Column("signal_id", sa.String(36), nullable=False),
        sa.Column("ticker", sa.String(16), nullable=False),
        # "long" または "short"（signal.side から導出: buy→long, sell→short）
        sa.Column("signal_direction", sa.String(8), nullable=False),
        # 参照した global decision の id（nullable: 対応する decision がない場合）
        sa.Column("global_decision_id", sa.String(36), nullable=True),
        # 参照した ticker decision の id（nullable: 対応する decision がない場合）
        sa.Column("symbol_decision_id", sa.String(36), nullable=True),
        # Gate 判定時刻（SignalStrategyGate.check() の now）
        sa.Column("decision_time", sa.DateTime(timezone=True), nullable=False),
        # Gate 最終結果
        sa.Column("entry_allowed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("size_ratio", sa.Float(), nullable=False, server_default="0.0"),
        # ─── 説明可能性 ──────────────────────────────────────────────────
        sa.Column(
            "blocking_reasons_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # signal_id: signal ごとの gate 結果を素早く取得
    op.create_index(
        "ix_ssd_signal_id",
        "signal_strategy_decisions",
        ["signal_id"],
    )
    # (ticker, decision_time DESC): ticker 別時系列クエリに使用
    op.create_index(
        "ix_ssd_ticker_decision_time",
        "signal_strategy_decisions",
        ["ticker", sa.text("decision_time DESC")],
    )


def downgrade() -> None:
    op.drop_table("signal_strategy_decisions")
