"""Add depth_json + source_type to market_quote; add oi/volume/ltp to option_greeks_snapshot;
add unique constraint for market_quote upsert support.

Revision ID: 20260917_000000
Revises: 20260916_000000
Create Date: 2026-09-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "20260917_000000"
down_revision: str = "20260916_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── market_quote: add depth_json, source_type, change, change_pct, avg_traded_price ──
    op.add_column(
        "market_quote",
        sa.Column("depth_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True,
                  comment="Market depth buy/sell levels serialised as JSON"),
    )
    op.add_column(
        "market_quote",
        sa.Column("source_type", sa.String(32), nullable=True,
                  comment="BROKER_AUTHENTICATED | OPEN_SOURCE_NSE_DERIVED"),
    )
    op.add_column(
        "market_quote",
        sa.Column("change", sa.Numeric(precision=18, scale=6), nullable=True),
    )
    op.add_column(
        "market_quote",
        sa.Column("change_pct", sa.Numeric(precision=10, scale=4), nullable=True),
    )
    op.add_column(
        "market_quote",
        sa.Column("avg_traded_price", sa.Numeric(precision=18, scale=6), nullable=True),
    )

    # unique constraint needed for ON CONFLICT upsert
    op.create_unique_constraint(
        "mq_instrument_exchange_ts_provider_uq",
        "market_quote",
        ["instrument_id", "exchange", "timestamp", "provider"],
    )

    # ── option_greeks_snapshot: add oi, volume, ltp ───────────────────────────────────
    op.add_column(
        "option_greeks_snapshot",
        sa.Column("oi", sa.BigInteger(), nullable=True,
                  comment="Open interest for this option contract"),
    )
    op.add_column(
        "option_greeks_snapshot",
        sa.Column("volume", sa.BigInteger(), nullable=True,
                  comment="Traded volume today"),
    )
    op.add_column(
        "option_greeks_snapshot",
        sa.Column("ltp", sa.Numeric(precision=18, scale=6), nullable=True,
                  comment="Last traded price (aliases option_price for clarity)"),
    )
    op.add_column(
        "option_greeks_snapshot",
        sa.Column("prev_close", sa.Numeric(precision=18, scale=6), nullable=True,
                  comment="Previous session close price"),
    )
    op.add_column(
        "option_greeks_snapshot",
        sa.Column("ltq", sa.BigInteger(), nullable=True,
                  comment="Last traded quantity"),
    )
    op.add_column(
        "option_greeks_snapshot",
        sa.Column("instrument_key", sa.String(128), nullable=True,
                  comment="Provider instrument key (e.g. NSE_FO|43885)"),
    )


def downgrade() -> None:
    op.drop_column("option_greeks_snapshot", "instrument_key")
    op.drop_column("option_greeks_snapshot", "ltq")
    op.drop_column("option_greeks_snapshot", "prev_close")
    op.drop_column("option_greeks_snapshot", "ltp")
    op.drop_column("option_greeks_snapshot", "volume")
    op.drop_column("option_greeks_snapshot", "oi")

    op.drop_constraint("mq_instrument_exchange_ts_provider_uq", "market_quote", type_="unique")
    op.drop_column("market_quote", "avg_traded_price")
    op.drop_column("market_quote", "change_pct")
    op.drop_column("market_quote", "change")
    op.drop_column("market_quote", "source_type")
    op.drop_column("market_quote", "depth_json")
