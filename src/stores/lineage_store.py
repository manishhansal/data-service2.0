"""
src/stores/lineage_store.py
===========================

LineageStore — in-memory LRU cache + PostgreSQL durable backend for data
provenance / lineage records.

Design contract (Requirements 8.3, 8.6, 10.2):
- In-memory LRU holds the *most recent* ``max_cache_size`` observations.
  When capacity is exceeded the oldest (least-recently-inserted) entry is
  evicted.  Eviction is insertion-ordered, not access-ordered, so the LRU
  here is really a FIFO bounded store; the spec says "evicted in insertion
  order at limit" (Req 8.3).
- PostgreSQL persistence: every ``put()`` also writes an upsert to the
  ``data_provenance`` table.  The store degrades gracefully to cache-only
  mode when no ``db_engine`` is supplied or when the DB is unavailable.
- Persistence retry: up to 3 attempts at 500ms intervals; on exhaustion the
  record is logged and discarded from the persistent layer only — the cache
  entry is retained (Requirement 8.6).
- All DB queries use SQLAlchemy ``text()`` with named bind-parameters; no
  string interpolation (Requirement 19.5).

Usage::

    from src.stores.lineage_store import LineageStore

    # Cache-only (no DB):
    store = LineageStore()

    # With DB:
    store = LineageStore(db_engine=engine, max_cache_size=100_000)

    await store.put(observation_id, provenance)
    prov = await store.get(observation_id)
    records = await store.get_by_instrument("NIFTY-NFO", limit=50)
    deleted = await store.delete(observation_id)
    n = store.cache_size()
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core.schemas.provenance import DataProvenance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_RETRY_ATTEMPTS: int = 3
_RETRY_INTERVAL_SEC: float = 0.5

# DB columns that map 1-to-1 from DataProvenance fields.
# Ordered consistently with the INSERT statement below.
_DB_COLUMNS = (
    "observation_id",
    "dataset_key",
    "instrument_id",
    "exchange",
    "interval_str",
    "session_date",
    "provider",
    "source_type",
    "authenticated",
    "fetched_at",
    "source_timestamp",
    "data_as_of",
    "response_hash",
    "from_ts",
    "to_ts",
    "dataset_version",
    "normalisation_version",
    "data_trust_status",
    "row_count",
    "is_fallback",
    "fallback_reason",
)


# ---------------------------------------------------------------------------
# Helper: build a parameter dict from a DataProvenance record
# ---------------------------------------------------------------------------


def _provenance_to_db_params(prov: DataProvenance) -> dict:
    """Convert a :class:`DataProvenance` to a flat dict for DB bind-params."""
    import datetime

    # Convert epoch-ms timestamps to aware UTC datetimes (PG expects timestamptz).
    def _ms_to_dt(ms: Optional[int]) -> Optional[datetime.datetime]:
        if ms is None:
            return None
        return datetime.datetime.fromtimestamp(ms / 1_000, tz=datetime.timezone.utc)

    return {
        "observation_id": str(prov.dataObservationId),
        "dataset_key": prov.datasetKey or "",
        "instrument_id": prov.instrumentId or "",
        "exchange": prov.exchange,
        "interval_str": prov.intervalStr,
        "session_date": prov.sessionDate or None,
        "provider": prov.provider or (prov.source.value if prov.source else None),
        "source_type": prov.sourceType.value if prov.sourceType else None,
        "authenticated": prov.authenticated,
        "fetched_at": _ms_to_dt(prov.receivedAtMs),
        "source_timestamp": _ms_to_dt(prov.eventTimeMs),
        "data_as_of": prov.dataAsOf,
        "response_hash": prov.responseHash,
        "from_ts": prov.fromTs,
        "to_ts": prov.toTs,
        "dataset_version": prov.datasetVersion,
        "normalisation_version": prov.normalisationVersion,
        "data_trust_status": prov.dataTrustStatus.value,
        "row_count": prov.rowCount,
        "is_fallback": prov.isFallback,
        "fallback_reason": prov.fallbackReason,
    }


# ---------------------------------------------------------------------------
# Helper: rebuild DataProvenance from a DB row (mapping)
# ---------------------------------------------------------------------------


def _row_to_provenance(row: dict) -> DataProvenance:
    """Reconstruct a :class:`DataProvenance` from a raw DB row dict."""
    import datetime
    from uuid import UUID as _UUID

    from src.core.schemas.provenance import (
        DataSource,
        DataTrustStatus,
        ProviderSourceType,
    )

    def _dt_to_ms(dt: object) -> Optional[int]:
        if dt is None:
            return None
        if isinstance(dt, datetime.datetime):
            return int(dt.timestamp() * 1_000)
        return None

    raw_source_type = row.get("source_type")
    source_type = None
    if raw_source_type:
        try:
            source_type = ProviderSourceType(raw_source_type)
        except ValueError:
            pass

    raw_trust = row.get("data_trust_status", "TRUSTED")
    try:
        trust = DataTrustStatus(raw_trust)
    except ValueError:
        trust = DataTrustStatus.TRUSTED

    raw_source = row.get("provider") or "UNKNOWN"
    try:
        source = DataSource(raw_source.upper())
    except ValueError:
        source = DataSource.UNKNOWN

    raw_obs_id = row.get("observation_id")
    obs_id: UUID
    if isinstance(raw_obs_id, UUID):
        obs_id = raw_obs_id
    else:
        obs_id = _UUID(str(raw_obs_id))

    raw_session = row.get("session_date")
    session_date_str = None
    if raw_session is not None:
        if hasattr(raw_session, "isoformat"):
            session_date_str = raw_session.isoformat()
        else:
            session_date_str = str(raw_session)

    return DataProvenance(
        dataObservationId=obs_id,
        source=source,
        normalisationVersion=row.get("normalisation_version", "2.0.0") or "2.0.0",
        isFallback=bool(row.get("is_fallback", False)),
        fallbackReason=row.get("fallback_reason"),
        instrumentId=row.get("instrument_id") or None,
        exchange=row.get("exchange"),
        intervalStr=row.get("interval_str"),
        sessionDate=session_date_str,
        provider=row.get("provider"),
        sourceType=source_type,
        authenticated=bool(row.get("authenticated", False)),
        datasetKey=row.get("dataset_key"),
        dataAsOf=row.get("data_as_of"),
        responseHash=row.get("response_hash"),
        fromTs=row.get("from_ts"),
        toTs=row.get("to_ts"),
        datasetVersion=row.get("dataset_version"),
        dataTrustStatus=trust,
        rowCount=row.get("row_count"),
        eventTimeMs=_dt_to_ms(row.get("source_timestamp")),
        receivedAtMs=_dt_to_ms(row.get("fetched_at")),
    )


# ---------------------------------------------------------------------------
# LineageStore
# ---------------------------------------------------------------------------


class LineageStore:
    """In-memory LRU + PostgreSQL lineage / provenance store.

    The cache is an :class:`collections.OrderedDict` used as a FIFO bounded
    structure: entries are appended in insertion order and the *oldest* entry
    (``next(iter(cache))``) is evicted when capacity is exceeded.

    The PostgreSQL backend is optional.  When ``db_engine`` is ``None`` or
    when the DB is unreachable at persist-time, the store operates in
    *cache-only mode* without raising to the caller.

    Args:
        max_cache_size: Maximum number of provenance records to hold in the
            in-memory cache.  Default: 10,000.  The spec ceiling is 100,000
            (Requirement 8.3); callers should pass the correct value.
        db_engine: Optional live ``AsyncEngine``.  Pass ``None`` for
            cache-only operation (useful in tests and degraded mode).
    """

    def __init__(
        self,
        *,
        max_cache_size: int = 10_000,
        db_engine: Optional[AsyncEngine] = None,
    ) -> None:
        if max_cache_size < 1:
            raise ValueError(f"max_cache_size must be ≥ 1, got {max_cache_size}")
        self._max: int = max_cache_size
        self._cache: OrderedDict[str, DataProvenance] = OrderedDict()
        self._db_engine: Optional[AsyncEngine] = db_engine
        # Cumulative count of all records ever written (never decremented on
        # eviction or delete) — reported in lineage API responses.
        self._total_recorded: int = 0
        # Lock serialises mutations to the in-memory cache.
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def put(
        self,
        observation_id: str,
        provenance: DataProvenance,
    ) -> None:
        """Store a provenance record.

        Steps:
        1. Insert / refresh in the in-memory LRU cache (always).
        2. Evict the oldest entry if capacity is exceeded.
        3. Persist to PostgreSQL with up to 3 retries (when db_engine is set).

        Args:
            observation_id: The ``dataObservationId`` as a string (UUID v4).
            provenance:     The :class:`DataProvenance` record to store.
        """
        async with self._lock:
            # Refresh position: if the key already exists, remove it first so
            # the re-insert puts it at the MRU (tail) position.
            if observation_id in self._cache:
                del self._cache[observation_id]
            self._cache[observation_id] = provenance
            self._total_recorded += 1

            # Evict the oldest entry (head of the OrderedDict) when over capacity.
            if len(self._cache) > self._max:
                evicted_key, _ = self._cache.popitem(last=False)
                logger.debug(
                    "lineage_cache_evict",
                    extra={"evicted_observation_id": evicted_key},
                )

        # Persist to DB outside the lock to avoid holding it during I/O.
        if self._db_engine is not None:
            await self._persist_with_retry(observation_id, provenance)

    async def get(self, observation_id: str) -> Optional[DataProvenance]:
        """Look up a provenance record by observation ID.

        Checks the in-memory cache first; falls back to PostgreSQL if the
        record was evicted.

        Args:
            observation_id: String UUID v4 of the observation.

        Returns:
            The :class:`DataProvenance` record, or ``None`` if not found in
            either the cache or the database.
        """
        async with self._lock:
            cached = self._cache.get(observation_id)

        if cached is not None:
            return cached

        # Cache miss — try the database.
        return await self._db_get(observation_id)

    async def get_by_instrument(
        self,
        instrument_id: str,
        limit: int = 100,
    ) -> list[DataProvenance]:
        """Return the most-recent N provenance records for an instrument.

        Results are ordered by ``fetched_at DESC``.  The database is always
        consulted (gives the most complete and ordered view); results from the
        cache are **not** merged because they may duplicate DB rows and the
        ordering would be unreliable.

        When no DB is available, falls back to a linear scan of the in-memory
        cache filtered by ``instrumentId`` (unordered; best-effort).

        Args:
            instrument_id: Canonical instrument identifier to filter on.
            limit:         Maximum number of records to return (1–1000).

        Returns:
            List of :class:`DataProvenance` records, newest first.
        """
        if limit < 1:
            limit = 1
        elif limit > 1000:
            limit = 1000

        if self._db_engine is not None:
            db_results = await self._db_get_by_instrument(instrument_id, limit)
            if db_results is not None:
                return db_results

        # Fallback: linear scan of cache.
        logger.debug(
            "lineage_store_instrument_cache_fallback",
            extra={"instrument_id": instrument_id},
        )
        async with self._lock:
            # Iterate newest-first (reversed insertion order).
            result: list[DataProvenance] = []
            for prov in reversed(list(self._cache.values())):
                if prov.instrumentId == instrument_id:
                    result.append(prov)
                    if len(result) >= limit:
                        break
        return result

    async def delete(self, observation_id: str) -> bool:
        """Remove a provenance record from the cache and database.

        Args:
            observation_id: String UUID v4.

        Returns:
            ``True`` if the record was found and deleted from at least one
            location (cache or DB); ``False`` if it was not found anywhere.
        """
        found_in_cache = False
        async with self._lock:
            if observation_id in self._cache:
                del self._cache[observation_id]
                found_in_cache = True

        found_in_db = await self._db_delete(observation_id)
        return found_in_cache or found_in_db

    def cache_size(self) -> int:
        """Return the current number of records in the in-memory cache."""
        return len(self._cache)

    @property
    def total_recorded(self) -> int:
        """Cumulative count of records written since the store was created.

        Never decremented on eviction or delete — reflects the total
        observations ever ingested through this store instance.
        """
        return self._total_recorded

    @property
    def max_cache_size(self) -> int:
        """Maximum configured in-memory cache capacity."""
        return self._max

    # ------------------------------------------------------------------
    # Private — PostgreSQL persistence
    # ------------------------------------------------------------------

    async def _persist_with_retry(
        self,
        observation_id: str,
        provenance: DataProvenance,
    ) -> None:
        """Persist *provenance* to PostgreSQL with up to 3 retries.

        Requirement 8.6: persist within 1,000ms; retry 3× at 500ms intervals;
        on exhaustion log and discard.
        """
        params = _provenance_to_db_params(provenance)
        # Use the caller-supplied observation_id (string) directly.
        params["observation_id"] = observation_id

        insert_sql = text(
            """
            INSERT INTO data_provenance (
                observation_id,
                dataset_key,
                instrument_id,
                exchange,
                interval_str,
                session_date,
                provider,
                source_type,
                authenticated,
                fetched_at,
                source_timestamp,
                data_as_of,
                response_hash,
                from_ts,
                to_ts,
                dataset_version,
                normalisation_version,
                data_trust_status,
                row_count,
                is_fallback,
                fallback_reason
            ) VALUES (
                :observation_id,
                :dataset_key,
                :instrument_id,
                :exchange,
                :interval_str,
                :session_date,
                :provider,
                :source_type,
                :authenticated,
                :fetched_at,
                :source_timestamp,
                :data_as_of,
                :response_hash,
                :from_ts,
                :to_ts,
                :dataset_version,
                :normalisation_version,
                :data_trust_status,
                :row_count,
                :is_fallback,
                :fallback_reason
            )
            ON CONFLICT (observation_id) DO UPDATE SET
                dataset_key          = EXCLUDED.dataset_key,
                response_hash        = EXCLUDED.response_hash,
                data_trust_status    = EXCLUDED.data_trust_status,
                row_count            = EXCLUDED.row_count
            """
        )

        for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
            try:
                async with self._db_engine.begin() as conn:  # type: ignore[union-attr]
                    await conn.execute(insert_sql, params)
                logger.debug(
                    "lineage_persisted",
                    extra={
                        "observation_id": observation_id,
                        "attempt": attempt,
                    },
                )
                return  # success — done
            except SQLAlchemyError as exc:
                logger.warning(
                    "lineage_persist_failure",
                    extra={
                        "observation_id": observation_id,
                        "attempt": attempt,
                        "max_attempts": _MAX_RETRY_ATTEMPTS,
                        "error": str(exc),
                    },
                )
                if attempt < _MAX_RETRY_ATTEMPTS:
                    await asyncio.sleep(_RETRY_INTERVAL_SEC)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "lineage_persist_unexpected_error",
                    extra={
                        "observation_id": observation_id,
                        "attempt": attempt,
                        "error": str(exc),
                    },
                )
                if attempt < _MAX_RETRY_ATTEMPTS:
                    await asyncio.sleep(_RETRY_INTERVAL_SEC)

        logger.error(
            "lineage_persist_exhausted",
            extra={
                "observation_id": observation_id,
                "max_attempts": _MAX_RETRY_ATTEMPTS,
            },
        )

    async def _db_get(
        self,
        observation_id: str,
    ) -> Optional[DataProvenance]:
        """Fetch a single record from PostgreSQL by observation_id."""
        if self._db_engine is None:
            return None

        select_sql = text(
            """
            SELECT *
              FROM data_provenance
             WHERE observation_id = :observation_id
             LIMIT 1
            """
        )
        try:
            async with self._db_engine.connect() as conn:
                result = await conn.execute(
                    select_sql, {"observation_id": observation_id}
                )
                row = result.mappings().first()
            if row is None:
                return None
            return _row_to_provenance(dict(row))
        except SQLAlchemyError as exc:
            logger.warning(
                "lineage_db_get_failure",
                extra={"observation_id": observation_id, "error": str(exc)},
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lineage_db_get_unexpected",
                extra={"observation_id": observation_id, "error": str(exc)},
            )
            return None

    async def _db_get_by_instrument(
        self,
        instrument_id: str,
        limit: int,
    ) -> Optional[list[DataProvenance]]:
        """Fetch the most-recent ``limit`` records for *instrument_id* from DB.

        Returns ``None`` on DB error (so callers can fall back to cache scan).
        """
        select_sql = text(
            """
            SELECT *
              FROM data_provenance
             WHERE instrument_id = :instrument_id
             ORDER BY fetched_at DESC NULLS LAST
             LIMIT :limit
            """
        )
        try:
            async with self._db_engine.connect() as conn:  # type: ignore[union-attr]
                result = await conn.execute(
                    select_sql,
                    {"instrument_id": instrument_id, "limit": limit},
                )
                rows = result.mappings().all()
            return [_row_to_provenance(dict(r)) for r in rows]
        except SQLAlchemyError as exc:
            logger.warning(
                "lineage_db_get_by_instrument_failure",
                extra={"instrument_id": instrument_id, "error": str(exc)},
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lineage_db_get_by_instrument_unexpected",
                extra={"instrument_id": instrument_id, "error": str(exc)},
            )
            return None

    async def _db_delete(self, observation_id: str) -> bool:
        """Delete a single record from PostgreSQL.

        Returns ``True`` if a row was deleted, ``False`` otherwise.
        """
        if self._db_engine is None:
            return False

        delete_sql = text(
            """
            DELETE FROM data_provenance
             WHERE observation_id = :observation_id
            """
        )
        try:
            async with self._db_engine.begin() as conn:
                result = await conn.execute(
                    delete_sql, {"observation_id": observation_id}
                )
            return result.rowcount > 0
        except SQLAlchemyError as exc:
            logger.warning(
                "lineage_db_delete_failure",
                extra={"observation_id": observation_id, "error": str(exc)},
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lineage_db_delete_unexpected",
                extra={"observation_id": observation_id, "error": str(exc)},
            )
            return False
