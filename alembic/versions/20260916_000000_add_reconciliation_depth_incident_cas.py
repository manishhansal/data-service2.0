"""Add reconciliation, market_depth, data_incident, closing_auction_snapshot tables.

Also adds missing columns to market_quote (depth JSON) and market_tick
(provider_instrument_id) to capture full provider quote data.

Revision ID: 20260916_000000
Revises: 20260915_000000_v2_production_schema
Create Date: 2026-09-16 00:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision = "20260916_000000"
down_revision = "20260915_000000_v2_production_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # market_depth — ephemeral depth level storage (TimescaleDB hypertable)
    # ------------------------------------------------------------------ #
    op.create_table(
        "market_depth",
        sa.Column("depth_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            comment="Exchange-side depth timestamp — hypertable partition key",
        ),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("side", sa.String(4), nullable=False, comment="BUY | SELL"),
        sa.Column("level", sa.BigInteger(), nullable=False, comment="1=best, 30=worst"),
        sa.Column("price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("quantity", sa.BigInteger(), nullable=True),
        sa.Column("orders", sa.BigInteger(), nullable=True),
        sa.Column(
            "depth_type",
            sa.String(8),
            nullable=False,
            server_default="D5",
            comment="D5=5-level, D30=30-level",
        ),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("depth_id", "timestamp"),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="md_side_valid"),
        sa.CheckConstraint("level BETWEEN 1 AND 30", name="md_level_valid"),
        sa.CheckConstraint("depth_type IN ('D5','D30')", name="md_depth_type_valid"),
        sa.CheckConstraint("price IS NULL OR price >= 0", name="md_price_non_negative"),
    )
    op.create_index(
        "md_instrument_provider_timestamp",
        "market_depth",
        ["instrument_id", "provider", sa.text("timestamp DESC")],
    )
    # TimescaleDB hypertable — 1-day chunks for market_depth
    op.execute(
        "SELECT create_hypertable('market_depth', 'timestamp', "
        "chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);"
    )
    # 1-day retention for raw depth ticks
    op.execute(
        "SELECT add_retention_policy('market_depth', "
        "INTERVAL '1 day', if_not_exists => TRUE);"
    )

    # ------------------------------------------------------------------ #
    # reconciliation_record — provider agreement/disagreement log
    # ------------------------------------------------------------------ #
    op.create_table(
        "reconciliation_record",
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column(
            "reconciled_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("observation_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        # Provider A
        sa.Column("provider_a", sa.String(32), nullable=True),
        sa.Column("provider_a_ltp", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("provider_a_oi", sa.BigInteger(), nullable=True),
        sa.Column("provider_a_volume", sa.BigInteger(), nullable=True),
        sa.Column("provider_a_bid", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("provider_a_ask", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("provider_a_iv", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("provider_a_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        # Provider B
        sa.Column("provider_b", sa.String(32), nullable=True),
        sa.Column("provider_b_ltp", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("provider_b_oi", sa.BigInteger(), nullable=True),
        sa.Column("provider_b_volume", sa.BigInteger(), nullable=True),
        sa.Column("provider_b_bid", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("provider_b_ask", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("provider_b_iv", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("provider_b_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        # Differences
        sa.Column("ltp_diff_abs", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ltp_diff_pct", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("oi_diff_abs", sa.BigInteger(), nullable=True),
        sa.Column("iv_diff_abs", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("timestamp_diff_ms", sa.BigInteger(), nullable=True),
        # Canonical result
        sa.Column(
            "classification",
            sa.String(32),
            nullable=False,
            server_default="MATCH",
        ),
        sa.Column("canonical_provider", sa.String(32), nullable=True),
        sa.Column("canonical_ltp", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("canonical_oi", sa.BigInteger(), nullable=True),
        sa.Column("resolution_rule", sa.String(128), nullable=True),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "classification IN ('MATCH','MINOR_DIFFERENCE','SIGNIFICANT_DIFFERENCE',"
            "'STALE','MISSING','CONFLICT')",
            name="rr_classification_valid",
        ),
    )
    op.create_index(
        "rr_instrument_ts",
        "reconciliation_record",
        ["instrument_id", sa.text("observation_timestamp DESC")],
    )
    op.create_index(
        "rr_classification",
        "reconciliation_record",
        ["classification", sa.text("reconciled_at DESC")],
        postgresql_where=sa.text("classification <> 'MATCH'"),
    )

    # ------------------------------------------------------------------ #
    # data_incident — durable incident log (never truncated)
    # ------------------------------------------------------------------ #
    op.create_table(
        "data_incident",
        sa.Column(
            "incident_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("incident_type", sa.String(32), nullable=False),
        sa.Column("instrument_id", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("exchange", sa.String(8), nullable=True),
        sa.Column("interval_str", sa.String(4), nullable=True),
        sa.Column(
            "detected_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("incident_start", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("incident_end", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "severity",
            sa.String(16),
            nullable=False,
            server_default="MEDIUM",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("affected_rows", sa.BigInteger(), nullable=True),
        sa.Column("gap_count", sa.BigInteger(), nullable=True),
        sa.Column(
            "recovery_attempts",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("session_date", sa.Date(), nullable=True),
        sa.CheckConstraint(
            "incident_type IN ('DATA_GAP','PROVIDER_DISAGREEMENT','WEBSOCKET_SILENCE',"
            "'TIMESTAMP_ANOMALY','QUALITY_DEGRADATION','PROVIDER_FAILURE',"
            "'SCHEMA_VIOLATION','OI_ANOMALY')",
            name="di_incident_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','RESOLVED','SUPPRESSED','INVESTIGATING')",
            name="di_status_valid",
        ),
        sa.CheckConstraint(
            "severity IN ('CRITICAL','HIGH','MEDIUM','LOW')",
            name="di_severity_valid",
        ),
    )
    op.create_index(
        "di_instrument_type_detected",
        "data_incident",
        ["instrument_id", "incident_type", sa.text("detected_at DESC")],
    )
    op.create_index(
        "di_open_incidents",
        "data_incident",
        ["status", sa.text("detected_at DESC")],
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_index(
        "di_provider_detected",
        "data_incident",
        ["provider", sa.text("detected_at DESC")],
    )

    # ------------------------------------------------------------------ #
    # closing_auction_snapshot — CAS indicative price (NOT LTP)
    # ------------------------------------------------------------------ #
    op.create_table(
        "closing_auction_snapshot",
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("instrument_id", sa.String(64), nullable=False),
        sa.Column("exchange", sa.String(8), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column(
            "indicative_equilibrium_price",
            sa.Numeric(precision=18, scale=6),
            nullable=True,
            comment="CAS indicative clearing price — NOT a traded LTP",
        ),
        sa.Column("indicative_equilibrium_quantity", sa.BigInteger(), nullable=True),
        sa.Column("total_indicative_quantity", sa.BigInteger(), nullable=True),
        sa.Column("market_indicative_imbalance", sa.BigInteger(), nullable=True),
        sa.Column("reference_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column(
            "pre_cas_ltp",
            sa.Numeric(precision=18, scale=6),
            nullable=True,
            comment="LTP at CAS phase entry — for reference only",
        ),
        sa.Column(
            "is_final",
            sa.Boolean(),
            nullable=False,
            server_default="FALSE",
        ),
        sa.Column(
            "quality_status",
            sa.String(16),
            nullable=False,
            server_default="TRUSTED",
        ),
        sa.UniqueConstraint(
            "instrument_id", "session_date", "timestamp", "provider",
            name="cas_instrument_date_ts_provider_uq",
        ),
        sa.CheckConstraint(
            "quality_status IN ('TRUSTED','DEGRADED','POOR_QUALITY','BLOCKED',"
            "'MISSING','QUARANTINED','DERIVED')",
            name="cas_quality_status_valid",
        ),
    )
    op.create_index(
        "cas_instrument_date",
        "closing_auction_snapshot",
        ["instrument_id", "session_date", sa.text("timestamp DESC")],
    )
    op.create_index(
        "cas_exchange_date",
        "closing_auction_snapshot",
        ["exchange", "session_date"],
    )

    # ------------------------------------------------------------------ #
    # Add missing columns to existing tables
    # ------------------------------------------------------------------ #

    # market_tick: add provider_instrument_id for traceability
    op.add_column(
        "market_tick",
        sa.Column(
            "provider_instrument_id",
            sa.String(128),
            nullable=True,
            comment="Provider-native token/key (e.g. Angel One token or Upstox instrument_key)",
        ),
    )

    # market_quote: add JSON depth column so full-quote depth is not dropped
    # and add circuit limits + net_change from full quote response
    op.add_column(
        "market_quote",
        sa.Column(
            "depth_json",
            postgresql.JSONB(),
            nullable=True,
            comment="Market depth as JSONB: {buy: [{price, qty, orders, level}...], sell: [...]}",
        ),
    )
    op.add_column(
        "market_quote",
        sa.Column("net_change", sa.Numeric(precision=18, scale=6), nullable=True),
    )
    op.add_column(
        "market_quote",
        sa.Column("percent_change", sa.Numeric(precision=10, scale=4), nullable=True),
    )
    op.add_column(
        "market_quote",
        sa.Column("avg_price", sa.Numeric(precision=18, scale=6), nullable=True),
    )
    op.add_column(
        "market_quote",
        sa.Column("last_traded_quantity", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "market_quote",
        sa.Column("provider_instrument_id", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    # Remove added columns
    op.drop_column("market_quote", "provider_instrument_id")
    op.drop_column("market_quote", "last_traded_quantity")
    op.drop_column("market_quote", "avg_price")
    op.drop_column("market_quote", "percent_change")
    op.drop_column("market_quote", "net_change")
    op.drop_column("market_quote", "depth_json")
    op.drop_column("market_tick", "provider_instrument_id")

    # Drop tables
    op.drop_table("closing_auction_snapshot")
    op.drop_table("data_incident")
    op.drop_table("reconciliation_record")
    op.drop_table("market_depth")
