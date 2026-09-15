"""v2 Production Schema — Indian Market Data Architecture Redesign.

Adds 16 new tables and enhances instrument_master to support the full
production-grade Indian market data architecture:

LAYER 1 — INSTRUMENT IDENTITY (enhanced + 2 new)
  instrument_master            — adds instrument_class, name columns
  instrument_provider_mapping  — NEW: normalises provider token storage
  instrument_identity_history  — NEW: symbol/token change audit trail

LAYER 2 — CANONICAL CANDLES (3 new TimescaleDB hypertables)
  equity_candle                — NSE equities + indices OHLCV
  futures_candle               — NSE F&O futures OHLCV + OI
  options_candle               — NSE F&O options OHLCV + OI

LAYER 3 — LIVE DATA (2 new TimescaleDB hypertables)
  market_tick                  — live WebSocket ticks
  market_quote                 — live quote snapshots

LAYER 4 — OPTION CHAIN (3 new, greeks is hypertable)
  option_chain_snapshot        — point-in-time chain header
  option_chain_contract        — per-strike chain rows
  option_greeks_snapshot       — IV + Greeks time-series

LAYER 5 — CALENDAR + SESSIONS (3 new)
  exchange_calendar            — NSE/BSE holiday + trading day registry
  market_session               — actual session open/close records
  fno_universe_membership      — point-in-time F&O eligibility

LAYER 6 — OPERATIONS (3 new)
  ingestion_job                — backfill/ingestion job tracking
  ingestion_checkpoint         — resumable job state
  candle_bar_quarantine        — unclassifiable migration rows

Safety guarantees:
  - candle_bar is NOT dropped or modified structurally
  - All new tables are additive
  - Migration is fully reversible via downgrade()
  - TimescaleDB hypertable promotion uses if_not_exists=TRUE (idempotent)

Revision ID: b1c2d3e4f5a6
Revises:     a1b2c3d4e5f6
Create Date: 2026-09-15 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---------------------------------------------------------------------------
# Quality status values — centralised here so CHECK constraints are consistent
# ---------------------------------------------------------------------------
_QUALITY_STATUS_CHECK = (
    "quality_status IN ("
    "'TRUSTED','DEGRADED','POOR_QUALITY','BLOCKED','MISSING','QUARANTINED','DERIVED'"
    ")"
)
_DATA_ORIGIN_CHECK = "data_origin IN ('PROVIDER', 'DERIVED')"
_INTERVAL_NOT_3M = "interval_str <> '3m'"
_OHLC_HIGH_GTE_OPEN = "high >= open"
_OHLC_HIGH_GTE_CLOSE = "high >= close"
_OHLC_LOW_LTE_OPEN = "low <= open"
_OHLC_LOW_LTE_CLOSE = "low <= close"
_OHLC_HIGH_GTE_LOW = "high >= low"


# ===========================================================================
# upgrade
# ===========================================================================

def upgrade() -> None:

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 1A — enhance instrument_master (additive columns only)
    # ────────────────────────────────────────────────────────────────────────
    op.add_column(
        "instrument_master",
        sa.Column(
            "instrument_class",
            sa.String(8),
            nullable=True,
            comment="EQ | IDX | FUT | OPT | CRYPTO | ETF",
        ),
    )
    op.add_column(
        "instrument_master",
        sa.Column(
            "name",
            sa.String(256),
            nullable=True,
            comment="Full display name independent of trading symbol",
        ),
    )
    op.create_index(
        "im_instrument_class",
        "instrument_master",
        ["instrument_class"],
        unique=False,
        postgresql_where=sa.text("instrument_class IS NOT NULL"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 1B — instrument_provider_mapping
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "instrument_provider_mapping",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_instrument_id", sa.String(128), nullable=True,
                  comment="Provider's native token/ID (e.g. Angel One token '2885')"),
        sa.Column("provider_symbol", sa.String(128), nullable=True,
                  comment="Provider's trading symbol"),
        sa.Column("exchange_segment", sa.String(32), nullable=True,
                  comment="Provider-specific segment string (e.g. 'NSE_EQ|INE002A01018')"),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True,
                  comment="NULL = currently active"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="TRUE"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id", name="instrument_provider_mapping_pkey"),
        sa.UniqueConstraint(
            "instrument_id", "provider", "valid_from",
            name="ipm_instrument_provider_valid_from_uq",
        ),
    )
    op.create_index(
        "ipm_provider_token",
        "instrument_provider_mapping",
        ["provider", "provider_instrument_id"],
        unique=False,
    )
    op.create_index(
        "ipm_active_by_instrument",
        "instrument_provider_mapping",
        ["instrument_id", "provider"],
        unique=False,
        postgresql_where=sa.text("is_active = TRUE"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 1C — instrument_identity_history
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "instrument_identity_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("old_symbol", sa.String(64), nullable=True),
        sa.Column("new_symbol", sa.String(64), nullable=True),
        sa.Column("exchange", sa.String(8), nullable=True),
        sa.Column("segment", sa.String(8), nullable=True),
        sa.Column(
            "change_type", sa.String(32), nullable=False,
            comment="SYMBOL_CHANGE | TOKEN_CHANGE | CORPORATE_ACTION | EXPIRY | DELISTED",
        ),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id", name="instrument_identity_history_pkey"),
    )
    op.create_index(
        "iih_instrument_id",
        "instrument_identity_history",
        ["instrument_id", sa.text("valid_from DESC")],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 2A — equity_candle  (TimescaleDB hypertable)
    #
    # Stores OHLCV for NSE/BSE equities AND indices (segment = EQ | IDX | ETF).
    # Partitioned by time (7-day chunks).
    # 3m interval permanently banned by CHECK constraint.
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "equity_candle",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column(
            "segment", sa.String(8), nullable=False, server_default="EQ",
            comment="EQ | IDX | ETF",
        ),
        sa.Column("interval_str", sa.String(4), nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False,
                  comment="Candle open timestamp UTC — hypertable partition key"),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("vwap", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("turnover", sa.Numeric(precision=24, scale=4), nullable=True,
                  comment="Total traded value INR"),
        sa.Column(
            "data_origin", sa.String(16), nullable=False, server_default="PROVIDER",
            comment="PROVIDER | DERIVED",
        ),
        sa.Column("derived_from_interval", sa.String(4), nullable=True,
                  comment="Source interval when data_origin=DERIVED"),
        sa.Column("aggregation_version", sa.String(16), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "normalisation_version", sa.String(16), nullable=False,
            server_default="2.0.0",
        ),
        sa.Column("dataset_version", sa.BigInteger(), nullable=False,
                  server_default="1"),
        sa.Column(
            "quality_status", sa.String(16), nullable=False,
            server_default="TRUSTED",
        ),
        sa.Column("poor_quality", sa.Boolean(), nullable=False,
                  server_default="FALSE"),
        sa.Column("reconciliation_status", sa.String(32), nullable=True),
        sa.Column("provenance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("volume_unavailable", sa.Boolean(), nullable=False,
                  server_default="FALSE"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        # ── Composite primary key — required for TimescaleDB hypertable ──
        sa.PrimaryKeyConstraint("id", "time", name="equity_candle_pkey"),
        # ── Data integrity constraints ────────────────────────────────────
        sa.CheckConstraint(_INTERVAL_NOT_3M, name="ec_no_3m_interval"),
        sa.CheckConstraint(_OHLC_HIGH_GTE_OPEN, name="ec_high_gte_open"),
        sa.CheckConstraint(_OHLC_HIGH_GTE_CLOSE, name="ec_high_gte_close"),
        sa.CheckConstraint(_OHLC_LOW_LTE_OPEN, name="ec_low_lte_open"),
        sa.CheckConstraint(_OHLC_LOW_LTE_CLOSE, name="ec_low_lte_close"),
        sa.CheckConstraint(_OHLC_HIGH_GTE_LOW, name="ec_high_gte_low"),
        sa.CheckConstraint("volume >= 0", name="ec_volume_non_negative"),
        sa.CheckConstraint(_DATA_ORIGIN_CHECK, name="ec_data_origin_valid"),
        sa.CheckConstraint(_QUALITY_STATUS_CHECK, name="ec_quality_status_valid"),
    )
    # Deterministic unique key — enables ON CONFLICT DO NOTHING / DO UPDATE
    op.create_index(
        "equity_candle_uq",
        "equity_candle",
        ["instrument_id", "exchange", "interval_str", "time"],
        unique=True,
    )
    # Primary query pattern: symbol + interval + time range (descending)
    op.create_index(
        "ec_instrument_interval_time",
        "equity_candle",
        ["instrument_id", "interval_str", sa.text("time DESC")],
        unique=False,
    )
    # Exchange + segment + interval scan
    op.create_index(
        "ec_exchange_segment_interval_time",
        "equity_candle",
        ["exchange", "segment", "interval_str", sa.text("time DESC")],
        unique=False,
    )
    # Quality filter — partial index for non-TRUSTED rows only
    op.create_index(
        "ec_quality_filter",
        "equity_candle",
        ["quality_status", "instrument_id", "interval_str", sa.text("time DESC")],
        unique=False,
        postgresql_where=sa.text("quality_status <> 'TRUSTED'"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 2B — futures_candle  (TimescaleDB hypertable)
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "futures_candle",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("underlying_id", sa.String(64), nullable=True,
                  comment="instrument_id of the underlying equity or index"),
        sa.Column("exchange", sa.String(8), nullable=False,
                  comment="NFO | BFO"),
        sa.Column("interval_str", sa.String(4), nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column(
            "contract_type", sa.String(8), nullable=False, server_default="FUT",
        ),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("oi_change", sa.BigInteger(), nullable=True),
        sa.Column("vwap", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("turnover", sa.Numeric(precision=24, scale=4), nullable=True),
        sa.Column(
            "data_origin", sa.String(16), nullable=False, server_default="PROVIDER",
        ),
        sa.Column("derived_from_interval", sa.String(4), nullable=True),
        sa.Column("aggregation_version", sa.String(16), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "normalisation_version", sa.String(16), nullable=False,
            server_default="2.0.0",
        ),
        sa.Column("dataset_version", sa.BigInteger(), nullable=False,
                  server_default="1"),
        sa.Column(
            "quality_status", sa.String(16), nullable=False,
            server_default="TRUSTED",
        ),
        sa.Column("poor_quality", sa.Boolean(), nullable=False,
                  server_default="FALSE"),
        sa.Column("reconciliation_status", sa.String(32), nullable=True),
        sa.Column("provenance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", "time", name="futures_candle_pkey"),
        sa.CheckConstraint(_INTERVAL_NOT_3M, name="fc_no_3m_interval"),
        sa.CheckConstraint(_OHLC_HIGH_GTE_OPEN, name="fc_high_gte_open"),
        sa.CheckConstraint(_OHLC_HIGH_GTE_CLOSE, name="fc_high_gte_close"),
        sa.CheckConstraint(_OHLC_LOW_LTE_OPEN, name="fc_low_lte_open"),
        sa.CheckConstraint(_OHLC_LOW_LTE_CLOSE, name="fc_low_lte_close"),
        sa.CheckConstraint(_OHLC_HIGH_GTE_LOW, name="fc_high_gte_low"),
        sa.CheckConstraint("volume >= 0", name="fc_volume_non_negative"),
        sa.CheckConstraint(
            "open_interest IS NULL OR open_interest >= 0",
            name="fc_oi_non_negative",
        ),
        sa.CheckConstraint("contract_type = 'FUT'", name="fc_contract_type_fut"),
        sa.CheckConstraint(_DATA_ORIGIN_CHECK, name="fc_data_origin_valid"),
        sa.CheckConstraint(_QUALITY_STATUS_CHECK, name="fc_quality_status_valid"),
    )
    op.create_index(
        "futures_candle_uq",
        "futures_candle",
        ["instrument_id", "exchange", "interval_str", "time"],
        unique=True,
    )
    op.create_index(
        "fc_instrument_interval_time",
        "futures_candle",
        ["instrument_id", "interval_str", sa.text("time DESC")],
        unique=False,
    )
    op.create_index(
        "fc_underlying_expiry_interval_time",
        "futures_candle",
        ["underlying_id", "expiry", "interval_str", sa.text("time DESC")],
        unique=False,
        postgresql_where=sa.text("underlying_id IS NOT NULL"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 2C — options_candle  (TimescaleDB hypertable)
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "options_candle",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("underlying_id", sa.String(64), nullable=True),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("interval_str", sa.String(4), nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("option_type", sa.String(2), nullable=False,
                  comment="CE | PE only"),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("oi_change", sa.BigInteger(), nullable=True),
        sa.Column("vwap", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("turnover", sa.Numeric(precision=24, scale=4), nullable=True),
        sa.Column(
            "data_origin", sa.String(16), nullable=False, server_default="PROVIDER",
        ),
        sa.Column("derived_from_interval", sa.String(4), nullable=True),
        sa.Column("aggregation_version", sa.String(16), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "normalisation_version", sa.String(16), nullable=False,
            server_default="2.0.0",
        ),
        sa.Column("dataset_version", sa.BigInteger(), nullable=False,
                  server_default="1"),
        sa.Column(
            "quality_status", sa.String(16), nullable=False,
            server_default="TRUSTED",
        ),
        sa.Column("poor_quality", sa.Boolean(), nullable=False,
                  server_default="FALSE"),
        sa.Column("reconciliation_status", sa.String(32), nullable=True),
        sa.Column("provenance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", "time", name="options_candle_pkey"),
        sa.CheckConstraint(_INTERVAL_NOT_3M, name="oc_no_3m_interval"),
        sa.CheckConstraint("option_type IN ('CE','PE')", name="oc_option_type_valid"),
        sa.CheckConstraint("strike > 0", name="oc_strike_positive"),
        sa.CheckConstraint(_OHLC_HIGH_GTE_OPEN, name="oc_high_gte_open"),
        sa.CheckConstraint(_OHLC_HIGH_GTE_CLOSE, name="oc_high_gte_close"),
        sa.CheckConstraint(_OHLC_LOW_LTE_OPEN, name="oc_low_lte_open"),
        sa.CheckConstraint(_OHLC_LOW_LTE_CLOSE, name="oc_low_lte_close"),
        sa.CheckConstraint(_OHLC_HIGH_GTE_LOW, name="oc_high_gte_low"),
        sa.CheckConstraint("volume >= 0", name="oc_volume_non_negative"),
        sa.CheckConstraint(
            "open_interest IS NULL OR open_interest >= 0",
            name="oc_oi_non_negative",
        ),
        sa.CheckConstraint(_DATA_ORIGIN_CHECK, name="oc_data_origin_valid"),
        sa.CheckConstraint(_QUALITY_STATUS_CHECK, name="oc_quality_status_valid"),
    )
    op.create_index(
        "options_candle_uq",
        "options_candle",
        ["instrument_id", "exchange", "interval_str", "time"],
        unique=True,
    )
    op.create_index(
        "oc_instrument_interval_time",
        "options_candle",
        ["instrument_id", "interval_str", sa.text("time DESC")],
        unique=False,
    )
    op.create_index(
        "oc_underlying_expiry_strike_type",
        "options_candle",
        ["underlying_id", "expiry", "strike", "option_type", "interval_str",
         sa.text("time DESC")],
        unique=False,
        postgresql_where=sa.text("underlying_id IS NOT NULL"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 3A — market_tick  (TimescaleDB hypertable, 1-day chunks, 7-day retention)
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "market_tick",
        sa.Column("tick_id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False,
                  comment="Exchange-side tick timestamp — hypertable partition key"),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("ltp", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=True,
                  comment="Day open price"),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=True,
                  comment="Day high"),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=True,
                  comment="Day low"),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=True,
                  comment="Previous session close"),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("bid", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ask", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("bid_quantity", sa.BigInteger(), nullable=True),
        sa.Column("ask_quantity", sa.BigInteger(), nullable=True),
        sa.Column("last_traded_quantity", sa.BigInteger(), nullable=True),
        sa.Column("total_buy_quantity", sa.BigInteger(), nullable=True),
        sa.Column("total_sell_quantity", sa.BigInteger(), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("sequence_number", sa.BigInteger(), nullable=True),
        sa.Column("source_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "quality_status", sa.String(16), nullable=False,
            server_default="TRUSTED",
        ),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("tick_id", "timestamp", name="market_tick_pkey"),
        sa.CheckConstraint(_QUALITY_STATUS_CHECK, name="mt_quality_status_valid"),
    )
    op.create_index(
        "mt_instrument_timestamp",
        "market_tick",
        ["instrument_id", sa.text("timestamp DESC")],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 3B — market_quote  (TimescaleDB hypertable, 1-day chunks, 30-day retention)
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "market_quote",
        sa.Column("quote_id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("ltp", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ltq", sa.BigInteger(), nullable=True),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("bid", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ask", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("bid_quantity", sa.BigInteger(), nullable=True),
        sa.Column("ask_quantity", sa.BigInteger(), nullable=True),
        sa.Column("total_buy_quantity", sa.BigInteger(), nullable=True),
        sa.Column("total_sell_quantity", sa.BigInteger(), nullable=True),
        sa.Column("upper_circuit", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("lower_circuit", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("week_high_52", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("week_low_52", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "quality_status", sa.String(16), nullable=False,
            server_default="TRUSTED",
        ),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("quote_id", "timestamp", name="market_quote_pkey"),
        sa.CheckConstraint(_QUALITY_STATUS_CHECK, name="mq_quality_status_valid"),
    )
    op.create_index(
        "mq_instrument_timestamp",
        "market_quote",
        ["instrument_id", sa.text("timestamp DESC")],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 4A — option_chain_snapshot
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "option_chain_snapshot",
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("underlying_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("spot_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("atm_strike", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("pcr_oi", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("pcr_volume", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("total_ce_oi", sa.BigInteger(), nullable=True),
        sa.Column("total_pe_oi", sa.BigInteger(), nullable=True),
        sa.Column("max_pain", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("atm_iv", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column(
            "quality_status", sa.String(16), nullable=False,
            server_default="TRUSTED",
        ),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("snapshot_id", name="option_chain_snapshot_pkey"),
        sa.CheckConstraint(_QUALITY_STATUS_CHECK, name="ocs_quality_status_valid"),
    )
    op.create_index(
        "ocs_underlying_expiry_ts",
        "option_chain_snapshot",
        ["underlying_id", "expiry", sa.text("timestamp DESC")],
        unique=False,
    )
    op.create_index(
        "ocs_exchange_ts",
        "option_chain_snapshot",
        ["exchange", sa.text("timestamp DESC")],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 4B — option_chain_contract
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "option_chain_contract",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("instrument_id", sa.String(64), nullable=True),
        sa.Column("strike", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("option_type", sa.String(2), nullable=False),
        sa.Column("ltp", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("oi_change", sa.BigInteger(), nullable=True),
        sa.Column("bid", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ask", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("bid_quantity", sa.BigInteger(), nullable=True),
        sa.Column("ask_quantity", sa.BigInteger(), nullable=True),
        # Greeks — NULL when not supplied by provider; zero is NOT a substitute
        sa.Column("iv", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("delta", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("gamma", sa.Numeric(precision=10, scale=8), nullable=True),
        sa.Column("theta", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("vega", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("rho", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("intrinsic_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("time_value", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("is_atm", sa.Boolean(), nullable=False, server_default="FALSE"),
        sa.PrimaryKeyConstraint("id", name="option_chain_contract_pkey"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["option_chain_snapshot.snapshot_id"],
            name="occ_snapshot_id_fk",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("option_type IN ('CE','PE')", name="occ_option_type_valid"),
        sa.CheckConstraint("strike > 0", name="occ_strike_positive"),
    )
    op.create_index(
        "occ_snapshot_strike_type",
        "option_chain_contract",
        ["snapshot_id", "strike", "option_type"],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 4C — option_greeks_snapshot  (TimescaleDB hypertable)
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "option_greeks_snapshot",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("underlying_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("option_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("iv", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("delta", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("gamma", sa.Numeric(precision=10, scale=8), nullable=True),
        sa.Column("theta", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("vega", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("rho", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column(
            "calculation_method", sa.String(32), nullable=True,
            comment="BS | BINOMIAL | PROVIDER",
        ),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("source_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "received_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "quality_status", sa.String(16), nullable=False,
            server_default="TRUSTED",
        ),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("id", "timestamp", name="option_greeks_snapshot_pkey"),
        sa.CheckConstraint(_QUALITY_STATUS_CHECK, name="ogs_quality_status_valid"),
    )
    op.create_index(
        "ogs_instrument_timestamp",
        "option_greeks_snapshot",
        ["instrument_id", sa.text("timestamp DESC")],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 5A — exchange_calendar
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "exchange_calendar",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("segment", sa.String(8), nullable=True),
        sa.Column("market", sa.String(32), nullable=True),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column(
            "day_type", sa.String(32), nullable=False,
            comment="TRADING_DAY | WEEKEND | OFFICIAL_HOLIDAY | SPECIAL_SESSION | NOT_PUBLISHED | UNKNOWN",
        ),
        sa.Column("holiday_name", sa.String(256), nullable=True),
        sa.Column("holiday_type", sa.String(32), nullable=True,
                  comment="NATIONAL | EXCHANGE_SPECIFIC | HALF_DAY"),
        sa.Column("is_trading_day", sa.Boolean(), nullable=False),
        sa.Column(
            "session_status", sa.String(32), nullable=False,
            server_default="UNKNOWN",
            comment="OPEN | CLOSED | HALF_DAY | MUHURAT | UNKNOWN",
        ),
        sa.Column("session_open", sa.Time(), nullable=True,
                  comment="IST session open time"),
        sa.Column("session_close", sa.Time(), nullable=True,
                  comment="IST session close time"),
        sa.Column("special_session", sa.Boolean(), nullable=False,
                  server_default="FALSE"),
        sa.Column("is_official", sa.Boolean(), nullable=False,
                  server_default="FALSE"),
        sa.Column("source", sa.String(64), nullable=True,
                  comment="NSE_CIRCULAR | NSE_API | MANUAL"),
        sa.Column("source_reference", sa.String(256), nullable=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="exchange_calendar_pkey"),
        sa.UniqueConstraint(
            "exchange", "segment", "calendar_date",
            name="ec_exchange_segment_date_uq",
        ),
    )
    op.create_index(
        "ecal_exchange_year_trading",
        "exchange_calendar",
        ["exchange", "year", "is_trading_day"],
        unique=False,
    )
    op.create_index(
        "ecal_trading_dates",
        "exchange_calendar",
        ["calendar_date"],
        unique=False,
        postgresql_where=sa.text("is_trading_day = TRUE"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 5B — market_session
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "market_session",
        sa.Column("session_id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("segment", sa.String(8), nullable=False, server_default="EQ"),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column(
            "session_type", sa.String(32), nullable=False,
            comment="REGULAR | PRE_OPEN | POST_MARKET | MUHURAT | SPECIAL",
        ),
        sa.Column("open_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("close_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_trading_day", sa.Boolean(), nullable=False),
        sa.Column("actual_open", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("actual_close", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("session_id", name="market_session_pkey"),
        sa.UniqueConstraint(
            "exchange", "segment", "session_date", "session_type",
            name="ms_exchange_segment_date_type_uq",
        ),
    )
    op.create_index(
        "ms_exchange_session_date",
        "market_session",
        ["exchange", sa.text("session_date DESC")],
        unique=False,
    )
    op.create_index(
        "ms_trading_days",
        "market_session",
        ["session_date"],
        unique=False,
        postgresql_where=sa.text("is_trading_day = TRUE"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 5C — fno_universe_membership
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "fno_universe_membership",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("underlying", sa.String(64), nullable=True),
        sa.Column("segment", sa.String(8), nullable=False, server_default="FO"),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="ACTIVE",
            comment="ACTIVE | REMOVED | SUSPENDED",
        ),
        sa.Column("removal_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="fno_universe_membership_pkey"),
        sa.UniqueConstraint(
            "instrument_id", "effective_from",
            name="fum_instrument_effective_from_uq",
        ),
    )
    op.create_index(
        "fum_active_instruments",
        "fno_universe_membership",
        ["instrument_id", "effective_to"],
        unique=False,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "fum_underlying",
        "fno_universe_membership",
        ["underlying", "effective_from"],
        unique=False,
        postgresql_where=sa.text("underlying IS NOT NULL"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 6A — ingestion_job
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "ingestion_job",
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "job_type", sa.String(32), nullable=False,
            comment="BACKFILL | LIVE | RECONCILE | GAP_RECOVERY",
        ),
        sa.Column(
            "dataset", sa.String(32), nullable=False,
            comment="EQUITY_CANDLE | FUTURES_CANDLE | OPTIONS_CANDLE | etc.",
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=True),
        sa.Column("exchange", sa.String(8), nullable=True),
        sa.Column("interval_str", sa.String(4), nullable=True),
        sa.Column("start_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("end_time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="PENDING",
            comment="PENDING | RUNNING | COMPLETED | FAILED | CANCELLED",
        ),
        sa.Column("requested_rows", sa.Integer(), nullable=True),
        sa.Column("received_rows", sa.Integer(), nullable=True),
        sa.Column("inserted_rows", sa.Integer(), nullable=True),
        sa.Column("updated_rows", sa.Integer(), nullable=True),
        sa.Column("skipped_rows", sa.Integer(), nullable=True),
        sa.Column("invalid_rows", sa.Integer(), nullable=True),
        sa.Column("duplicate_rows", sa.Integer(), nullable=True),
        sa.Column("failed_rows", sa.Integer(), nullable=True),
        sa.Column("quarantine_rows", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parent_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("job_id", name="ingestion_job_pkey"),
        sa.ForeignKeyConstraint(
            ["parent_job_id"],
            ["ingestion_job.job_id"],
            name="ij_parent_job_fk",
        ),
    )
    op.create_index(
        "ij_instrument_dataset_status",
        "ingestion_job",
        ["instrument_id", "interval_str", "dataset", "status"],
        unique=False,
        postgresql_where=sa.text("instrument_id IS NOT NULL"),
    )
    op.create_index(
        "ij_active_jobs",
        "ingestion_job",
        ["status", sa.text("created_at DESC")],
        unique=False,
        postgresql_where=sa.text("status <> 'COMPLETED'"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 6B — ingestion_checkpoint
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "ingestion_checkpoint",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("dataset", sa.String(32), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("interval_str", sa.String(4), nullable=False),
        sa.Column("last_successful_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_attempted_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="IDLE",
            comment="IDLE | RUNNING | PAUSED | FAILED",
        ),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="ingestion_checkpoint_pkey"),
        sa.UniqueConstraint(
            "provider", "dataset", "instrument_id", "exchange", "interval_str",
            name="ic_provider_dataset_instrument_uq",
        ),
    )
    op.create_index(
        "ic_instrument_exchange_interval",
        "ingestion_checkpoint",
        ["instrument_id", "exchange", "interval_str", "dataset"],
        unique=False,
    )
    op.create_index(
        "ic_non_idle",
        "ingestion_checkpoint",
        ["status"],
        unique=False,
        postgresql_where=sa.text("status <> 'IDLE'"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # LAYER 6C — candle_bar_quarantine
    # Receives rows from candle_bar that could not be classified during migration.
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "candle_bar_quarantine",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("original_id", sa.BigInteger(), nullable=False,
                  comment="candle_bar.id of the original row"),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=True),
        sa.Column("interval_str", sa.String(4), nullable=True),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("oi", sa.BigInteger(), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("source_type", sa.String(32), nullable=True),
        sa.Column("normalisation_version", sa.String(16), nullable=True),
        sa.Column("quarantine_reason", sa.Text(), nullable=False),
        sa.Column(
            "quarantine_status", sa.String(16), nullable=False,
            server_default="PENDING",
            comment="PENDING | RESOLVED | DISCARDED",
        ),
        sa.Column("resolved_to_table", sa.String(64), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="candle_bar_quarantine_pkey"),
    )
    op.create_index(
        "cbq_instrument_status",
        "candle_bar_quarantine",
        ["instrument_id", "quarantine_status"],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # TimescaleDB hypertable promotion
    #
    # Uses IF NOT EXISTS so this block is idempotent on re-run.
    # Chunk intervals are tuned to the expected write volume:
    #   - Candle tables: 7-day chunks (historical, low write rate post-backfill)
    #   - Tick/quote:    1-day chunks  (live, high write rate)
    #   - Greeks:        1-day chunks  (live, moderate write rate)
    # ────────────────────────────────────────────────────────────────────────
    conn = op.get_bind()
    hypertable_configs = [
        ("equity_candle",          "time",      "7 days"),
        ("futures_candle",         "time",      "7 days"),
        ("options_candle",         "time",      "7 days"),
        ("market_tick",            "timestamp", "1 day"),
        ("market_quote",           "timestamp", "1 day"),
        ("option_greeks_snapshot", "timestamp", "1 day"),
    ]
    for table_name, time_col, chunk_interval in hypertable_configs:
        conn.execute(
            sa.text(
                f"SELECT create_hypertable("  # noqa: S608
                f"'{table_name}', '{time_col}', "
                f"chunk_time_interval => INTERVAL '{chunk_interval}', "
                f"if_not_exists => TRUE"
                f")"
            )
        )

    # ────────────────────────────────────────────────────────────────────────
    # Add deprecation comment to candle_bar
    # candle_bar is NOT dropped — it is kept as a read-only archive.
    # ────────────────────────────────────────────────────────────────────────
    op.execute(
        sa.text(
            "COMMENT ON TABLE candle_bar IS "
            "'DEPRECATED 2026-09-15: All NSE data migrated to equity_candle. "
            "BINANCE rows retained as CRYPTO_PENDING. "
            "This table is a read-only archive. "
            "Scheduled for removal after 2026-10-15 once all consumers switch.';"
        )
    )


# ===========================================================================
# downgrade — drop all new objects in reverse creation order
# ===========================================================================

def downgrade() -> None:
    # Remove deprecation comment
    op.execute(sa.text("COMMENT ON TABLE candle_bar IS NULL;"))

    # Layer 6
    op.drop_index("cbq_instrument_status", table_name="candle_bar_quarantine")
    op.drop_table("candle_bar_quarantine")

    op.drop_index("ic_non_idle", table_name="ingestion_checkpoint")
    op.drop_index("ic_instrument_exchange_interval", table_name="ingestion_checkpoint")
    op.drop_table("ingestion_checkpoint")

    op.drop_index("ij_active_jobs", table_name="ingestion_job")
    op.drop_index("ij_instrument_dataset_status", table_name="ingestion_job")
    op.drop_table("ingestion_job")

    # Layer 5
    op.drop_index("fum_underlying", table_name="fno_universe_membership")
    op.drop_index("fum_active_instruments", table_name="fno_universe_membership")
    op.drop_table("fno_universe_membership")

    op.drop_index("ms_trading_days", table_name="market_session")
    op.drop_index("ms_exchange_session_date", table_name="market_session")
    op.drop_table("market_session")

    op.drop_index("ecal_trading_dates", table_name="exchange_calendar")
    op.drop_index("ecal_exchange_year_trading", table_name="exchange_calendar")
    op.drop_table("exchange_calendar")

    # Layer 4
    op.drop_index("ogs_instrument_timestamp", table_name="option_greeks_snapshot")
    op.drop_table("option_greeks_snapshot")

    op.drop_index("occ_snapshot_strike_type", table_name="option_chain_contract")
    op.drop_table("option_chain_contract")

    op.drop_index("ocs_exchange_ts", table_name="option_chain_snapshot")
    op.drop_index("ocs_underlying_expiry_ts", table_name="option_chain_snapshot")
    op.drop_table("option_chain_snapshot")

    # Layer 3
    op.drop_index("mq_instrument_timestamp", table_name="market_quote")
    op.drop_table("market_quote")

    op.drop_index("mt_instrument_timestamp", table_name="market_tick")
    op.drop_table("market_tick")

    # Layer 2
    op.drop_index("oc_underlying_expiry_strike_type", table_name="options_candle")
    op.drop_index("oc_instrument_interval_time", table_name="options_candle")
    op.drop_index("options_candle_uq", table_name="options_candle")
    op.drop_table("options_candle")

    op.drop_index("fc_underlying_expiry_interval_time", table_name="futures_candle")
    op.drop_index("fc_instrument_interval_time", table_name="futures_candle")
    op.drop_index("futures_candle_uq", table_name="futures_candle")
    op.drop_table("futures_candle")

    op.drop_index("ec_quality_filter", table_name="equity_candle")
    op.drop_index("ec_exchange_segment_interval_time", table_name="equity_candle")
    op.drop_index("ec_instrument_interval_time", table_name="equity_candle")
    op.drop_index("equity_candle_uq", table_name="equity_candle")
    op.drop_table("equity_candle")

    # Layer 1
    op.drop_index("iih_instrument_id", table_name="instrument_identity_history")
    op.drop_table("instrument_identity_history")

    op.drop_index("ipm_active_by_instrument",
                  table_name="instrument_provider_mapping")
    op.drop_index("ipm_provider_token", table_name="instrument_provider_mapping")
    op.drop_table("instrument_provider_mapping")

    op.drop_index("im_instrument_class", table_name="instrument_master")
    op.drop_column("instrument_master", "name")
    op.drop_column("instrument_master", "instrument_class")
