"""Add fo_universe table — master F&O eligible instrument registry.

Single source of truth for all NSE F&O-eligible instruments with:
  - Symbol, company name, ISIN
  - F&O listed / delisted dates (prevents survivorship bias)
  - Spot listed / delisted dates
  - Backfill priority (FO index=1, FO stock=2, broad index=3, Nifty50=4, other=5)
  - Provider keys (Upstox, Angel One, Yahoo)
  - Successor symbol for merged/renamed instruments

Revision ID: 20260920_000000
Revises: 20260919_000000
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_000000"
down_revision: str = "20260919_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fo_universe",
        # ── PK ────────────────────────────────────────────────────────────
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),

        # ── Identity ──────────────────────────────────────────────────────
        sa.Column("instrument_id",    sa.String(64),  nullable=False,
                  comment="Canonical ID: exchange:symbol  e.g. NSE:RELIANCE"),
        sa.Column("symbol",           sa.String(64),  nullable=False,
                  comment="NSE trading symbol"),
        sa.Column("company_name",     sa.String(256), nullable=True,
                  comment="Full company / index name"),
        sa.Column("isin",             sa.String(12),  nullable=True,
                  comment="SEBI ISIN — NULL for indices"),

        # ── Classification ────────────────────────────────────────────────
        sa.Column("exchange",         sa.String(8),   nullable=False, server_default="NSE"),
        sa.Column("instrument_class", sa.String(8),   nullable=False,
                  comment="EQ | IDX | FO | ETF"),
        sa.Column("instrument_type",  sa.String(16),  nullable=False, server_default="EQ",
                  comment="EQ | IDX | FUTSTK | FUTIDX | ETF"),
        sa.Column("sector",           sa.String(64),  nullable=True,
                  comment="Broad sector label"),
        sa.Column("is_index",         sa.Boolean(),   nullable=False, server_default="FALSE",
                  comment="True for index underlyings"),

        # ── Backfill priority ─────────────────────────────────────────────
        sa.Column("backfill_priority", sa.Integer(), nullable=False, server_default="5",
                  comment="1=IndexFutures 2=StockFutures 3=BroadIndices 4=Nifty50 5=Other"),

        # ── F&O eligibility window ────────────────────────────────────────
        sa.Column("fo_listed_date",   sa.Date(), nullable=True,
                  comment="Date first admitted to F&O segment"),
        sa.Column("fo_delisted_date", sa.Date(), nullable=True,
                  comment="Date removed from F&O; NULL = currently active"),
        sa.Column("is_fo_active",     sa.Boolean(), nullable=False, server_default="TRUE",
                  comment="True if instrument currently has active F&O contracts"),

        # ── Spot window ───────────────────────────────────────────────────
        sa.Column("spot_listed_date",   sa.Date(), nullable=True),
        sa.Column("spot_delisted_date", sa.Date(), nullable=True,
                  comment="Date delisted/merged; NULL = still listed"),
        sa.Column("is_spot_active",     sa.Boolean(), nullable=False, server_default="TRUE",
                  comment="True if spot equity still tradeable on NSE"),

        # ── Corporate actions ─────────────────────────────────────────────
        sa.Column("successor_symbol", sa.String(64), nullable=True,
                  comment="For merged/renamed: the new symbol"),
        sa.Column("notes",            sa.Text(), nullable=True,
                  comment="Merger details, ticker changes, ban periods, etc."),

        # ── Contract specs ────────────────────────────────────────────────
        sa.Column("lot_size", sa.Integer(), nullable=True,
                  comment="F&O contract lot size"),

        # ── Provider keys ─────────────────────────────────────────────────
        sa.Column("upstox_key",    sa.String(128), nullable=True,
                  comment="Upstox V3 instrument key e.g. NSE_EQ|INE002A01018"),
        sa.Column("angel_token",   sa.String(32),  nullable=True,
                  comment="Angel One numeric scrip token"),
        sa.Column("yahoo_symbol",  sa.String(32),  nullable=True,
                  comment="Yahoo Finance ticker e.g. RELIANCE.NS"),

        # ── Timestamps ────────────────────────────────────────────────────
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )

    # Unique constraint
    op.create_unique_constraint(
        "fou_symbol_exchange_uq", "fo_universe", ["symbol", "exchange"]
    )

    # Check constraints
    op.create_check_constraint(
        "fou_instrument_class_valid", "fo_universe",
        "instrument_class IN ('EQ','IDX','FO','ETF')",
    )
    op.create_check_constraint(
        "fou_priority_range", "fo_universe",
        "backfill_priority BETWEEN 1 AND 10",
    )
    op.create_check_constraint(
        "fou_fo_dates_consistent", "fo_universe",
        "fo_delisted_date IS NULL OR fo_delisted_date >= fo_listed_date",
    )

    # Indexes
    op.create_index("fou_priority_active",   "fo_universe",
                    ["backfill_priority", "is_fo_active"])
    op.create_index("fou_instrument_class",  "fo_universe",
                    ["instrument_class", "backfill_priority"])
    op.create_index("fou_fo_active",         "fo_universe", ["is_fo_active"],
                    postgresql_where=sa.text("is_fo_active = TRUE"))
    op.create_index("fou_isin",              "fo_universe", ["isin"],
                    postgresql_where=sa.text("isin IS NOT NULL"))
    op.create_index("fou_instrument_id",     "fo_universe", ["instrument_id"])


def downgrade() -> None:
    op.drop_table("fo_universe")
