"""Initial schema — all seven tables.

Creates the complete DATA-SERVICE 2.0 database schema:

  1. candle_bar            — OHLCV time-series (TimescaleDB hypertable candidate)
  2. instrument_master     — canonical instrument registry
  3. fno_universe_snapshot — point-in-time F&O eligible universe
  4. data_gap              — gap detection and recovery records
  5. data_incident         — integrity / quality incident log
  6. data_provenance       — full provenance / lineage records
  7. provider_health       — provider capability health time-series

Key invariants enforced at the database layer:
- ``candle_bar.interval_str <> '3m'`` CHECK constraint (non-negotiable
  defense-in-depth; Requirement 4.2, 10.11).
- ``fno_universe_snapshot.checksum`` UNIQUE — idempotent snapshot writes.
- ``candle_bar`` PRIMARY KEY is ``(id, time)`` to satisfy TimescaleDB's
  hypertable requirement that the partition key be part of the PK.
- After applying this migration, promoting to a TimescaleDB hypertable
  requires exactly one DDL command with no data migration (Requirement 20.6):

      SELECT create_hypertable(
          'candle_bar', 'time',
          chunk_time_interval => INTERVAL '1 day',
          if_not_exists => TRUE
      );

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2024-01-01 00:00:00.000000

Requirements: 4.2, 10.11, 20.6
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# upgrade — create all tables, indexes, and constraints
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ────────────────────────────────────────────────────────────────────────
    # 1. candle_bar
    #
    # TimescaleDB hypertable candidate.  PRIMARY KEY includes ``time`` so that
    # ``create_hypertable('candle_bar', 'time')`` succeeds without altering
    # the schema.  The CHECK on interval_str is the last line of defense
    # against any 3m data entering the database (Requirement 4.2, 10.11).
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "candle_bar",
        # Surrogate key — BIGSERIAL; combined with ``time`` as composite PK
        # so TimescaleDB can partition on the time column.
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        # interval_str: '1m','5m','10m','15m','30m','1h','1d','1w','1M'
        # The CHECK constraint below blocks '3m' at the DB layer.
        sa.Column("interval_str", sa.String(4), nullable=False),
        # Candle open time in UTC.  Used as the hypertable partition key.
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False, server_default="0"),
        # oi is null for non-F&O instruments; never populated from tradedValue.
        sa.Column("oi", sa.BigInteger(), nullable=True),
        sa.Column(
            "volume_unavailable", sa.Boolean(), nullable=False, server_default="FALSE"
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "normalisation_version",
            sa.String(16),
            nullable=False,
            server_default="2.0.0",
        ),
        sa.Column("dataset_version", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("reconciliation_status", sa.String(32), nullable=True),
        sa.Column(
            "poor_quality", sa.Boolean(), nullable=False, server_default="FALSE"
        ),
        sa.Column("data_observation_id", postgresql.UUID(as_uuid=True), nullable=True),
        # ── Composite primary key: required for TimescaleDB hypertable ──────
        sa.PrimaryKeyConstraint("id", "time", name="candle_bar_pkey"),
        # ── Defense-in-depth: permanently ban 3m interval (Req 4.2, 10.11) ─
        sa.CheckConstraint("interval_str <> '3m'", name="no_3m_interval"),
    )

    # Unique index: one candle per (instrument, exchange, interval, time).
    # Used by the bulk-upsert ON CONFLICT clause.
    op.create_index(
        "candle_bar_uq",
        "candle_bar",
        ["instrument_id", "exchange", "interval_str", "time"],
        unique=True,
    )

    # Query index: range scans ordered by time descending per symbol/interval.
    op.create_index(
        "candle_bar_symbol_interval_time",
        "candle_bar",
        ["instrument_id", "interval_str", sa.text("time DESC")],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # 2. instrument_master
    #
    # Canonical instrument registry with point-in-time accuracy via
    # activeFrom/activeTo.  Provider token columns (angel_token, upstox_key,
    # etc.) are stored here but stripped from consumer-facing API responses
    # (Requirement 2.7, 19.4).
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "instrument_master",
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("trading_symbol", sa.String(64), nullable=False),
        sa.Column("display_symbol", sa.String(128), nullable=True),
        sa.Column("isin", sa.String(12), nullable=True),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("segment", sa.String(8), nullable=False),
        sa.Column("instrument_type", sa.String(16), nullable=False),
        sa.Column("underlying", sa.String(64), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("strike", sa.Numeric(precision=18, scale=2), nullable=True),
        # 'CE' | 'PE' | NULL
        sa.Column("option_type", sa.String(4), nullable=True),
        sa.Column("lot_size", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "tick_size",
            sa.Numeric(precision=10, scale=4),
            nullable=False,
            server_default="0.0500",
        ),
        sa.Column("active_from", sa.Date(), nullable=False),
        # NULL means currently active (no expiry recorded yet).
        sa.Column("active_to", sa.Date(), nullable=True),
        # ── Provider token mappings (not exposed in consumer API by default) ─
        sa.Column("angel_token", sa.String(32), nullable=True),
        sa.Column("angel_symbol", sa.String(64), nullable=True),
        sa.Column("upstox_key", sa.String(64), nullable=True),
        sa.Column("upstox_symbol", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("instrument_id", name="instrument_master_pkey"),
    )

    # Fast lookup by trading symbol (e.g. "NIFTY25JANFUT").
    op.create_index(
        "im_trading_symbol",
        "instrument_master",
        ["trading_symbol"],
        unique=False,
    )

    # Active instruments by exchange + type (partial: excludes expired).
    op.create_index(
        "im_active_instruments",
        "instrument_master",
        ["exchange", "instrument_type"],
        unique=False,
        postgresql_where=sa.text("active_to IS NULL"),
    )

    # Derivative lookup by underlying + expiry (partial: derivative rows only).
    op.create_index(
        "im_underlying_expiry",
        "instrument_master",
        ["underlying", "expiry"],
        unique=False,
        postgresql_where=sa.text("expiry IS NOT NULL"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # 3. fno_universe_snapshot
    #
    # Point-in-time record of the NSE F&O eligible universe.  The SHA-256
    # checksum UNIQUE constraint ensures idempotent writes — a snapshot whose
    # content hasn't changed produces no new row.
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "fno_universe_snapshot",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        # SHA-256 of sorted constituent instrument IDs.
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("fno_equity_count", sa.Integer(), nullable=False),
        sa.Column("fno_index_count", sa.Integer(), nullable=False),
        sa.Column("constituent_count", sa.Integer(), nullable=False),
        # 'ACTIVE' | 'SUPERSEDED'
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="ACTIVE"
        ),
        sa.PrimaryKeyConstraint("id", name="fno_universe_snapshot_pkey"),
        sa.UniqueConstraint("checksum", name="fno_universe_snapshot_checksum_uq"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # 4. data_gap
    #
    # Records detected gaps in candle sequences and tracks the automated
    # recovery state machine (PENDING → RECOVERING → RECOVERED | EXHAUSTED).
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "data_gap",
        sa.Column(
            "gap_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("interval_str", sa.String(4), nullable=False),
        sa.Column("gap_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("gap_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("duration_sec", sa.Integer(), nullable=False),
        # Recovery state: PENDING | RECOVERING | RECOVERED | EXHAUSTED
        sa.Column(
            "recovery_status",
            sa.String(16),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "recovery_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("expected_provider", sa.String(32), nullable=False),
        sa.Column("recovery_provider", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("gap_id", name="data_gap_pkey"),
    )

    # Partial index on pending gaps — the gap-recovery scheduler queries this.
    op.create_index(
        "data_gap_pending",
        "data_gap",
        ["instrument_id", "interval_str"],
        unique=False,
        postgresql_where=sa.text("recovery_status = 'PENDING'"),
    )

    # ────────────────────────────────────────────────────────────────────────
    # 5. data_incident
    #
    # Immutable incident log for: circuit breaker opens, OHLC invariant
    # violations, semantic integrity violations, gap detection, gap-recovery
    # exhaustion, and cross-provider MAJOR_DISCREPANCY findings.
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "data_incident",
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("incident_type", sa.String(64), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        # LOW | MEDIUM | HIGH | CRITICAL
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("incident_id", name="data_incident_pkey"),
    )

    # Range scan by instrument ordered by newest first.
    op.create_index(
        "di_instrument_ts",
        "data_incident",
        ["instrument_id", sa.text("timestamp DESC")],
        unique=False,
    )

    # Alert dashboards often filter by severity.
    op.create_index(
        "di_severity",
        "data_incident",
        ["severity", sa.text("timestamp DESC")],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # 6. data_provenance
    #
    # Full provenance / lineage record for every market data observation.
    # Persisted within 1,000ms of successful acquisition.  Read-only after
    # creation (Requirement 8.9).
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "data_provenance",
        sa.Column(
            "observation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("dataset_key", sa.String(256), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=True),
        sa.Column("interval_str", sa.String(4), nullable=True),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        # BROKER_AUTHENTICATED | OPEN_SOURCE_NSE_DERIVED | CREDENTIAL_FREE
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column(
            "authenticated", sa.Boolean(), nullable=False, server_default="FALSE"
        ),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("data_as_of", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("response_hash", sa.String(64), nullable=True),
        sa.Column("from_ts", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("to_ts", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dataset_version", sa.BigInteger(), nullable=True),
        sa.Column(
            "normalisation_version",
            sa.String(16),
            nullable=False,
            server_default="2.0.0",
        ),
        # TRUSTED | POOR_QUALITY | FALLBACK | etc.
        sa.Column(
            "data_trust_status",
            sa.String(32),
            nullable=False,
            server_default="TRUSTED",
        ),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column(
            "is_fallback", sa.Boolean(), nullable=False, server_default="FALSE"
        ),
        sa.Column("fallback_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("observation_id", name="data_provenance_pkey"),
    )

    # Lineage lookups ordered by most-recently received first.
    op.create_index(
        "dp_instrument_received",
        "data_provenance",
        ["instrument_id", sa.text("fetched_at DESC")],
        unique=False,
    )

    # ────────────────────────────────────────────────────────────────────────
    # 7. provider_health
    #
    # Time-series snapshot of per-provider, per-capability health metrics.
    # Used by ``GET /v1/providers/health`` and Prometheus gauges.
    # ────────────────────────────────────────────────────────────────────────
    op.create_table(
        "provider_health",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("capability", sa.String(32), nullable=False),
        # UP | DOWN | DEGRADED | UNKNOWN
        sa.Column("status", sa.String(16), nullable=False),
        # CLOSED | OPEN | HALF_OPEN
        sa.Column("circuit_state", sa.String(16), nullable=False),
        sa.Column(
            "availability",
            sa.Numeric(precision=5, scale=4),
            nullable=False,
            server_default="1.0000",
        ),
        sa.Column("latency_p50_ms", sa.Integer(), nullable=True),
        sa.Column("latency_p99_ms", sa.Integer(), nullable=True),
        sa.Column(
            "error_rate",
            sa.Numeric(precision=5, scale=4),
            nullable=False,
            server_default="0.0000",
        ),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "semantic_integrity",
            sa.Boolean(),
            nullable=False,
            server_default="TRUE",
        ),
        sa.Column(
            "measured_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="provider_health_pkey"),
    )

    # Latest health snapshot per provider × capability — most recent row first.
    op.create_index(
        "ph_provider_cap",
        "provider_health",
        ["provider", "capability", sa.text("measured_at DESC")],
        unique=False,
    )


# ---------------------------------------------------------------------------
# downgrade — drop all tables in reverse creation order
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Drop indexes first (Alembic usually handles this automatically when
    # dropping tables, but being explicit avoids any dialect edge-cases).

    # 7. provider_health
    op.drop_index("ph_provider_cap", table_name="provider_health")
    op.drop_table("provider_health")

    # 6. data_provenance
    op.drop_index("dp_instrument_received", table_name="data_provenance")
    op.drop_table("data_provenance")

    # 5. data_incident
    op.drop_index("di_severity", table_name="data_incident")
    op.drop_index("di_instrument_ts", table_name="data_incident")
    op.drop_table("data_incident")

    # 4. data_gap
    op.drop_index("data_gap_pending", table_name="data_gap")
    op.drop_table("data_gap")

    # 3. fno_universe_snapshot
    op.drop_table("fno_universe_snapshot")

    # 2. instrument_master
    op.drop_index("im_underlying_expiry", table_name="instrument_master")
    op.drop_index("im_active_instruments", table_name="instrument_master")
    op.drop_index("im_trading_symbol", table_name="instrument_master")
    op.drop_table("instrument_master")

    # 1. candle_bar
    op.drop_index("candle_bar_symbol_interval_time", table_name="candle_bar")
    op.drop_index("candle_bar_uq", table_name="candle_bar")
    op.drop_table("candle_bar")
