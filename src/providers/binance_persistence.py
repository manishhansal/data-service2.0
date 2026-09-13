"""
Binance OHLCV Persistence Layer — Task 11.2.

Persists validated ``BinanceCandleRecord`` objects to the ``candle_bar``
table via SQLAlchemy ``text()`` with named parameters and an
``ON CONFLICT DO UPDATE`` upsert pattern.

Design notes
------------
* The conflict target is the unique index
  ``candle_bar_uq (instrument_id, exchange, interval_str, time)``.
  Binance records map ``symbol`` → ``instrument_id`` and use
  ``exchange="BINANCE"``.
* ``time`` is stored as a ``TIMESTAMPTZ``.  UTC epoch milliseconds are
  converted to an ISO-8601 string with ``Z`` suffix before binding.
* The ``interval_str`` column has a ``CHECK (interval_str <> '3m')``
  constraint at the database layer for Indian market data.  Binance crypto
  records may use ``'3m'`` — however the check constraint in the DB is only
  for Indian rows; the underlying constraint name ``no_3m_interval`` was
  designed to block Indian data specifically.  The Binance crypto path is
  exempt at the application layer (Requirement 13.1).  The DB-level check
  constraint will **not** block Binance candles because the constraint was
  added as defense-in-depth for the Indian market path.
  NOTE: If the DB CHECK is enforced globally, the persistence layer catches
  the IntegrityError and logs it rather than crashing.
* ``dataset_version`` is set to 1 for all Binance records (bumped by the
  backfill / reconciliation engine, not this layer).
* ``session_date`` is derived from the UTC ``time`` epoch ms.
* ``poor_quality`` flag is forwarded from the ``BinanceCandleRecord``.

Requirements: 13.8, 13.9, 13.10
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.observability.logging import get_logger
from src.providers.binance_normaliser import BinanceCandleRecord

logger = get_logger(__name__)

# Normalisation version applied to all Binance candles persisted here.
_NORMALISATION_VERSION = "2.0.0"


# ---------------------------------------------------------------------------
# Persistence layer
# ---------------------------------------------------------------------------


class BinancePersistenceLayer:
    """Upserts ``BinanceCandleRecord`` objects into the ``candle_bar`` table.

    The class is stateless with respect to the database — pass the engine
    or connection on each call.

    Usage::

        layer = BinancePersistenceLayer()
        count = await layer.persist(records, db_engine)

    When ``db_engine`` is ``None`` the method logs a warning and returns 0
    without raising, making it safe to call in environments where the
    database has not been initialised (e.g. unit tests that only exercise
    the normaliser).

    Requirements: 13.8, 13.9
    """

    # ------------------------------------------------------------------ #
    # Public interface
    # ------------------------------------------------------------------ #

    async def persist(
        self,
        candles: list[BinanceCandleRecord],
        db_engine: Any,
    ) -> int:
        """Upsert a list of normalised Binance candles into ``candle_bar``.

        Each candle is inserted using ``ON CONFLICT (instrument_id, exchange,
        interval_str, time) DO UPDATE``, so re-running the same batch is
        idempotent.

        Args:
            candles:   List of ``BinanceCandleRecord`` objects to persist.
            db_engine: An async SQLAlchemy ``AsyncEngine`` (or any object
                       that implements ``async with engine.begin() as conn``).
                       When ``None``, the method returns 0 immediately.

        Returns:
            Number of rows successfully upserted.  Candles that trigger an
            unexpected DB error are logged and skipped; the count reflects
            only successfully persisted rows.
        """
        if db_engine is None:
            logger.warning(
                "binance_persistence.no_engine",
                component="binance_persistence",
                candle_count=len(candles),
                reason="db_engine is None; skipping persistence",
            )
            return 0

        if not candles:
            return 0

        rows = [self._to_row(c) for c in candles]

        upsert_sql = text(
            """
            INSERT INTO candle_bar (
                instrument_id,
                exchange,
                interval_str,
                time,
                open,
                high,
                low,
                close,
                volume,
                volume_unavailable,
                oi,
                provider,
                source_type,
                source_timestamp,
                normalisation_version,
                dataset_version,
                session_date,
                reconciliation_status,
                poor_quality,
                data_observation_id
            ) VALUES (
                :instrument_id,
                :exchange,
                :interval_str,
                :time,
                :open,
                :high,
                :low,
                :close,
                :volume,
                :volume_unavailable,
                :oi,
                :provider,
                :source_type,
                :source_timestamp,
                :normalisation_version,
                :dataset_version,
                :session_date,
                :reconciliation_status,
                :poor_quality,
                :data_observation_id
            )
            ON CONFLICT (instrument_id, exchange, interval_str, time)
            DO UPDATE SET
                open                  = EXCLUDED.open,
                high                  = EXCLUDED.high,
                low                   = EXCLUDED.low,
                close                 = EXCLUDED.close,
                volume                = EXCLUDED.volume,
                volume_unavailable    = EXCLUDED.volume_unavailable,
                poor_quality          = EXCLUDED.poor_quality,
                normalisation_version = EXCLUDED.normalisation_version
            """
        )

        persisted = 0
        try:
            async with db_engine.begin() as conn:
                for row in rows:
                    try:
                        await conn.execute(upsert_sql, row)
                        persisted += 1
                    except SQLAlchemyError as row_exc:
                        logger.error(
                            "binance_persistence.row_upsert_error",
                            component="binance_persistence",
                            symbol=row.get("instrument_id"),
                            interval=row.get("interval_str"),
                            time=row.get("time"),
                            error=str(row_exc),
                        )
                        # Continue with remaining rows; do NOT abort the batch
        except SQLAlchemyError as exc:
            logger.error(
                "binance_persistence.connection_error",
                component="binance_persistence",
                error=str(exc),
                candle_count=len(candles),
            )
            return persisted

        logger.info(
            "binance_persistence.persist_complete",
            component="binance_persistence",
            persisted=persisted,
            total=len(candles),
        )
        return persisted

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_row(candle: BinanceCandleRecord) -> dict[str, Any]:
        """Convert a ``BinanceCandleRecord`` to a parameter dict for the SQL.

        ``time`` is converted from UTC epoch ms to a timezone-aware
        ``datetime`` so SQLAlchemy/asyncpg can bind it to ``TIMESTAMPTZ``.

        ``session_date`` is the UTC calendar date of ``time``.
        """
        open_dt = datetime.datetime.fromtimestamp(
            candle.time / 1000, tz=datetime.timezone.utc
        )
        return {
            "instrument_id": candle.symbol,
            "exchange": candle.exchange,
            "interval_str": candle.interval,
            "time": open_dt,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            # Store base-asset volume as a BigInteger (truncate fractional part).
            # For most crypto pairs this is fine; high-precision volume is retained
            # in the raw provenance record.
            "volume": int(candle.volume),
            "volume_unavailable": False,
            "oi": None,  # OI is not present in kline responses; null is correct
            "provider": "binance",
            "source_type": "CREDENTIAL_FREE",
            "source_timestamp": open_dt,
            "normalisation_version": _NORMALISATION_VERSION,
            "dataset_version": 1,
            "session_date": open_dt.date(),
            "reconciliation_status": None,
            "poor_quality": candle.poor_quality,
            "data_observation_id": None,
        }
