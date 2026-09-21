"""Add continuous_futures table for Panama-adjusted continuous F&O price series.

This table is the ML-ready continuous futures dataset built by
``scripts/build_continuous_futures.py``.  It stores a per-underlying,
gap-free, Panama backward-adjusted OHLCV+OI series suitable for training
time-series models without expiry-roll price discontinuities.

Revision ID: 20260919_000000
Revises: 20260918_000000
Create Date: 2026-09-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_000000"
down_revision: str = "20260918_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "continuous_futures",
        sa.Column("id",                    sa.BigInteger(),               sa.Identity(), primary_key=True),
        sa.Column("underlying_id",         sa.String(64),                 nullable=False),
        sa.Column("exchange",              sa.String(8),                  nullable=False, server_default="NFO"),
        sa.Column("date",                  sa.Date(),                     nullable=False),
        sa.Column("open",                  sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high",                  sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low",                   sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close",                 sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("volume",                sa.BigInteger(),               nullable=False, server_default="0"),
        sa.Column("open_interest",         sa.BigInteger(),               nullable=True),
        sa.Column("oi_change",             sa.BigInteger(),               nullable=True),
        # Which physical contract was used for this day's OHLCV
        sa.Column("expiry_in_use",         sa.Date(),                     nullable=False),
        # Non-NULL only on the actual roll date
        sa.Column("roll_date",             sa.Date(),                     nullable=True),
        # Panama backward-adjustment factor. original_price = adj_price * cumulative_adj
        sa.Column("cumulative_adj",        sa.Numeric(precision=20, scale=8), nullable=False, server_default="1.0"),
        sa.Column("adjustment_type",       sa.String(16),                 nullable=False, server_default="PANAMA_RATIO",
                  comment="PANAMA_RATIO | PANAMA_ADDITIVE"),
        # Provenance
        sa.Column("provider",              sa.String(32),                 nullable=False, server_default="derived_panama"),
        sa.Column("source_type",           sa.String(32),                 nullable=False, server_default="DERIVED"),
        sa.Column("normalisation_version", sa.String(16),                 nullable=False, server_default="2.0.0"),
        sa.Column("created_at",            sa.TIMESTAMP(timezone=True),   nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at",            sa.TIMESTAMP(timezone=True),   nullable=False, server_default=sa.text("NOW()")),
    )

    # Business key — one row per (underlying, exchange, date)
    op.create_unique_constraint(
        "cf_underlying_exchange_date_uq",
        "continuous_futures",
        ["underlying_id", "exchange", "date"],
    )

    # Query indexes
    op.create_index(
        "cf_underlying_date",
        "continuous_futures",
        ["underlying_id", sa.text("date DESC")],
    )
    op.create_index(
        "cf_exchange_date",
        "continuous_futures",
        ["exchange", sa.text("date DESC")],
    )
    op.create_index(
        "cf_roll_dates",
        "continuous_futures",
        ["underlying_id", "roll_date"],
        postgresql_where=sa.text("roll_date IS NOT NULL"),
    )

    # OHLC integrity check
    op.create_check_constraint(
        "cf_high_gte_low",
        "continuous_futures",
        "high >= low",
    )
    op.create_check_constraint(
        "cf_volume_non_negative",
        "continuous_futures",
        "volume >= 0",
    )
    op.create_check_constraint(
        "cf_cumulative_adj_positive",
        "continuous_futures",
        "cumulative_adj > 0",
    )


def downgrade() -> None:
    op.drop_table("continuous_futures")
