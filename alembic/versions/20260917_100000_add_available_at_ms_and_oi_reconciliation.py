"""Add available_at_ms to candle tables; add OI fields to reconciliation.

available_at_ms is the epoch-millisecond timestamp at which a candle became
observable (i.e. after its period closed).  It is used for strict
point-in-time filtering in ML datasets to prevent look-ahead leakage.

Contract enforced by application code:
    candle_time_ms <= available_at_ms <= ingestion_time_ms (NOW())

The column is nullable for historical legacy data (pre-2026-09-17).
All newly ingested candles (live stream + future backfills) must set it.

Also adds open_interest and oi_change to the reconciliation_record table
so that OI discrepancies across providers can be tracked.

Revision ID: 20260917_100000
Revises: 20260917_000000
Create Date: 2026-09-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "20260917_100000"
down_revision: str = "20260917_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── available_at_ms: point-in-time availability timestamp ──────────────
    for table in ("equity_candle", "futures_candle", "options_candle"):
        op.add_column(
            table,
            sa.Column(
                "available_at_ms",
                sa.BigInteger(),
                nullable=True,
                comment=(
                    "UTC epoch ms when this candle became observable. "
                    "NULL for legacy data. Non-null for live/new backfills. "
                    "Contract: candle_time_ms <= available_at_ms <= ingestion_time_ms"
                ),
            ),
        )
        # Index for point-in-time backtest queries:
        # WHERE available_at_ms <= :as_of_ts
        op.create_index(
            f"{table[:8]}_avail_at_ms_idx",
            table,
            ["available_at_ms"],
            postgresql_where=sa.text("available_at_ms IS NOT NULL"),
        )

    # ── OI reconciliation columns on reconciliation_record ─────────────────
    # reconciliation_record table may not have OI columns yet
    # Use try/except in case it was already added
    try:
        op.add_column(
            "reconciliation_record",
            sa.Column(
                "oi_provider_a",
                sa.BigInteger(),
                nullable=True,
                comment="Open interest from provider A",
            ),
        )
        op.add_column(
            "reconciliation_record",
            sa.Column(
                "oi_provider_b",
                sa.BigInteger(),
                nullable=True,
                comment="Open interest from provider B",
            ),
        )
        op.add_column(
            "reconciliation_record",
            sa.Column(
                "oi_deviation_pct",
                sa.Numeric(precision=10, scale=4),
                nullable=True,
                comment="Percentage deviation in OI between providers",
            ),
        )
        op.add_column(
            "reconciliation_record",
            sa.Column(
                "oi_status",
                sa.String(32),
                nullable=True,
                comment="CONFIRMED | MINOR_DISCREPANCY | MAJOR_DISCREPANCY | BLOCKED_BY_PROVIDER | NULL_UNAVAILABLE",
            ),
        )
    except Exception:  # noqa: BLE001
        pass  # columns may already exist from a previous run


def downgrade() -> None:
    # Remove available_at_ms
    for table in ("equity_candle", "futures_candle", "options_candle"):
        try:
            op.drop_index(f"{table[:8]}_avail_at_ms_idx", table_name=table)
        except Exception:  # noqa: BLE001
            pass
        try:
            op.drop_column(table, "available_at_ms")
        except Exception:  # noqa: BLE001
            pass

    # Remove OI reconciliation columns
    for col in ("oi_provider_a", "oi_provider_b", "oi_deviation_pct", "oi_status"):
        try:
            op.drop_column("reconciliation_record", col)
        except Exception:  # noqa: BLE001
            pass
