"""
src/db/timescale.py

TimescaleDB hypertable promotion for DATA-SERVICE 2.0.

On startup the application calls ``promote_hypertable`` which:

1. Checks whether the TimescaleDB extension is present in the current
   PostgreSQL database by querying ``pg_extension``.
2. If TimescaleDB is available, runs the idempotent ``create_hypertable``
   DDL on ``candle_bar`` (``if_not_exists => TRUE``).
3. If TimescaleDB is NOT available, logs an INFO message and returns
   gracefully — the platform operates in plain PostgreSQL mode without
   time-series optimisation.

This satisfies Requirement 20.6:
  "THE Platform SHALL support TimescaleDB hypertable partitioning on the
  `candle_bar` table with chunk_time_interval = '1 day'; the table SHALL be
  designed for compatibility so that promotion from plain PostgreSQL to
  TimescaleDB requires only a single DDL command without data migration."

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

# Idempotent hypertable promotion — safe to run on already-promoted tables.
_PROMOTE_HYPERTABLE_SQL = text(
    """
    SELECT create_hypertable(
        'candle_bar',
        'time',
        chunk_time_interval => INTERVAL '1 day',
        if_not_exists       => TRUE
    )
    """
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def promote_hypertable(engine: "AsyncEngine") -> bool:
    """Attempt to promote ``candle_bar`` to a TimescaleDB hypertable.

    This function is designed to be called once during application startup,
    after the Alembic migrations have been applied and the ``candle_bar``
    table exists.

    Behaviour:
    - **TimescaleDB present**: executes ``create_hypertable`` with
      ``if_not_exists => TRUE`` (idempotent) and logs a confirmation.
    - **TimescaleDB absent**: logs an INFO message and returns ``False``
      without raising; the platform continues in plain PostgreSQL mode.
    - **PostgreSQL unreachable**: re-raises ``DatabaseUnavailableError``
      so the caller can apply degraded-mode handling (Requirement 20.2).

    Args:
        engine: A live ``AsyncEngine`` instance.  Must not be ``None``; the
                caller is responsible for graceful handling when the engine
                is unavailable (consistent with engine.py contract).

    Returns:
        ``True``  — hypertable promotion was executed (or was already done).
        ``False`` — TimescaleDB extension is not installed; plain PG mode.

    Raises:
        DatabaseUnavailableError: PostgreSQL could not be reached or
            returned an unexpected error during the detection query or the
            promotion statement.
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
                            "candle_bar will operate as a plain PostgreSQL table. "
                            "Install TimescaleDB and restart to enable hypertable "
                            "partitioning."
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
                },
            )

            # ── Step 2: promote candle_bar ──────────────────────────────
            await conn.execute(_PROMOTE_HYPERTABLE_SQL)
            await conn.commit()

            logger.info(
                "hypertable_promoted",
                extra={
                    "event": "hypertable_promoted",
                    "table": "candle_bar",
                    "partition_column": "time",
                    "chunk_time_interval": "1 day",
                    "timescaledb_version": tsdb_version,
                },
            )
            return True

    except OperationalError as exc:
        logger.warning(
            "timescale_promote_failed",
            extra={
                "event": "timescale_promote_failed",
                "error": str(exc),
            },
        )
        raise DatabaseUnavailableError(
            f"PostgreSQL is unreachable during TimescaleDB promotion: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        # Any unexpected driver-level or TimescaleDB-specific error.
        logger.warning(
            "timescale_promote_error",
            extra={
                "event": "timescale_promote_error",
                "error": str(exc),
            },
        )
        raise DatabaseUnavailableError(
            f"Unexpected error during TimescaleDB hypertable promotion: {exc}"
        ) from exc
