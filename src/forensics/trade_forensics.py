"""
Trade forensics provenance recording.

Every trade (live, paper, or simulated) that flows through the platform must
be stamped with the data observations and quality-gate state that existed at
execution time.  This allows any future forensics audit to reconstruct the
full data → signal → trade chain.

The ``TradeForensicsStore`` keeps an in-memory dict of records indexed by
``tradeId``.  Persistence to the ``data_provenance`` / ``data_incident``
tables is handled separately by the lineage store (task 10.2).

Requirements: 8.7
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TradeForensicsRecord(BaseModel):
    """Immutable provenance record that ties a trade to its data lineage.

    All fields are set at the moment ``TradeForensicsStore.record()`` is
    called and must not be mutated afterwards.

    Requirements: 8.7
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    # ── Trade identity ───────────────────────────────────────────────────
    tradeId: str = Field(
        description=(
            "Unique identifier for the trade.  Typically a UUID v4 assigned "
            "by the order management layer."
        )
    )
    strategyId: str = Field(
        description="Identifier of the strategy that generated the trade signal."
    )

    # ── Instrument ───────────────────────────────────────────────────────
    instrumentId: str = Field(
        description=(
            "Platform-canonical instrument ID of the instrument that was traded."
        )
    )
    exchange: str = Field(
        description="Exchange on which the trade was executed (e.g. 'NSE', 'NFO')."
    )

    # ── Data lineage ─────────────────────────────────────────────────────
    observationIds: list[str] = Field(
        description=(
            "Ordered list of ``dataObservationId`` values (UUID v4) that were "
            "consumed when the signal was evaluated.  At least one observation "
            "is expected; an empty list is permitted only for synthetic/test trades."
        )
    )

    # ── Quality gate snapshot ────────────────────────────────────────────
    dataConfidenceAtExecution: int = Field(
        ge=0,
        le=95,
        description=(
            "``DataConfidenceScore`` (0–95) at the moment the trade was executed. "
            "Scores above 95 are impossible; the ceiling is enforced here."
        ),
    )
    signalEngineAllowed: bool = Field(
        description=(
            "``True`` if the ``DataQualityGate`` was open (all five conditions "
            "satisfied) at execution time.  ``False`` implies the trade was "
            "executed with degraded or blocked data quality — this should be "
            "treated as a critical finding in any post-trade review."
        )
    )

    # ── Timing ───────────────────────────────────────────────────────────
    executedAt: str = Field(
        description=(
            "UTC ISO-8601 timestamp (with 'Z' suffix) of when the trade was "
            "executed, e.g. '2025-01-15T09:30:05.123Z'."
        )
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TradeForensicsStore:
    """In-memory store for ``TradeForensicsRecord`` objects.

    Thread-safety note: this implementation uses plain dicts which are safe
    for the GIL-protected CPython read/write patterns used here.  For true
    async/multi-threaded environments wrap mutations in an ``asyncio.Lock``.

    The store is deliberately kept simple and in-memory.  Durable persistence
    is the responsibility of the lineage store (task 10.2).  On process
    restart, records accumulated in this store are lost; the canonical source
    of truth is the database.

    Requirements: 8.7
    """

    def __init__(self) -> None:
        # Primary index: tradeId → record
        self._by_trade: dict[str, TradeForensicsRecord] = {}

        # Secondary indexes for efficient look-up by strategy / instrument
        self._by_strategy: dict[str, list[TradeForensicsRecord]] = defaultdict(list)
        self._by_instrument: dict[str, list[TradeForensicsRecord]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(
        self,
        trade_id: str,
        observation_ids: list[str],
        strategy_id: str,
        confidence_score: int,
        signal_allowed: bool,
        instrument_id: str,
        exchange: str,
        *,
        executed_at: Optional[str] = None,
    ) -> TradeForensicsRecord:
        """Create and store a ``TradeForensicsRecord``.

        Parameters
        ----------
        trade_id:
            Unique identifier for the trade.
        observation_ids:
            ``dataObservationId`` values consumed during signal evaluation.
        strategy_id:
            Identifier of the strategy that generated the signal.
        confidence_score:
            ``DataConfidenceScore`` at execution time (0–95).
        signal_allowed:
            Whether the ``DataQualityGate`` was open at execution time.
        instrument_id:
            Platform-canonical instrument ID.
        exchange:
            Exchange on which the trade was executed.
        executed_at:
            UTC ISO-8601 timestamp of execution.  When ``None`` (default),
            the current UTC time is used.

        Returns
        -------
        TradeForensicsRecord
            The newly created, immutable record.

        Raises
        ------
        ValueError
            If ``trade_id`` already exists in the store (duplicate recording
            is disallowed to preserve audit integrity).
        """
        if trade_id in self._by_trade:
            raise ValueError(
                f"TradeForensicsRecord for tradeId={trade_id!r} already exists. "
                "Duplicate recording is not permitted."
            )

        ts = executed_at or _utc_now_iso()

        rec = TradeForensicsRecord(
            tradeId=trade_id,
            observationIds=list(observation_ids),
            strategyId=strategy_id,
            dataConfidenceAtExecution=confidence_score,
            signalEngineAllowed=signal_allowed,
            executedAt=ts,
            instrumentId=instrument_id,
            exchange=exchange,
        )

        self._by_trade[trade_id] = rec
        self._by_strategy[strategy_id].append(rec)
        self._by_instrument[instrument_id].append(rec)

        logger.debug(
            "trade_forensics_recorded",
            extra={
                "tradeId": trade_id,
                "strategyId": strategy_id,
                "instrumentId": instrument_id,
                "confidenceScore": confidence_score,
                "signalAllowed": signal_allowed,
                "observationCount": len(observation_ids),
            },
        )

        return rec

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, trade_id: str) -> Optional[TradeForensicsRecord]:
        """Return the record for ``trade_id``, or ``None`` if not found."""
        return self._by_trade.get(trade_id)

    def get_by_strategy(
        self,
        strategy_id: str,
        limit: int = 100,
    ) -> list[TradeForensicsRecord]:
        """Return the most recent ``limit`` records for ``strategy_id``.

        Records are returned in reverse insertion order (most recent first).
        If the strategy has no records an empty list is returned.

        Parameters
        ----------
        strategy_id:
            Strategy identifier to filter by.
        limit:
            Maximum number of records to return (1–1 000).  Values outside
            this range are silently clamped.
        """
        limit = max(1, min(limit, 1000))
        records = self._by_strategy.get(strategy_id, [])
        return list(reversed(records[-limit:]))

    def get_by_instrument(
        self,
        instrument_id: str,
        limit: int = 100,
    ) -> list[TradeForensicsRecord]:
        """Return the most recent ``limit`` records for ``instrument_id``.

        Records are returned in reverse insertion order (most recent first).
        If the instrument has no records an empty list is returned.

        Parameters
        ----------
        instrument_id:
            Platform-canonical instrument ID to filter by.
        limit:
            Maximum number of records to return (1–1 000).  Values outside
            this range are silently clamped.
        """
        limit = max(1, min(limit, 1000))
        records = self._by_instrument.get(instrument_id, [])
        return list(reversed(records[-limit:]))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Total number of records currently held in the store."""
        return len(self._by_trade)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with 'Z' suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
