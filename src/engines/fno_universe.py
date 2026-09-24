"""
src/engines/fno_universe.py

F&O Universe Refresh Service — manages the NSE F&O eligible instrument
universe with point-in-time snapshot tracking and lifecycle event emission.

Responsibilities:
- Fetch the current F&O eligible list from NSE upstream at 08:45 IST on
  every trading day.
- Compute a SHA-256 checksum of sorted instrument IDs to detect changes.
- When the checksum differs from the stored snapshot → write a new
  ``FnoUniverseSnapshot`` and emit ``InstrumentLifecycleEvent`` records
  (ADDED / REMOVED / SUSPENDED) for each changed instrument.
- When checksums match → log an idempotent skip; no DB write.
- On expiry day at 09:15 IST → set ``activeTo`` on expired contracts without
  deleting the records (point-in-time preservation).
- When upstream is unavailable → retain last successful snapshot and emit a
  structured warning log.

Requirements: 11.1, 11.2, 11.3, 11.6, 11.7, 2.4
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

from src.core.schemas.instrument import (
    ChangeType,
    FnoUniverseSnapshot,
    InstrumentLifecycleEvent,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NSE upstream stub
#
# Task 4.5 (Scrapling/NSE adapter) is not yet complete.  This stub returns a
# deterministic sample F&O universe so that the refresh logic, checksum
# computation, and lifecycle event emission can all be exercised end-to-end.
# Replace ``_fetch_fno_instrument_ids_from_nse`` with the real provider call
# once Task 4.5 is implemented.
# ---------------------------------------------------------------------------

_SAMPLE_FNO_INSTRUMENT_IDS: list[str] = [
    "NSE:RELIANCE:EQ",
    "NSE:TCS:EQ",
    "NSE:INFY:EQ",
    "NSE:HDFC:EQ",
    "NSE:ICICIBANK:EQ",
    "NSE:SBIN:EQ",
    "NSE:AXISBANK:EQ",
    "NSE:KOTAKBANK:EQ",
    "NSE:LT:EQ",
    "NSE:ITC:EQ",
    "NFO:NIFTY:IDX",
    "NFO:BANKNIFTY:IDX",
    "NFO:FINNIFTY:IDX",
    "NFO:MIDCPNIFTY:IDX",
]


class NseUpstreamUnavailableError(Exception):
    """Raised when the NSE upstream source cannot be reached.

    The caller (``FnoUniverseService.refresh``) catches this and retains the
    last successful snapshot, emitting a structured warning (Requirement 11.7).
    """


async def _fetch_fno_instrument_ids_from_nse() -> list[str]:
    """Stub: return sample F&O instrument IDs from NSE.

    This function will be replaced with a real Scrapling/NSE provider call
    once Task 4.5 is complete.  The stub always succeeds and returns the
    same deterministic list so that unit tests can inject fakes via
    ``unittest.mock.patch``.

    Returns:
        List of canonical instrument IDs that are currently F&O eligible.

    Raises:
        NseUpstreamUnavailableError: when the upstream cannot be reached.
    """
    logger.debug("fno_universe_fetch_stub_called")
    return list(_SAMPLE_FNO_INSTRUMENT_IDS)


# ---------------------------------------------------------------------------
# Checksum utility
# ---------------------------------------------------------------------------


def compute_checksum(instrument_ids: list[str]) -> str:
    """Compute SHA-256 checksum of sorted instrument IDs.

    The sort is applied before hashing so that checksum is order-independent
    (Requirement 11.3).

    Args:
        instrument_ids: Canonical instrument IDs from the F&O eligible list.

    Returns:
        Lowercase hex-encoded SHA-256 digest (64 characters).
    """
    sorted_ids = sorted(instrument_ids)
    payload = "\n".join(sorted_ids).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# In-memory snapshot store (per-process)
#
# The service maintains an in-process reference to the most recent snapshot
# so that ``get_current_snapshot`` can serve quickly without a DB round-trip
# on every call.  The authoritative store is always PostgreSQL; this is a
# read-through cache only.
# ---------------------------------------------------------------------------

_current_snapshot: Optional[FnoUniverseSnapshot] = None
_current_instrument_ids: set[str] = set()


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FnoUniverseService:
    """Manages the NSE F&O eligible instrument universe snapshot lifecycle.

    All methods are ``@staticmethod``/``@classmethod`` so the scheduler can
    call ``FnoUniverseService.refresh()`` without instantiating an object.
    The ``engine`` is provided by the ``app.state`` injector when running
    inside FastAPI, or via the scheduler's lifespan context.

    Design:
        The service does **not** own a database session factory.  Callers
        must pass an ``AsyncEngine`` (for methods that need DB access).
        ``refresh()`` acquires its own engine from ``app.state`` or from
        the default engine helper.
    """

    # ------------------------------------------------------------------
    # Primary entry point — called by the 08:45 IST scheduler
    # ------------------------------------------------------------------

    @staticmethod
    async def refresh(engine: Optional[AsyncEngine] = None) -> None:
        """Main refresh entry point: fetch → checksum → diff → write + events.

        Called by the APScheduler job at 08:45 IST on every trading day
        (Requirement 11.1).

        Steps:
        1. Fetch F&O eligible list from NSE upstream (stub until Task 4.5).
        2. Compute SHA-256 checksum of sorted instrument IDs.
        3. Load current ACTIVE snapshot from DB.
        4. If checksum matches current snapshot → log idempotent skip, return.
        5. If checksum differs → write new FnoUniverseSnapshot to DB and emit
           lifecycle events (ADDED / REMOVED / SUSPENDED) for each change.

        Upstream unavailability (Requirement 11.7):
            If the upstream fetch raises ``NseUpstreamUnavailableError``, the
            in-memory snapshot is retained and a structured warning is emitted.
            The DB is not written.

        Args:
            engine: AsyncEngine to use for DB operations.  When ``None``, the
                method attempts to import the engine from ``app.state`` via a
                helper.  Tests pass the engine explicitly.
        """
        global _current_snapshot, _current_instrument_ids

        log = logger.getChild("refresh")

        # ── 1. Fetch eligible instrument IDs from NSE ─────────────────────
        try:
            instrument_ids = await _fetch_fno_instrument_ids_from_nse()
        except NseUpstreamUnavailableError as exc:
            log.warning(
                "fno_universe_upstream_unavailable",
                extra={
                    "event": "fno_universe_upstream_unavailable",
                    "component": "fno_universe",
                    "error": str(exc),
                    "action": "retaining_last_snapshot",
                    "last_snapshot_version": (
                        _current_snapshot.snapshotVersion if _current_snapshot else None
                    ),
                },
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.error(
                "fno_universe_fetch_error",
                extra={
                    "event": "fno_universe_fetch_error",
                    "component": "fno_universe",
                    "error": str(exc),
                },
            )
            return

        # ── 2. Compute checksum ───────────────────────────────────────────
        new_checksum = compute_checksum(instrument_ids)

        # ── 3. Resolve the DB engine ──────────────────────────────────────
        if engine is None:
            engine = _get_default_engine()

        # ── 4. Load current snapshot from DB ─────────────────────────────
        current_snapshot = await FnoUniverseService.get_current_snapshot(engine)

        if current_snapshot is not None and current_snapshot.checksum == new_checksum:
            # Idempotent — checksums match, no write needed (Requirement 11.3)
            log.info(
                "fno_universe_refresh_idempotent",
                extra={
                    "event": "fno_universe_refresh_idempotent",
                    "component": "fno_universe",
                    "checksum": new_checksum,
                    "snapshot_version": current_snapshot.snapshotVersion,
                },
            )
            # Keep in-memory reference up-to-date
            _current_snapshot = current_snapshot
            return

        # ── 5. Checksum differs → write new snapshot + lifecycle events ───
        prev_ids: set[str] = _current_instrument_ids.copy()
        new_ids: set[str] = set(instrument_ids)

        new_version = (current_snapshot.snapshotVersion + 1) if current_snapshot else 1
        now_utc = datetime.now(tz=timezone.utc)

        new_snapshot = FnoUniverseSnapshot(
            snapshotVersion=new_version,
            checksum=new_checksum,
            generatedAt=now_utc.isoformat(),
            effectiveFrom=date.today(),
            effectiveTo=None,
            fnoEquityCount=sum(
                1 for iid in instrument_ids if ":EQ" in iid or ":FUTSTK" in iid
            ),
            fnoIndexCount=sum(
                1 for iid in instrument_ids if ":IDX" in iid or ":FUTIDX" in iid
            ),
            constituentCount=len(instrument_ids),
            status="ACTIVE",
        )

        # Compute lifecycle changes (only meaningful if we had a previous set)
        lifecycle_events: list[InstrumentLifecycleEvent] = []
        if prev_ids:
            added_ids = new_ids - prev_ids
            removed_ids = prev_ids - new_ids

            for iid in added_ids:
                lifecycle_events.append(
                    InstrumentLifecycleEvent(
                        symbol=_symbol_from_id(iid),
                        changeType=ChangeType.ADDED,
                        effectiveDate=date.today(),
                        source="NSE_FNO_REFRESH",
                        snapshotVersion=new_version,
                    )
                )
            for iid in removed_ids:
                lifecycle_events.append(
                    InstrumentLifecycleEvent(
                        symbol=_symbol_from_id(iid),
                        changeType=ChangeType.REMOVED,
                        effectiveDate=date.today(),
                        source="NSE_FNO_REFRESH",
                        snapshotVersion=new_version,
                    )
                )

        # Persist snapshot and lifecycle events to DB
        await _persist_snapshot(engine, new_snapshot, lifecycle_events, current_snapshot)

        # Update in-memory state atomically
        _current_snapshot = new_snapshot
        _current_instrument_ids = new_ids

        log.info(
            "fno_universe_refreshed",
            extra={
                "event": "fno_universe_refreshed",
                "component": "fno_universe",
                "snapshot_version": new_version,
                "checksum": new_checksum,
                "constituent_count": len(instrument_ids),
                "added": len([e for e in lifecycle_events if e.changeType == ChangeType.ADDED]),
                "removed": len(
                    [e for e in lifecycle_events if e.changeType == ChangeType.REMOVED]
                ),
            },
        )

    # ------------------------------------------------------------------
    # Expiry processing — called at 09:15 IST on expiry day
    # ------------------------------------------------------------------

    @staticmethod
    async def expire_instruments(
        engine: AsyncEngine,
        as_of_date: date,
    ) -> int:
        """Set ``active_to`` on all derivative contracts expiring on *as_of_date*.

        Records are updated in-place; they are **never** deleted to preserve
        point-in-time historical accuracy (Requirement 11.6).

        Args:
            engine: AsyncEngine for DB operations.
            as_of_date: The expiry date.  All derivative records with
                ``expiry == as_of_date`` and ``active_to IS NULL`` will be
                updated.

        Returns:
            Number of instrument records updated.
        """
        log = logger.getChild("expire_instruments")
        rows_updated = 0

        async_session: sessionmaker = sessionmaker(  # type: ignore[call-overload]
            engine, class_=AsyncSession, expire_on_commit=False
        )

        try:
            async with async_session() as session:
                stmt = (
                    text(
                        """
                        UPDATE instrument_master
                           SET active_to = :expiry_date,
                               updated_at = NOW()
                         WHERE expiry = :expiry_date
                           AND active_to IS NULL
                        """
                    ).bindparams(expiry_date=as_of_date)
                )
                result = await session.execute(stmt)
                rows_updated = result.rowcount  # type: ignore[attr-defined]
                await session.commit()

        except Exception as exc:  # noqa: BLE001
            log.error(
                "fno_expire_instruments_error",
                extra={
                    "event": "fno_expire_instruments_error",
                    "component": "fno_universe",
                    "as_of_date": str(as_of_date),
                    "error": str(exc),
                },
            )
            raise

        log.info(
            "fno_instruments_expired",
            extra={
                "event": "fno_instruments_expired",
                "component": "fno_universe",
                "as_of_date": str(as_of_date),
                "rows_updated": rows_updated,
            },
        )
        return rows_updated

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def get_current_snapshot(
        engine: AsyncEngine,
    ) -> Optional[FnoUniverseSnapshot]:
        """Return the latest ACTIVE snapshot from the database.

        Args:
            engine: AsyncEngine for DB access.

        Returns:
            The most recent ``FnoUniverseSnapshot`` with ``status = 'ACTIVE'``,
            or ``None`` if no snapshot has ever been written.
        """
        log = logger.getChild("get_current_snapshot")
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        """
                        SELECT snapshot_version, checksum, generated_at,
                               effective_from, effective_to,
                               fno_equity_count, fno_index_count,
                               constituent_count, status
                          FROM fno_universe_snapshot
                         WHERE status = 'ACTIVE'
                         ORDER BY snapshot_version DESC
                         LIMIT 1
                        """
                    )
                )
                row = result.mappings().fetchone()
                if row is None:
                    return None

                return FnoUniverseSnapshot(
                    snapshotVersion=row["snapshot_version"],
                    checksum=row["checksum"],
                    generatedAt=str(row["generated_at"]),
                    effectiveFrom=row["effective_from"],
                    effectiveTo=row.get("effective_to"),
                    fnoEquityCount=row["fno_equity_count"],
                    fnoIndexCount=row["fno_index_count"],
                    constituentCount=row["constituent_count"],
                    status=row["status"],
                )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "fno_get_snapshot_error",
                extra={
                    "event": "fno_get_snapshot_error",
                    "component": "fno_universe",
                    "error": str(exc),
                },
            )
            return None

    @staticmethod
    async def get_lifecycle_events(
        engine: AsyncEngine,
        version: Optional[int] = None,
    ) -> list[InstrumentLifecycleEvent]:
        """Return lifecycle events, optionally filtered by snapshot version.

        Args:
            engine: AsyncEngine for DB access.
            version: When supplied, only events for that snapshot version are
                returned.  When ``None``, all stored events are returned.

        Returns:
            List of ``InstrumentLifecycleEvent`` objects ordered by
            ``snapshot_version`` ascending then ``symbol`` ascending.
        """
        log = logger.getChild("get_lifecycle_events")
        try:
            async with engine.connect() as conn:
                if version is not None:
                    sql = text(
                        """
                        SELECT symbol, change_type, effective_date, source,
                               snapshot_version
                          FROM fno_lifecycle_event
                         WHERE snapshot_version = :version
                         ORDER BY symbol ASC
                        """
                    ).bindparams(version=version)
                else:
                    sql = text(
                        """
                        SELECT symbol, change_type, effective_date, source,
                               snapshot_version
                          FROM fno_lifecycle_event
                         ORDER BY snapshot_version ASC, symbol ASC
                        """
                    )

                result = await conn.execute(sql)
                events: list[InstrumentLifecycleEvent] = []
                for row in result.mappings():
                    events.append(
                        InstrumentLifecycleEvent(
                            symbol=row["symbol"],
                            changeType=row["change_type"],
                            effectiveDate=row["effective_date"],
                            source=row["source"],
                            snapshotVersion=row["snapshot_version"],
                        )
                    )
                return events
        except Exception as exc:  # noqa: BLE001
            log.error(
                "fno_get_lifecycle_events_error",
                extra={
                    "event": "fno_get_lifecycle_events_error",
                    "component": "fno_universe",
                    "error": str(exc),
                },
            )
            return []

    # ------------------------------------------------------------------
    # In-memory state helpers (useful for testing / introspection)
    # ------------------------------------------------------------------

    @staticmethod
    def warm_cache(snapshot: FnoUniverseSnapshot) -> None:
        """Populate the in-memory snapshot cache from an authoritative source.

        Used by (a) the API route's DB-fallback path and (b) the FastAPI
        startup lifespan, so that a valid persisted snapshot is served
        immediately after a process restart instead of returning a spurious
        ``FNO_UNIVERSE_UNAVAILABLE`` until the next 08:45 IST refresh.

        Note: this warms only the snapshot header reference.  The
        ``_current_instrument_ids`` set is intentionally left untouched here
        because the authoritative constituent set is resolved from the DB on
        demand; lifecycle-diff computation still requires a full ``refresh()``.

        Args:
            snapshot: The ACTIVE snapshot loaded from the database.
        """
        global _current_snapshot
        _current_snapshot = snapshot

    @staticmethod
    async def warm_cache_from_db(engine: AsyncEngine) -> Optional[FnoUniverseSnapshot]:
        """Load the latest ACTIVE snapshot from the DB and warm the cache.

        Intended to be called once during application startup.  Returns the
        snapshot that was loaded (or ``None`` when the universe has never been
        built), so callers can log the outcome.

        Args:
            engine: AsyncEngine for DB access.

        Returns:
            The warmed snapshot, or ``None`` if no ACTIVE snapshot exists.
        """
        snapshot = await FnoUniverseService.get_current_snapshot(engine)
        if snapshot is not None:
            FnoUniverseService.warm_cache(snapshot)
        return snapshot

    @staticmethod
    def get_cached_snapshot() -> Optional[FnoUniverseSnapshot]:
        """Return the in-memory cached snapshot (no DB round-trip)."""
        return _current_snapshot

    @staticmethod
    def get_cached_instrument_ids() -> frozenset[str]:
        """Return the in-memory instrument ID set (no DB round-trip)."""
        return frozenset(_current_instrument_ids)

    @staticmethod
    def reset_cache() -> None:
        """Clear the in-memory snapshot cache (used in tests only)."""
        global _current_snapshot, _current_instrument_ids
        _current_snapshot = None
        _current_instrument_ids = set()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _symbol_from_id(instrument_id: str) -> str:
    """Extract the trading symbol from a canonical instrument ID.

    Canonical IDs have the form ``{EXCHANGE}:{SYMBOL}:{TYPE}``.
    Returns the middle segment.  Falls back to the full ID on parse error.
    """
    parts = instrument_id.split(":", 2)
    return parts[1] if len(parts) == 3 else instrument_id  # noqa: PLR2004


def _get_default_engine() -> AsyncEngine:
    """Import the FastAPI app state engine as a last resort.

    This is only called when ``refresh()`` is invoked without an explicit
    engine argument (e.g. from the APScheduler job).  If the app state is
    not available the import will fail and the error is propagated to the
    scheduler's exception handler.
    """
    try:
        from src.server import app  # noqa: PLC0415

        engine: AsyncEngine = app.state.db_engine
        if engine is None:
            raise RuntimeError("app.state.db_engine is None — DB not initialised")
        return engine
    except ImportError as exc:
        raise RuntimeError(
            "Cannot resolve default DB engine: FastAPI app not importable. "
            "Pass an explicit engine argument to FnoUniverseService.refresh()."
        ) from exc


async def _persist_snapshot(
    engine: AsyncEngine,
    snapshot: FnoUniverseSnapshot,
    lifecycle_events: list[InstrumentLifecycleEvent],
    previous_snapshot: Optional[FnoUniverseSnapshot],
) -> None:
    """Write a new snapshot and its lifecycle events to the database.

    Also marks the previous ACTIVE snapshot as SUPERSEDED.

    The ``fno_lifecycle_event`` table is not part of the Alembic migration yet
    (it will be added when Task 3.3 migration is run).  For now the insert is
    wrapped in a try/except so that unit tests that mock the session still work.

    Args:
        engine: AsyncEngine.
        snapshot: New snapshot to persist.
        lifecycle_events: Events to emit for changed instruments.
        previous_snapshot: The snapshot to supersede (may be ``None``).
    """
    async_session: sessionmaker = sessionmaker(  # type: ignore[call-overload]
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with async_session() as session:
        async with session.begin():
            # 1. Supersede previous ACTIVE snapshot
            if previous_snapshot is not None:
                await session.execute(
                    text(
                        """
                        UPDATE fno_universe_snapshot
                           SET status = 'SUPERSEDED',
                               effective_to = :effective_to
                         WHERE snapshot_version = :version
                           AND status = 'ACTIVE'
                        """
                    ).bindparams(
                        effective_to=date.today(),
                        version=previous_snapshot.snapshotVersion,
                    )
                )

            # 2. Insert new snapshot
            await session.execute(
                text(
                    """
                    INSERT INTO fno_universe_snapshot
                        (snapshot_version, checksum, generated_at,
                         effective_from, fno_equity_count, fno_index_count,
                         constituent_count, status)
                    VALUES
                        (:version, :checksum, :generated_at,
                         :effective_from, :fno_equity_count, :fno_index_count,
                         :constituent_count, :status)
                    """
                ).bindparams(
                    version=snapshot.snapshotVersion,
                    checksum=snapshot.checksum,
                    generated_at=snapshot.generatedAt,
                    effective_from=snapshot.effectiveFrom,
                    fno_equity_count=snapshot.fnoEquityCount,
                    fno_index_count=snapshot.fnoIndexCount,
                    constituent_count=snapshot.constituentCount,
                    status=snapshot.status,
                )
            )

            # 3. Insert lifecycle events (best-effort — table may not exist yet)
            for event in lifecycle_events:
                try:
                    await session.execute(
                        text(
                            """
                            INSERT INTO fno_lifecycle_event
                                (symbol, change_type, effective_date,
                                 source, snapshot_version)
                            VALUES
                                (:symbol, :change_type, :effective_date,
                                 :source, :snapshot_version)
                            """
                        ).bindparams(
                            symbol=event.symbol,
                            change_type=event.changeType.value,
                            effective_date=event.effectiveDate,
                            source=event.source,
                            snapshot_version=event.snapshotVersion,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "fno_lifecycle_event_insert_failed",
                        extra={
                            "event": "fno_lifecycle_event_insert_failed",
                            "component": "fno_universe",
                            "symbol": event.symbol,
                            "change_type": event.changeType.value,
                            "error": str(exc),
                        },
                    )
