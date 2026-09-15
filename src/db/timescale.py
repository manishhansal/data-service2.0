"""
src/db/timescale.py

TimescaleDB hypertable status check for DATA-SERVICE 2.0.

As of schema revision b1c2d3e4f5a6 (2026-09-15), the canonical time-series
tables are already created as TimescaleDB hypertables by the Alembic migration
itself via ``create_hypertable()``.  This module now only verifies that
TimescaleDB is present and logs the hypertable status — it does NOT attempt
to promote ``candle_bar`` (which is a deprecated archive table, not a
production write target).

Hypertables managed by Alembic migration (b1c2d3e4f5a6):
  - equity_candle          (7-day chunks)
  - futures_candle         (7-day chunks)
  - options_candle         (7-day chunks)
  - market_tick            (1-day chunks)
  - market_quote           (1-day chunks)
  - option_greeks_snapshot (1-day chunks)

``candle_bar`` is retained as a read-only archive.  It is NOT promoted to a
hypertable.  No new data is written to it by production code.

``DatabaseUnavailableError`` is re-raised to the caller so the server
lifespan can decide whether to enter degraded mode.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from src.db.engine import DatabaseUnavailableError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

# Query to detect whether TimescaleDB is installed in the current database.
_TIMESCALEDB_DETECT_SQL = text(
    "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
)

# Query to list all active hypertables for status logging.
_LIST_HYPERTABLES_SQL = text(
    """
    SELECT hypertable_name, num_chunks
    FROM timescaledb_information.hypertables
    ORDER BY hypertable_name
    """
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def promote_hypertable(engine: "AsyncEngine") -> bool:
    """Verify that TimescaleDB is present and log the canonical hypertable status.

    This function is called once during application startup.  As of schema
    revision b1c2d3e4f5a6, all production hypertables are created by the
    Alembic migration — no runtime DDL is executed here.

    The function specifically does NOT attempt to promote ``candle_bar``
    because:
    1. ``candle_bar`` is a deprecated archive table (no production writes).
    2. The canonical time-series tables (equity_candle, futures_candle, etc.)
       are already hypertables created by the migration.
    3. Attempting ``create_hypertable('candle_bar', ...)`` at startup on a
       table that may already have 5M+ rows would be a destructive no-op
       at best and a performance hazard at worst.

    Behaviour:
    - **TimescaleDB present**: logs the detected version and lists active
      hypertables for observability.  Returns ``True``.
    - **TimescaleDB absent**: logs an INFO message and returns ``False``
      without raising; the platform continues in plain PostgreSQL mode.
    - **PostgreSQL unreachable**: re-raises ``DatabaseUnavailableError``.

    Args:
        engine: A live ``AsyncEngine`` instance.

    Returns:
        ``True``  — TimescaleDB is installed and hypertables are active.
        ``False`` — TimescaleDB extension is not installed; plain PG mode.

    Raises:
        DatabaseUnavailableError: PostgreSQL could not be reached.
    """
    try:
        async with engine.connect() as conn:
            # ── Step 1: detect TimescaleDB ──────────────────────────────
            result = await conn.execute(_TIMESCALEDB_DETECT_SQL)
            row = result.fetchone()

            if row is None:
                # Extension is not installed — graceful plain-PG fallback.
                logger.info(
                    "timescaledb_not_available",
                    extra={
                        "event": "timescaledb_not_available",
                        "message": (
                            "TimescaleDB extension not found in pg_extension; "
                            "canonical tables will operate as plain PostgreSQL tables. "
                            "Install TimescaleDB and re-run migrations to enable "
                            "hypertable partitioning."
                        ),
                    },
                )
                return False

            tsdb_version: str = row[0]
            logger.info(
                "timescaledb_detected",
                extra={
                    "event": "timescaledb_detected",
                    "timescaledb_version": tsdb_version,
                    "note": (
                        "Canonical hypertables (equity_candle, futures_candle, "
                        "options_candle, market_tick, market_quote, "
                        "option_greeks_snapshot) are managed by Alembic migration "
                        "b1c2d3e4f5a6. candle_bar is a read-only archive."
                    ),
                },
            )

            # ── Step 2: log hypertable inventory for observability ──────
            try:
                ht_result = await conn.execute(_LIST_HYPERTABLES_SQL)
                hypertables = [
                    {"table": r[0], "chunks": r[1]}
                    for r in ht_result.fetchall()
                ]
                logger.info(
                    "hypertable_inventory",
                    extra={
                        "event": "hypertable_inventory",
                        "hypertables": hypertables,
                        "count": len(hypertables),
                    },
                )
            except Exception as list_exc:  # noqa: BLE001
                # Non-fatal: list query failing doesn't block startup.
                logger.warning(
                    "hypertable_inventory_failed",
                    extra={
                        "event": "hypertable_inventory_failed",
                        "error": str(list_exc),
                    },
                )

            return True

    except OperationalError as exc:
        logger.warning(
            "timescale_check_failed",
            extra={
                "event": "timescale_check_failed",
                "error": str(exc),
            },
        )
        raise DatabaseUnavailableError(
            f"PostgreSQL is unreachable during TimescaleDB status check: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "timescale_check_error",
            extra={
                "event": "timescale_check_error",
                "error": str(exc),
            },
        )
        raise DatabaseUnavailableError(
            f"Unexpected error during TimescaleDB status check: {exc}"
        ) from exc
