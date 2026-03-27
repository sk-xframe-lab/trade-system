"""daily_price_history テーブル追加

日次 OHLCV データを保存する。
MA5 / MA20 / ATR14 / RSI14 はアプリ側で計算するため、raw OHLCV のみを格納する。

Revision ID: 015
Revises: 014
Create Date: 2026-03-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_price_history",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(12, 2), nullable=True),
        sa.Column("high", sa.Numeric(12, 2), nullable=True),
        sa.Column("low", sa.Numeric(12, 2), nullable=True),
        sa.Column("close", sa.Numeric(12, 2), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # UNIQUE: ticker ごとに 1 取引日 1 行
    op.create_index(
        "uq_dph_ticker_date",
        "daily_price_history",
        ["ticker", "trading_date"],
        unique=True,
    )
    # 直近 N 行取得クエリ用
    op.create_index(
        "ix_dph_ticker_date",
        "daily_price_history",
        ["ticker", "trading_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_dph_ticker_date", table_name="daily_price_history")
    op.drop_index("uq_dph_ticker_date", table_name="daily_price_history")
    op.drop_table("daily_price_history")
