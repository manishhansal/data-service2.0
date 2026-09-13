"""
Delta Exchange OHLCV Persistence Layer — DS2-RCA-001 fix.

Persists validated ``DeltaCandleRecord`` objects to the ``candle_bar``
table.  Mirrors ``BinancePersistenceLayer`` exactly, changing only
``provider = "delta"`` and ``exchange = "DELTA"``.

Requirements: DS2-RCA-001
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.observability.logging import get_logger
from src.providers.delta_normaliser import DeltaCandleRecord

logger = get_logger(__name__)

_NORMALISATION_VERSION = "2.0.0"


class DeltaPersistenceLayer:
    """Upserts ``DeltaCandleRecord`` objects into the ``candle_bar`` table.

    Usage::

        layer = DeltaPersistenceLayer()
        count = await layer.persist(records, db_engine)

    When ``db_engine`` is ``None`` the method logs a warning and returns 0
    without raising.

    The conflict target is the unique index
    ``candle_bar_uq (instrument_id, exchange, interval_str, time)``.
    Delta records use ``exchange="DELTA"`` so they never collide with
    Binance rows (exchange="BINANCE") for the same symbol.

    Requirements: DS2-RCA-001
    """

    async def persist(
        self,
        candles: list[DeltaCandleRecord],
        db_engine: Any,
    ) -> int:
        """Upsert a list of normalised Delta candles into ``candle_bar``.

        Args:
            candles:   Validated ``DeltaCandleRecord`` objects.
            db_engine: Async SQLAlchemy engine.  ``None`` → returns 0.

        Returns:
            Number of successfully upserted rows.
        """
        if db_engine is None:
            logger.warning(
                "delta_persistence.no_engine",
                component="delta_persistence",
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
                instrument_id, exchange, interval_str, time,
                open, high, low, close, volume, volume_unavailable, oi,
                provider, source_type, source_timestamp,
                normalisation_version, dataset_version,
                session_date, reconciliation_status, poor_quality,
                data_observation_id
            ) VALUES (
                :instrument_id, :exchange, :interval_str, :time,
                :open, :high, :low, :close, :volume, :volume_unavailable, :oi,
                :provider, :source_type, :source_timestamp,
                :normalisation_version, :dataset_version,
                :session_date, :reconciliation_status, :poor_quality,
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
                            "delta_persistence.row_upsert_error",
                            component="delta_persistence",
                            symbol=row.get("instrument_id"),
                            interval=row.get("interval_str"),
                            time=row.get("time"),
                            error=str(row_exc),
                        )
        except SQLAlchemyError as exc:
            logger.error(
                "delta_persistence.connection_error",
                component="delta_persistence",
                error=str(exc),
                candle_count=len(candles),
            )
            return persisted

        logger.info(
            "delta_persistence.persist_complete",
            component="delta_persistence",
            persisted=persisted,
            total=len(candles),
        )
        return persisted

    @staticmethod
    def _to_row(candle: DeltaCandleRecord) -> dict[str, Any]:
        """Convert a ``DeltaCandleRecord`` to an SQL parameter dict."""
        open_dt = datetime.datetime.fromtimestamp(
            candle.time / 1000, tz=datetime.timezone.utc
        )
        return {
            "instrument_id":        candle.symbol,
            "exchange":             candle.exchange,        # "DELTA"
            "interval_str":         candle.interval,
            "time":                 open_dt,
            "open":                 candle.open,
            "high":                 candle.high,
            "low":                  candle.low,
            "close":                candle.close,
            "volume":               int(candle.volume),
            "volume_unavailable":   False,
            "oi":                   None,
            "provider":             "delta",
            "source_type":          "CREDENTIAL_FREE",
            "source_timestamp":     open_dt,
            "normalisation_version": _NORMALISATION_VERSION,
            "dataset_version":      1,
            "session_date":         open_dt.date(),
            "reconciliation_status": None,
            "poor_quality":         candle.poor_quality,
            "data_observation_id":  None,
        }
