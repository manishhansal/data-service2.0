"""
Gap Recovery Engine — Task 7.3.

Implements the gap detection and recovery state machine for the Historical
Engine.  A "gap" is a contiguous missing range in a candle sequence detected
by pipeline step 7 (``src.core.validators.gap_detection``).

State Machine
-------------
::

    PENDING  ─→  RECOVERING  ─→  RECOVERED
                      │
                      └──────────→  EXHAUSTED  (after max_attempts)

- **PENDING**:    Gap has been detected and persisted; not yet attempted.
- **RECOVERING**: A recovery attempt is in-progress / just attempted.
- **RECOVERED**:  The gap has been filled by a successful provider fetch.
- **EXHAUSTED**:  Max recovery attempts reached; retained for manual review;
                  no further auto-retry.  A ``DataIncident`` is emitted.

Key design rules (Requirements 4.7, 4.8, 10.8, 10.10)
-------------------------------------------------------
- Max recovery attempts: configurable; default 5; valid range 1–10.
- ``EXHAUSTED`` gaps are never automatically retried again.
- ``EXHAUSTED`` status generates a ``DataIncident`` record with
  ``incidentId``, ``symbol``, ``exchange``, ``interval``, ``gapStart``,
  ``gapEnd``, and ``exhaustedAt``.
- Gap records are persisted to the ``data_gap`` PostgreSQL table.
- In-memory dict keyed by ``gapId`` is used for fast status queries;
  the in-memory store is rehydrated from the database on startup via
  ``load_gaps_from_db``.

Requirements: 4.7, 4.8, 10.8, 10.10
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from src.observability.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine
    from redis.asyncio import Redis as AsyncRedis

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ATTEMPTS: int = 5
_MIN_MAX_ATTEMPTS: int = 1
_MAX_MAX_ATTEMPTS: int = 10

# Valid gap statuses
_STATUS_PENDING = "PENDING"
_STATUS_RECOVERING = "RECOVERING"
_STATUS_RECOVERED = "RECOVERED"
_STATUS_EXHAUSTED = "EXHAUSTED"
_VALID_STATUSES = frozenset({
    _STATUS_PENDING, _STATUS_RECOVERING, _STATUS_RECOVERED, _STATUS_EXHAUSTED
})


# ---------------------------------------------------------------------------
# DataGap dataclass
# ---------------------------------------------------------------------------


@dataclass
class DataGap:
    """Represents a detected gap in a candle sequence.

    Attributes
    ----------
    gapId:            UUID v4 string — unique identifier for this gap.
    instrumentId:     Canonical instrument ID (e.g. ``"NSE:RELIANCE:EQ"``).
    exchange:         Exchange identifier (e.g. ``"NSE"``).
    intervalStr:      Candle interval string (e.g. ``"1m"``, ``"1d"``).
    gapStart:         UTC epoch **milliseconds** of the first missing candle.
    gapEnd:           UTC epoch **milliseconds** of the last missing candle.
    durationSec:      Duration of the gap in seconds.
    recoveryStatus:   One of ``PENDING``, ``RECOVERING``, ``RECOVERED``,
                      ``EXHAUSTED``.
    recoveryAttempts: Number of recovery attempts made so far.
    expectedProvider: The provider expected to fill this gap (from the
                      Capability_Matrix at detection time).
    recoveryProvider: Provider that successfully (or last) attempted recovery.
                      ``None`` until a recovery attempt is made.
    """

    gapId: str
    instrumentId: str
    exchange: str
    intervalStr: str
    gapStart: int       # UTC epoch ms
    gapEnd: int         # UTC epoch ms
    durationSec: int
    recoveryStatus: str
    recoveryAttempts: int
    expectedProvider: str
    recoveryProvider: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (suitable for API responses and DB rows)."""
        return {
            "gapId": self.gapId,
            "instrumentId": self.instrumentId,
            "exchange": self.exchange,
            "intervalStr": self.intervalStr,
            "gapStart": self.gapStart,
            "gapEnd": self.gapEnd,
            "durationSec": self.durationSec,
            "recoveryStatus": self.recoveryStatus,
            "recoveryAttempts": self.recoveryAttempts,
            "expectedProvider": self.expectedProvider,
            "recoveryProvider": self.recoveryProvider,
        }


# ---------------------------------------------------------------------------
# GapRecoveryEngine
# ---------------------------------------------------------------------------


class GapRecoveryEngine:
    """Manages gap lifecycle: creation, persistence, and recovery.

    All durable state is stored in the ``data_gap`` PostgreSQL table.
    The in-memory ``_gaps`` dict provides fast lookups and is rehydrated
    from the database on startup via :meth:`load_gaps_from_db`.

    Parameters
    ----------
    max_attempts:
        Maximum number of recovery attempts before a gap is marked
        ``EXHAUSTED``.  Clamped to the range [1, 10] (Requirements 4.8, 10.10).
    """

    def __init__(self, max_attempts: int = _DEFAULT_MAX_ATTEMPTS) -> None:
        # Clamp to valid range.
        self._max_attempts: int = max(
            _MIN_MAX_ATTEMPTS, min(_MAX_MAX_ATTEMPTS, max_attempts)
        )
        # In-memory store: gapId → DataGap
        self._gaps: dict[str, DataGap] = {}

    # ------------------------------------------------------------------ #
    # create_gap
    # ------------------------------------------------------------------ #

    def create_gap(
        self,
        *,
        instrument_id: str,
        exchange: str,
        interval: str,
        gap_start_ms: int,
        gap_end_ms: int,
        expected_provider: str,
    ) -> DataGap:
        """Create a new ``DataGap`` with ``PENDING`` status.

        The gap is stored in the in-memory dict immediately.  Call
        :meth:`persist_gap` to write it to the database.

        Parameters
        ----------
        instrument_id:     Canonical instrument ID.
        exchange:          Exchange identifier.
        interval:          Candle interval string.
        gap_start_ms:      UTC epoch ms of the first missing candle.
        gap_end_ms:        UTC epoch ms of the last missing candle.
        expected_provider: Provider expected to fill this gap.

        Returns
        -------
        The newly created :class:`DataGap` instance.

        Raises
        ------
        ValueError:
            If ``gap_end_ms < gap_start_ms``.
        """
        if gap_end_ms < gap_start_ms:
            raise ValueError(
                f"gap_end_ms ({gap_end_ms}) must be >= gap_start_ms ({gap_start_ms})"
            )

        duration_sec = max(0, (gap_end_ms - gap_start_ms) // 1000)

        gap = DataGap(
            gapId=str(uuid.uuid4()),
            instrumentId=instrument_id,
            exchange=exchange,
            intervalStr=interval,
            gapStart=gap_start_ms,
            gapEnd=gap_end_ms,
            durationSec=duration_sec,
            recoveryStatus=_STATUS_PENDING,
            recoveryAttempts=0,
            expectedProvider=expected_provider,
            recoveryProvider=None,
        )

        self._gaps[gap.gapId] = gap

        logger.info(
            "gap_created",
            component="gap_recovery_engine",
            gap_id=gap.gapId,
            instrument_id=instrument_id,
            exchange=exchange,
            interval=interval,
            gap_start_ms=gap_start_ms,
            gap_end_ms=gap_end_ms,
            expected_provider=expected_provider,
        )

        return gap

    # ------------------------------------------------------------------ #
    # persist_gap
    # ------------------------------------------------------------------ #

    async def persist_gap(
        self,
        gap: DataGap,
        db_engine: "AsyncEngine",
    ) -> None:
        """Insert or update a gap record in the ``data_gap`` table.

        Uses an upsert pattern: if a row with the same ``gap_id`` already
        exists, its status and attempt count are updated.

        Parameters
        ----------
        gap:       The :class:`DataGap` to persist.
        db_engine: Async SQLAlchemy engine.
        """
        from sqlalchemy import text  # local import

        gap_start_dt = _ms_to_utc_datetime(gap.gapStart)
        gap_end_dt = _ms_to_utc_datetime(gap.gapEnd)
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        upsert_sql = text(
            """
            INSERT INTO data_gap (
                gap_id,
                instrument_id,
                exchange,
                interval_str,
                gap_start,
                gap_end,
                duration_sec,
                recovery_status,
                recovery_attempts,
                expected_provider,
                recovery_provider,
                created_at,
                updated_at
            ) VALUES (
                :gap_id,
                :instrument_id,
                :exchange,
                :interval_str,
                :gap_start,
                :gap_end,
                :duration_sec,
                :recovery_status,
                :recovery_attempts,
                :expected_provider,
                :recovery_provider,
                :created_at,
                :updated_at
            )
            ON CONFLICT (gap_id) DO UPDATE SET
                recovery_status   = EXCLUDED.recovery_status,
                recovery_attempts = EXCLUDED.recovery_attempts,
                recovery_provider = EXCLUDED.recovery_provider,
                updated_at        = EXCLUDED.updated_at
            """
        )

        try:
            async with db_engine.begin() as conn:
                await conn.execute(
                    upsert_sql,
                    {
                        "gap_id": gap.gapId,
                        "instrument_id": gap.instrumentId,
                        "exchange": gap.exchange,
                        "interval_str": gap.intervalStr,
                        "gap_start": gap_start_dt,
                        "gap_end": gap_end_dt,
                        "duration_sec": gap.durationSec,
                        "recovery_status": gap.recoveryStatus,
                        "recovery_attempts": gap.recoveryAttempts,
                        "expected_provider": gap.expectedProvider,
                        "recovery_provider": gap.recoveryProvider,
                        "created_at": now_utc,
                        "updated_at": now_utc,
                    },
                )
            logger.info(
                "gap_persisted",
                component="gap_recovery_engine",
                gap_id=gap.gapId,
                status=gap.recoveryStatus,
            )
        except Exception as exc:
            logger.error(
                "gap_persist_failed",
                component="gap_recovery_engine",
                gap_id=gap.gapId,
                error=str(exc),
            )
            raise

    # ------------------------------------------------------------------ #
    # recover_gap
    # ------------------------------------------------------------------ #

    async def recover_gap(
        self,
        gap: DataGap,
        db_engine: "AsyncEngine",
        redis_client: "AsyncRedis",
    ) -> DataGap:
        """Attempt to recover a gap using the Capability_Matrix fallback provider.

        State transitions:
        - ``PENDING``    → ``RECOVERING`` (this call)
        - ``RECOVERING`` → ``RECOVERED``  (on success)
        - ``RECOVERING`` → ``EXHAUSTED``  (after max attempts, on failure)

        If the gap is already ``RECOVERED`` or ``EXHAUSTED`` it is returned
        unchanged without any further processing.

        Parameters
        ----------
        gap:          The :class:`DataGap` to attempt recovery for.
        db_engine:    Async SQLAlchemy engine for checkpoint + candle writes.
        redis_client: Async Redis client for checkpoint management.

        Returns
        -------
        The updated :class:`DataGap`.
        """
        # Guard: do not re-attempt terminal states.
        if gap.recoveryStatus in (_STATUS_RECOVERED, _STATUS_EXHAUSTED):
            logger.debug(
                "gap_recovery_skipped_terminal",
                component="gap_recovery_engine",
                gap_id=gap.gapId,
                status=gap.recoveryStatus,
            )
            return gap

        # Transition to RECOVERING.
        gap.recoveryStatus = _STATUS_RECOVERING
        gap.recoveryAttempts += 1

        # Resolve the fallback provider from the Capability_Matrix.
        fallback_provider = _resolve_fallback_provider(
            instrument_id=gap.instrumentId,
            interval=gap.intervalStr,
            expected_provider=gap.expectedProvider,
        )
        gap.recoveryProvider = fallback_provider

        logger.info(
            "gap_recovery_attempt",
            component="gap_recovery_engine",
            gap_id=gap.gapId,
            instrument_id=gap.instrumentId,
            interval=gap.intervalStr,
            attempt=gap.recoveryAttempts,
            max_attempts=self._max_attempts,
            fallback_provider=fallback_provider,
        )

        # Attempt the actual data fetch (stub: wire real adapters in Phase 4).
        success = await self._attempt_fetch(
            gap=gap,
            fallback_provider=fallback_provider,
            db_engine=db_engine,
            redis_client=redis_client,
        )

        if success:
            gap.recoveryStatus = _STATUS_RECOVERED
            logger.info(
                "gap_recovery_succeeded",
                component="gap_recovery_engine",
                gap_id=gap.gapId,
                instrument_id=gap.instrumentId,
                interval=gap.intervalStr,
                attempts=gap.recoveryAttempts,
            )
        elif gap.recoveryAttempts >= self._max_attempts:
            # Max attempts exhausted.
            gap.recoveryStatus = _STATUS_EXHAUSTED
            exhausted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

            logger.warning(
                "gap_recovery_exhausted",
                component="gap_recovery_engine",
                gap_id=gap.gapId,
                instrument_id=gap.instrumentId,
                exchange=gap.exchange,
                interval=gap.intervalStr,
                gap_start=gap.gapStart,
                gap_end=gap.gapEnd,
                attempts=gap.recoveryAttempts,
            )

            # Generate DataIncident.
            incident = _make_exhausted_incident(gap=gap, exhausted_at=exhausted_at)
            logger.warning(
                "data_incident",
                component="gap_recovery_engine",
                **incident,
            )
        else:
            # Failed but not yet exhausted; stays RECOVERING so the caller
            # can schedule another attempt.
            logger.info(
                "gap_recovery_failed_will_retry",
                component="gap_recovery_engine",
                gap_id=gap.gapId,
                attempt=gap.recoveryAttempts,
                max_attempts=self._max_attempts,
            )

        # Persist the updated gap state.
        try:
            await self.persist_gap(gap, db_engine)
        except Exception:  # noqa: BLE001
            # Log but don't fail the overall recovery — state is still updated
            # in-memory.
            logger.warning(
                "gap_persist_after_recovery_failed",
                component="gap_recovery_engine",
                gap_id=gap.gapId,
            )

        # Keep in-memory store in sync.
        self._gaps[gap.gapId] = gap

        return gap

    # ------------------------------------------------------------------ #
    # Query helpers
    # ------------------------------------------------------------------ #

    def get_pending_gaps(self) -> list[DataGap]:
        """Return all gaps with ``PENDING`` status from the in-memory store."""
        return [g for g in self._gaps.values() if g.recoveryStatus == _STATUS_PENDING]

    def get_gaps_by_status(self, status: str) -> list[DataGap]:
        """Return gaps filtered by the given status string.

        Parameters
        ----------
        status: One of ``PENDING``, ``RECOVERING``, ``RECOVERED``, ``EXHAUSTED``.

        Returns
        -------
        List of matching :class:`DataGap` objects.

        Raises
        ------
        ValueError: If ``status`` is not a recognised status value.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"Unknown gap status {status!r}. "
                f"Valid values: {sorted(_VALID_STATUSES)}"
            )
        return [g for g in self._gaps.values() if g.recoveryStatus == status]

    def get_gap_by_id(self, gap_id: str) -> Optional[DataGap]:
        """Return a single gap by its UUID, or ``None`` if not found."""
        return self._gaps.get(gap_id)

    def all_gaps(self) -> list[DataGap]:
        """Return all gaps from the in-memory store (any status)."""
        return list(self._gaps.values())

    # ------------------------------------------------------------------ #
    # load_gaps_from_db
    # ------------------------------------------------------------------ #

    async def load_gaps_from_db(self, db_engine: "AsyncEngine") -> int:
        """Rehydrate the in-memory store from the ``data_gap`` table.

        Fetches all rows and rebuilds the ``_gaps`` dict.  Existing in-memory
        entries that are not in the database are NOT removed (to preserve
        gaps that have been created but not yet persisted).

        Parameters
        ----------
        db_engine: Async SQLAlchemy engine.

        Returns
        -------
        Number of gap records loaded from the database.
        """
        from sqlalchemy import text  # local import

        select_sql = text(
            """
            SELECT
                gap_id,
                instrument_id,
                exchange,
                interval_str,
                EXTRACT(EPOCH FROM gap_start)::bigint * 1000 AS gap_start_ms,
                EXTRACT(EPOCH FROM gap_end)::bigint   * 1000 AS gap_end_ms,
                duration_sec,
                recovery_status,
                recovery_attempts,
                expected_provider,
                recovery_provider
            FROM data_gap
            ORDER BY created_at DESC
            """
        )

        try:
            async with db_engine.connect() as conn:
                result = await conn.execute(select_sql)
                rows = result.fetchall()
        except Exception as exc:
            logger.error(
                "gap_load_from_db_failed",
                component="gap_recovery_engine",
                error=str(exc),
            )
            return 0

        count = 0
        for row in rows:
            gap = DataGap(
                gapId=str(row.gap_id),
                instrumentId=str(row.instrument_id),
                exchange=str(row.exchange),
                intervalStr=str(row.interval_str),
                gapStart=int(row.gap_start_ms),
                gapEnd=int(row.gap_end_ms),
                durationSec=int(row.duration_sec),
                recoveryStatus=str(row.recovery_status),
                recoveryAttempts=int(row.recovery_attempts),
                expectedProvider=str(row.expected_provider),
                recoveryProvider=(
                    str(row.recovery_provider) if row.recovery_provider else None
                ),
            )
            self._gaps[gap.gapId] = gap
            count += 1

        logger.info(
            "gaps_loaded_from_db",
            component="gap_recovery_engine",
            count=count,
        )
        return count

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    async def _attempt_fetch(
        self,
        *,
        gap: DataGap,
        fallback_provider: str,
        db_engine: "AsyncEngine",
        redis_client: "AsyncRedis",
    ) -> bool:
        """Attempt to fetch missing candles from the fallback provider.

        This is a stub that will be wired to real provider adapters in
        Phase 4 (Tasks 4.5–4.8).  Until then, it always returns ``False``
        (simulating a failed fetch) so that the state machine can be
        tested end-to-end.

        In the full implementation, this will:
        1. Call the fallback provider adapter via the ProviderGateway.
        2. Run the validation pipeline on the fetched candles.
        3. Bulk-upsert valid candles into ``candle_bar``.
        4. Update the backfill checkpoint in Redis.
        5. Return ``True`` on success, ``False`` on failure.
        """
        # TODO(task-4.5-4.8): wire real provider adapters via ProviderGateway.
        logger.debug(
            "gap_recovery_fetch_stub",
            component="gap_recovery_engine",
            gap_id=gap.gapId,
            fallback_provider=fallback_provider,
        )
        return False  # stub — always fails until real adapters are wired


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _resolve_fallback_provider(
    *,
    instrument_id: str,
    interval: str,
    expected_provider: str,
) -> str:
    """Select a fallback provider for gap recovery using the Capability_Matrix.

    The fallback selection logic mirrors the routing rules in
    ``src.engines.historical_engine._resolve_provider``:
    - If the primary provider was Angel One → try Upstox, then OpenChart.
    - If the primary was Upstox → try Angel One, then OpenChart.
    - If the primary was Jugaad → try OpenChart.
    - All other cases → OpenChart (open-source supplement, Req 10.3).

    Parameters
    ----------
    instrument_id:     Canonical instrument ID (unused in current logic
                       but reserved for future instrument-class lookup).
    interval:          Candle interval string.
    expected_provider: The provider that was originally assigned to this gap.

    Returns
    -------
    A provider identifier string.
    """
    from src.core.schemas.provider import ProviderId  # local import

    fallback_chain: dict[str, str] = {
        ProviderId.ANGEL_ONE.value:   ProviderId.UPSTOX.value,
        ProviderId.UPSTOX.value:      ProviderId.ANGEL_ONE.value,
        ProviderId.JUGAAD_DATA.value: ProviderId.OPENCHART.value,
    }

    # If the expected provider has a defined fallback, use it.
    if expected_provider in fallback_chain:
        return fallback_chain[expected_provider]

    # Default fallback: OpenChart (open-source supplement for all timeframes).
    return ProviderId.OPENCHART.value


def _make_exhausted_incident(
    *,
    gap: DataGap,
    exhausted_at: str,
) -> dict[str, Any]:
    """Build a DataIncident dict for a gap that reached EXHAUSTED status.

    Fields match the incident schema from Requirements 10.10, 18.7.
    """
    # Extract a short symbol from the instrumentId (e.g. "NSE:RELIANCE:EQ" → "RELIANCE")
    parts = gap.instrumentId.split(":")
    symbol = parts[1] if len(parts) >= 2 else gap.instrumentId

    return {
        "incidentId": str(uuid.uuid4()),
        "incidentType": "GAP_RECOVERY_EXHAUSTED",
        "instrumentId": gap.instrumentId,
        "provider": gap.recoveryProvider or gap.expectedProvider,
        "timestamp": exhausted_at,
        "severity": "HIGH",
        "details": {
            "symbol": symbol,
            "exchange": gap.exchange,
            "interval": gap.intervalStr,
            "gapStart": gap.gapStart,
            "gapEnd": gap.gapEnd,
            "exhaustedAt": exhausted_at,
            "recoveryAttempts": gap.recoveryAttempts,
        },
    }


def _ms_to_utc_datetime(epoch_ms: int) -> datetime.datetime:
    """Convert UTC epoch milliseconds to an aware datetime object."""
    return datetime.datetime.fromtimestamp(
        epoch_ms / 1000.0, tz=datetime.timezone.utc
    )
