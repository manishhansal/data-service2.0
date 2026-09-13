"""
Historical Engine — Task 7.1: Resumable Checkpointed Backfill.

The HistoricalEngine is responsible for:
  - Acquiring historical OHLCV candle data for NSE equities, indices, F&O
    instruments, and Binance crypto.
  - Splitting large date ranges into provider-safe chunks (per Capability Matrix
    chunk limits).
  - Persisting candles to the ``candle_bar`` table via bulk upsert.
  - Maintaining a resumable checkpoint in Redis so that interrupted backfill
    jobs resume from the last successfully persisted candle, not from scratch.

Critical invariants (non-negotiable):
  - ``3m`` interval for Indian market data (is_indian_market=True) raises
    ``ValueError`` BEFORE any I/O — no exceptions (Requirements 1.5, 4.2,
    10.11).
  - Routing follows the Capability_Matrix:
      * Angel One → primary for EQ intraday (1m–1h)
      * Upstox    → primary for IDX intraday
      * Jugaad    → primary for FO EOD (1d)
      * OpenChart → reconciliation fallback for all canonical timeframes

Checkpoint scheme (Requirement 10.1):
  Key:   ``mds:backfill:checkpoint:{symbol}:{exchange}:{interval}``
  Value: UTC ISO-8601 timestamp of the last successfully persisted candle.
  TTL:   None — the checkpoint is persistent.

Resume behaviour (Requirement 10.2):
  On next run, if a checkpoint exists the ``from_ts`` is overridden with the
  checkpoint value so acquisition starts from where it left off.

Bulk upsert pattern (design §candle_bar DDL):
  INSERT … ON CONFLICT (instrument_id, exchange, interval_str, time) DO UPDATE …

Requirements: 4.1, 4.2, 10.1, 10.2, 10.3, 10.4, 10.11
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import uuid
from typing import Any, Optional, TYPE_CHECKING

from src.core.schemas.provider import (
    CANONICAL_INDIAN_TIMEFRAMES,
    CRYPTO_INSTRUMENT_CLASSES,
    DataType,
    ProviderId,
)
from src.observability.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine
    from redis.asyncio import Redis as AsyncRedis

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Checkpoint Redis key pattern (no TTL — persistent).
_CHECKPOINT_KEY_TEMPLATE = "mds:backfill:checkpoint:{symbol}:{exchange}:{interval}"

# Minimum history depths per interval (calendar days).
# These are the _minimum_ depths; actual backfill range is determined by
# the caller.
MINIMUM_HISTORY_DAYS: dict[str, int] = {
    "1m":  60,
    "5m":  180,
    "10m": 180,
    "15m": 180,
    "30m": 180,
    "1h":  365,
    "1d":  3650,   # 10 years
    "1w":  3650,
    "1M":  3650,
}

# Instrument-class to primary provider mapping.
# These mirror the Capability_Matrix routing rules from the design.
_INSTRUMENT_CLASS_PRIMARY_PROVIDER: dict[str, ProviderId] = {
    "EQ":  ProviderId.ANGEL_ONE,    # Angel One: primary for equity intraday
    "IDX": ProviderId.UPSTOX,       # Upstox: primary for index intraday
    "FO":  ProviderId.JUGAAD_DATA,  # Jugaad: primary for F&O EOD (1d)
}

# Fallback provider for all instrument classes / reconciliation.
_FALLBACK_PROVIDER = ProviderId.OPENCHART

# Per-provider, per-interval chunk sizes (calendar days).
# Source: design doc "Provider chunk limits" table + capability_matrix.py.
_ANGEL_ONE_CHUNK_DAYS: dict[str, int] = {
    "1m":  30,
    "5m":  90,
    "10m": 90,
    "15m": 90,
    "30m": 90,
    "1h":  90,
    "1d":  365,
    "1w":  365,
    "1M":  365,
}

_UPSTOX_CHUNK_DAYS: dict[str, int] = {
    "1m":  7,
    "5m":  30,
    "10m": 30,
    "15m": 30,
    "30m": 30,
    "1h":  30,
    "1d":  365,
    "1w":  365,
    "1M":  365,
}

_OPENCHART_CHUNK_DAYS: int = 365   # any interval
_JUGAAD_CHUNK_DAYS: int = 3650     # EOD F&O, up to 10 years

# Provider timeout (seconds) — backfill job treats a provider call that
# exceeds this as an interruption and resumes from checkpoint on next run.
_PROVIDER_TIMEOUT_SEC: float = 30.0

# ---------------------------------------------------------------------------
# Reconciliation thresholds (Requirements 10.5, 10.6, 10.7)
# ---------------------------------------------------------------------------

# Fields included in cross-provider OHLCV comparison.
_RECONCILIATION_FIELDS = ("open", "high", "low", "close", "volume")

# Deviation boundaries (percent).
_CONFIRMED_THRESHOLD_PCT: float = 0.5       # ≤ 0.5% → CONFIRMED
_MINOR_THRESHOLD_PCT: float = 2.0           # > 0.5% and ≤ 2.0% → MINOR_DISCREPANCY
                                             # > 2.0% → MAJOR_DISCREPANCY

# Reconciliation status strings.
RECONCILIATION_CONFIRMED = "CONFIRMED"
RECONCILIATION_MINOR = "MINOR_DISCREPANCY"
RECONCILIATION_MAJOR = "MAJOR_DISCREPANCY"


@dataclasses.dataclass
class ReconciliationResult:
    """Result of a cross-provider OHLCV comparison for one candle tuple.

    Attributes:
        status:               Overall reconciliation status — ``CONFIRMED``,
                              ``MINOR_DISCREPANCY``, or ``MAJOR_DISCREPANCY``.
                              Determined by the worst-case field deviation.
        instrument_id:        Canonical instrument ID (e.g. ``"NSE:RELIANCE"``).
        exchange:             Exchange identifier (e.g. ``"NSE"``).
        interval:             Candle interval string (e.g. ``"1m"``).
        timestamp:            Candle open time as UTC epoch seconds.
        provider_a:           First provider identifier string.
        provider_b:           Second provider identifier string.
        field_deviations:     Per-field percentage deviation computed as
                              ``|A − B| / max(|A|, |B|) × 100``.  Zero when
                              both values are zero.
        worst_field:          The OHLCV field with the highest deviation.
        worst_deviation_pct:  The percentage deviation of ``worst_field``.
        incident:             A DataIncident-compatible dict when
                              ``status == MAJOR_DISCREPANCY``; ``None`` otherwise.

    Requirements: 10.5, 10.6, 10.7
    """

    status: str
    instrument_id: str
    exchange: str
    interval: str
    timestamp: int
    provider_a: str
    provider_b: str
    field_deviations: dict[str, float]
    worst_field: str
    worst_deviation_pct: float
    incident: Optional[dict]


# ---------------------------------------------------------------------------
# DataIncident helper (lightweight — avoids circular import with pipeline.py)
# ---------------------------------------------------------------------------

def _make_incident(
    incident_type: str,
    instrument_id: str,
    provider: str,
    severity: str,
    details: dict,
) -> dict:
    """Build a minimal DataIncident-compatible dict."""
    return {
        "incidentId": str(uuid.uuid4()),
        "incidentType": incident_type,
        "instrumentId": instrument_id,
        "provider": provider,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "severity": severity,
        "details": details,
    }


def _deviation_pct(a: float, b: float) -> float:
    """Compute percentage deviation between two values.

    Formula (non-negotiable per design):
        |A − B| / max(|A|, |B|) × 100

    When both values are zero, deviation is 0.0 (no discrepancy).
    """
    denom = max(abs(a), abs(b))
    if denom == 0.0:
        return 0.0
    return abs(a - b) / denom * 100.0


# ---------------------------------------------------------------------------
# HistoricalEngine
# ---------------------------------------------------------------------------


class HistoricalEngine:
    """Manages historical OHLCV acquisition, backfill, and gap-recovery routing.

    This class is stateless between method calls — all durable state lives
    in Redis (checkpoints) and PostgreSQL (candles + gaps).  The one
    exception is the in-memory reconciliation results store
    (``_reconciliation_results``) which accumulates ``ReconciliationResult``
    objects across ``reconcile()`` calls for the lifetime of the process.

    Usage::

        engine_instance = HistoricalEngine()
        await engine_instance.run_backfill(
            symbol="RELIANCE",
            exchange="NSE",
            instrument_class="EQ",
            interval="1m",
            from_ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            to_ts=datetime(2024, 3, 1, tzinfo=timezone.utc),
            db_engine=db_engine,
            redis_client=redis_client,
        )
    """

    def __init__(self) -> None:
        # In-memory store of reconciliation results for statistics reporting.
        # This accumulates across calls in the process lifetime.
        self._reconciliation_results: list[ReconciliationResult] = []

    # ------------------------------------------------------------------ #
    # Core public method
    # ------------------------------------------------------------------ #

    async def run_backfill(
        self,
        *,
        symbol: str,
        exchange: str,
        instrument_class: str,
        interval: str,
        from_ts: datetime.datetime,
        to_ts: datetime.datetime,
        db_engine: "AsyncEngine",
        redis_client: "AsyncRedis",
        provider: Optional[ProviderId] = None,
        is_indian_market: bool = True,
    ) -> dict:
        """Run a resumable, checkpointed backfill for a single instrument.

        This is the main entry point for both scheduled and on-demand backfills.

        The method:
        1. Hard-blocks ``interval="3m"`` for Indian market data.
        2. Reads any existing Redis checkpoint and advances ``from_ts`` to
           resume mid-range rather than re-fetching already-stored candles.
        3. Resolves the primary provider from ``instrument_class`` if one is
           not supplied explicitly.
        4. Splits the (possibly advanced) date range into provider-safe chunks
           using ``chunk_date_ranges``.
        5. For each chunk: fetch → validate invariants → bulk upsert →
           update checkpoint.
        6. Returns a summary dict with chunk counts, candle counts, and any
           incidents generated.

        Args:
            symbol:           Instrument trading symbol (e.g. "RELIANCE").
            exchange:         Exchange identifier (e.g. "NSE", "NFO").
            instrument_class: Instrument class — "EQ", "FO", "IDX", or crypto.
            interval:         Candle interval string (e.g. "1m", "1d").
            from_ts:          Start of the desired date range (UTC, inclusive).
            to_ts:            End of the desired date range (UTC, exclusive).
            db_engine:        Async SQLAlchemy engine for bulk upsert.
            redis_client:     Async Redis client for checkpoint read/write.
            provider:         Override the primary provider.  When ``None``,
                              the provider is resolved from ``instrument_class``
                              via the routing table.
            is_indian_market: Set to ``False`` for Binance crypto data — the
                              ``3m`` block only applies when ``True``.

        Returns:
            Summary dict::

                {
                    "symbol": str,
                    "exchange": str,
                    "interval": str,
                    "chunks_attempted": int,
                    "chunks_succeeded": int,
                    "candles_persisted": int,
                    "resumed_from_checkpoint": bool,
                    "checkpoint_ts": str | None,   # UTC ISO-8601 or None
                    "incidents": list[dict],
                }

        Raises:
            ValueError: If ``is_indian_market=True`` and ``interval == "3m"``.
                        This is an unconditional hard block — no I/O is
                        performed.
        """
        # ── Hard block: 3m is permanently unsupported for Indian market ───
        if is_indian_market and interval == "3m":
            raise ValueError(
                "interval 3m is permanently unsupported for Indian market data"
            )

        instrument_id = f"{exchange}:{symbol}"

        # ── Read checkpoint (resume from last persisted candle if present) ─
        checkpoint_ts = await self.get_checkpoint(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            redis_client=redis_client,
        )
        resumed = checkpoint_ts is not None
        if checkpoint_ts is not None and checkpoint_ts > from_ts:
            logger.info(
                "backfill_resuming_from_checkpoint",
                component="historical_engine",
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                checkpoint_ts=checkpoint_ts.isoformat(),
                original_from_ts=from_ts.isoformat(),
            )
            from_ts = checkpoint_ts

        # Sanity: if checkpoint has passed to_ts there's nothing to do.
        if from_ts >= to_ts:
            logger.info(
                "backfill_already_complete",
                component="historical_engine",
                symbol=symbol,
                exchange=exchange,
                interval=interval,
            )
            return {
                "symbol": symbol,
                "exchange": exchange,
                "interval": interval,
                "chunks_attempted": 0,
                "chunks_succeeded": 0,
                "candles_persisted": 0,
                "resumed_from_checkpoint": resumed,
                "checkpoint_ts": checkpoint_ts.isoformat() if checkpoint_ts else None,
                "incidents": [],
            }

        # ── Resolve provider from instrument_class ────────────────────────
        if provider is None:
            provider = self._resolve_provider(
                instrument_class=instrument_class,
                interval=interval,
                is_indian_market=is_indian_market,
            )

        # ── Determine chunk size for this provider × interval ─────────────
        max_chunk_days = self._get_chunk_days(provider=provider, interval=interval)

        # ── Build chunk list ───────────────────────────────────────────────
        chunks = self.chunk_date_ranges(
            from_ts=from_ts,
            to_ts=to_ts,
            max_chunk_days=max_chunk_days,
        )

        # ── Acquisition loop ───────────────────────────────────────────────
        chunks_attempted = 0
        chunks_succeeded = 0
        candles_persisted = 0
        incidents: list[dict] = []
        last_checkpoint_ts: Optional[datetime.datetime] = checkpoint_ts

        for chunk_start, chunk_end in chunks:
            chunks_attempted += 1
            try:
                # Fetch candles from provider (stub: yields empty list until
                # real adapters are wired in Phase 4.5–4.8).
                candles = await asyncio.wait_for(
                    self._fetch_candles(
                        provider=provider,
                        symbol=symbol,
                        exchange=exchange,
                        instrument_class=instrument_class,
                        interval=interval,
                        from_ts=chunk_start,
                        to_ts=chunk_end,
                    ),
                    timeout=_PROVIDER_TIMEOUT_SEC,
                )

                # Validate each candle against OHLCV invariants.
                valid_candles, chunk_incidents = self._validate_candles(
                    candles=candles,
                    provider=provider.value,
                    instrument_id=instrument_id,
                    interval=interval,
                )
                incidents.extend(chunk_incidents)

                # Bulk upsert valid candles to candle_bar.
                if valid_candles:
                    await self.bulk_upsert_candles(
                        candles=valid_candles,
                        db_engine=db_engine,
                        symbol=symbol,
                        exchange=exchange,
                        interval=interval,
                        provider=provider.value,
                    )
                    candles_persisted += len(valid_candles)

                    # Update checkpoint to the last persisted candle's time.
                    last_candle_time = self._extract_candle_time(valid_candles[-1])
                    if last_candle_time is not None:
                        await self.set_checkpoint(
                            symbol=symbol,
                            exchange=exchange,
                            interval=interval,
                            ts=last_candle_time,
                            redis_client=redis_client,
                        )
                        last_checkpoint_ts = last_candle_time

                chunks_succeeded += 1

                logger.info(
                    "backfill_chunk_complete",
                    component="historical_engine",
                    symbol=symbol,
                    exchange=exchange,
                    interval=interval,
                    provider=provider.value,
                    chunk_start=chunk_start.isoformat(),
                    chunk_end=chunk_end.isoformat(),
                    candles=len(valid_candles) if valid_candles else 0,
                )

            except asyncio.TimeoutError:
                # Provider timeout > 30s — treat as interruption; checkpoint
                # already written after the last successful chunk so the next
                # run resumes correctly (Requirement 10.2).
                logger.warning(
                    "backfill_chunk_timeout",
                    component="historical_engine",
                    symbol=symbol,
                    exchange=exchange,
                    interval=interval,
                    provider=provider.value,
                    chunk_start=chunk_start.isoformat(),
                    chunk_end=chunk_end.isoformat(),
                )
                incidents.append(
                    _make_incident(
                        incident_type="PROVIDER_TIMEOUT",
                        instrument_id=instrument_id,
                        provider=provider.value,
                        severity="MEDIUM",
                        details={
                            "intervalStr": interval,
                            "chunkStart": chunk_start.isoformat(),
                            "chunkEnd": chunk_end.isoformat(),
                            "timeoutSec": _PROVIDER_TIMEOUT_SEC,
                        },
                    )
                )
                # Stop processing remaining chunks; resume from checkpoint on
                # the next scheduled run.
                break

            except Exception as exc:  # noqa: BLE001
                # Unhandled exception — log and break; checkpoint preserves
                # the progress so far (Requirement 10.2).
                logger.error(
                    "backfill_chunk_error",
                    component="historical_engine",
                    symbol=symbol,
                    exchange=exchange,
                    interval=interval,
                    provider=provider.value,
                    chunk_start=chunk_start.isoformat(),
                    chunk_end=chunk_end.isoformat(),
                    error=str(exc),
                )
                incidents.append(
                    _make_incident(
                        incident_type="BACKFILL_ERROR",
                        instrument_id=instrument_id,
                        provider=provider.value,
                        severity="HIGH",
                        details={
                            "intervalStr": interval,
                            "chunkStart": chunk_start.isoformat(),
                            "chunkEnd": chunk_end.isoformat(),
                            "error": str(exc),
                        },
                    )
                )
                break

        return {
            "symbol": symbol,
            "exchange": exchange,
            "interval": interval,
            "chunks_attempted": chunks_attempted,
            "chunks_succeeded": chunks_succeeded,
            "candles_persisted": candles_persisted,
            "resumed_from_checkpoint": resumed,
            "checkpoint_ts": last_checkpoint_ts.isoformat() if last_checkpoint_ts else None,
            "incidents": incidents,
        }

    # ------------------------------------------------------------------ #
    # reconcile — cross-provider OHLCV comparison (Task 7.2)
    # ------------------------------------------------------------------ #

    def reconcile(
        self,
        *,
        instrument_id: str,
        exchange: str,
        interval: str,
        timestamp: int,
        a_values: dict[str, Any],
        b_values: dict[str, Any],
        provider_a: str,
        provider_b: str,
    ) -> ReconciliationResult:
        """Compare OHLCV values from two providers for the same candle tuple.

        Computes a per-field percentage deviation using the formula::

            deviation_pct = |A − B| / max(|A|, |B|) × 100

        When both A and B are zero, deviation is defined as 0.0 (no
        discrepancy — the field is genuinely zero from both providers).

        The overall reconciliation status is determined by the worst-case
        field deviation across all OHLCV fields:

        - All deviations ≤ 0.5%           → ``CONFIRMED``
        - Any deviation > 0.5% and ≤ 2.0% → ``MINOR_DISCREPANCY``
        - Any deviation > 2.0%             → ``MAJOR_DISCREPANCY``

        When the status is ``MAJOR_DISCREPANCY``, a ``DataIncident`` dict is
        generated and attached to the result (Requirement 10.7).

        Args:
            instrument_id: Canonical instrument ID.
            exchange:      Exchange identifier.
            interval:      Candle interval string (e.g. ``"1m"``).
            timestamp:     Candle open time as UTC epoch seconds.
            a_values:      OHLCV dict from provider A.  Must contain keys
                           ``open``, ``high``, ``low``, ``close``, ``volume``.
            b_values:      OHLCV dict from provider B.  Same shape as
                           ``a_values``.
            provider_a:    Identifier string for provider A.
            provider_b:    Identifier string for provider B.

        Returns:
            :class:`ReconciliationResult` with full per-field deviations,
            overall status, and an incident dict when applicable.

        Requirements: 10.5, 10.6, 10.7
        """
        field_deviations: dict[str, float] = {}

        for field in _RECONCILIATION_FIELDS:
            a_val = float(a_values.get(field) or 0)
            b_val = float(b_values.get(field) or 0)
            field_deviations[field] = _deviation_pct(a_val, b_val)

        # Determine worst-case field.
        worst_field = max(field_deviations, key=lambda f: field_deviations[f])
        worst_dev = field_deviations[worst_field]

        # Classify overall status by worst-case deviation.
        if worst_dev > _MINOR_THRESHOLD_PCT:
            status = RECONCILIATION_MAJOR
        elif worst_dev > _CONFIRMED_THRESHOLD_PCT:
            status = RECONCILIATION_MINOR
        else:
            status = RECONCILIATION_CONFIRMED

        # Generate DataIncident for MAJOR_DISCREPANCY (Requirement 10.7).
        incident: Optional[dict] = None
        if status == RECONCILIATION_MAJOR:
            incident = _make_incident(
                incident_type="MAJOR_DISCREPANCY",
                instrument_id=instrument_id,
                provider=f"{provider_a}/{provider_b}",
                severity="HIGH",
                details={
                    "exchange": exchange,
                    "intervalStr": interval,
                    "timestamp": timestamp,
                    "providerA": provider_a,
                    "providerB": provider_b,
                    "fieldDeviations": field_deviations,
                    "worstField": worst_field,
                    "worstDeviationPct": worst_dev,
                },
            )
            logger.warning(
                "reconciliation_major_discrepancy",
                component="historical_engine",
                instrument_id=instrument_id,
                exchange=exchange,
                interval=interval,
                timestamp=timestamp,
                provider_a=provider_a,
                provider_b=provider_b,
                worst_field=worst_field,
                worst_deviation_pct=round(worst_dev, 4),
            )

        result = ReconciliationResult(
            status=status,
            instrument_id=instrument_id,
            exchange=exchange,
            interval=interval,
            timestamp=timestamp,
            provider_a=provider_a,
            provider_b=provider_b,
            field_deviations=field_deviations,
            worst_field=worst_field,
            worst_deviation_pct=worst_dev,
            incident=incident,
        )

        # Accumulate for statistics (get_reconciliation_stats).
        self._reconciliation_results.append(result)

        return result

    # ------------------------------------------------------------------ #
    # get_reconciliation_stats — aggregated statistics (Task 7.4)
    # ------------------------------------------------------------------ #

    def get_reconciliation_stats(self) -> dict:
        """Return aggregated cross-provider reconciliation statistics.

        Aggregates all ``ReconciliationResult`` objects collected by
        ``reconcile()`` calls on this engine instance.

        Returns:
            Dict with ``totalCompared``, ``matched``, ``matchRatePct``,
            ``distribution``, and ``byProviderPair`` keys suitable for the
            ``GET /v1/india/historical/reconciliation`` endpoint.

        Requirements: 10.5, 10.6, 10.7 (reporting)
        """
        results = self._reconciliation_results
        total = len(results)
        confirmed = sum(1 for r in results if r.status == RECONCILIATION_CONFIRMED)
        minor = sum(1 for r in results if r.status == RECONCILIATION_MINOR)
        major = sum(1 for r in results if r.status == RECONCILIATION_MAJOR)
        match_rate = round(confirmed / total * 100.0, 2) if total > 0 else 0.0

        # Per-provider-pair breakdown.
        pair_buckets: dict[str, list[ReconciliationResult]] = {}
        for r in results:
            pair_key = f"{r.provider_a}/{r.provider_b}"
            pair_buckets.setdefault(pair_key, []).append(r)

        by_provider_pair: dict[str, dict] = {}
        for pair_key, pair_results in pair_buckets.items():
            pair_total = len(pair_results)
            pair_confirmed = sum(
                1 for r in pair_results if r.status == RECONCILIATION_CONFIRMED
            )
            pair_minor = sum(
                1 for r in pair_results if r.status == RECONCILIATION_MINOR
            )
            pair_major = sum(
                1 for r in pair_results if r.status == RECONCILIATION_MAJOR
            )
            by_provider_pair[pair_key] = {
                "totalCompared": pair_total,
                "matched": pair_confirmed,
                "matchRatePct": round(pair_confirmed / pair_total * 100.0, 2)
                if pair_total > 0
                else 0.0,
                "distribution": {
                    RECONCILIATION_CONFIRMED: pair_confirmed,
                    RECONCILIATION_MINOR: pair_minor,
                    RECONCILIATION_MAJOR: pair_major,
                },
            }

        return {
            "totalCompared": total,
            "matched": confirmed,
            "matchRatePct": match_rate,
            "distribution": {
                RECONCILIATION_CONFIRMED: confirmed,
                RECONCILIATION_MINOR: minor,
                RECONCILIATION_MAJOR: major,
            },
            "byProviderPair": by_provider_pair,
        }

    # ------------------------------------------------------------------ #
    # chunk_date_ranges
    # ------------------------------------------------------------------ #

    @staticmethod
    def chunk_date_ranges(
        from_ts: datetime.datetime,
        to_ts: datetime.datetime,
        max_chunk_days: int,
    ) -> list[tuple[datetime.datetime, datetime.datetime]]:
        """Split a date range into chunks of at most ``max_chunk_days`` each.

        The final chunk may be shorter than ``max_chunk_days``.

        Args:
            from_ts:        Range start (inclusive), UTC-aware datetime.
            to_ts:          Range end (exclusive), UTC-aware datetime.
            max_chunk_days: Maximum number of calendar days per chunk.
                            Must be ≥ 1.

        Returns:
            List of ``(chunk_start, chunk_end)`` tuples, each covering at
            most ``max_chunk_days`` days.  The list is empty when
            ``from_ts >= to_ts``.

        Raises:
            ValueError: If ``max_chunk_days < 1``.

        Example::

            chunk_date_ranges(
                from_ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
                to_ts=datetime(2024, 2, 15, tzinfo=timezone.utc),
                max_chunk_days=30,
            )
            # → [(datetime(2024,1,1), datetime(2024,1,31)),
            #    (datetime(2024,1,31), datetime(2024,2,15))]
        """
        if max_chunk_days < 1:
            raise ValueError(
                f"max_chunk_days must be ≥ 1; got {max_chunk_days}"
            )

        chunks: list[tuple[datetime.datetime, datetime.datetime]] = []
        delta = datetime.timedelta(days=max_chunk_days)
        current = from_ts

        while current < to_ts:
            chunk_end = min(current + delta, to_ts)
            chunks.append((current, chunk_end))
            current = chunk_end

        return chunks

    # ------------------------------------------------------------------ #
    # Checkpoint helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    async def get_checkpoint(
        *,
        symbol: str,
        exchange: str,
        interval: str,
        redis_client: "AsyncRedis",
    ) -> Optional[datetime.datetime]:
        """Read the last successfully persisted candle's UTC timestamp.

        The checkpoint key has no TTL — it is persistent (Requirement 10.1).

        Args:
            symbol:       Instrument trading symbol.
            exchange:     Exchange identifier.
            interval:     Candle interval string.
            redis_client: Async Redis client.

        Returns:
            UTC-aware ``datetime`` of the last checkpoint, or ``None`` if no
            checkpoint has been recorded for this combination.
        """
        key = _CHECKPOINT_KEY_TEMPLATE.format(
            symbol=symbol, exchange=exchange, interval=interval
        )
        try:
            value = await redis_client.get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "backfill_checkpoint_read_error",
                component="historical_engine",
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                error=str(exc),
            )
            return None

        if value is None:
            return None

        # Decode bytes → str if necessary.
        if isinstance(value, bytes):
            value = value.decode("utf-8")

        try:
            return datetime.datetime.fromisoformat(value).astimezone(
                datetime.timezone.utc
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "backfill_checkpoint_parse_error",
                component="historical_engine",
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                raw_value=value,
                error=str(exc),
            )
            return None

    @staticmethod
    async def set_checkpoint(
        *,
        symbol: str,
        exchange: str,
        interval: str,
        ts: datetime.datetime,
        redis_client: "AsyncRedis",
    ) -> None:
        """Persist the UTC timestamp of the last successfully stored candle.

        The key is written with no TTL (Requirement 10.1).

        Args:
            symbol:       Instrument trading symbol.
            exchange:     Exchange identifier.
            interval:     Candle interval string.
            ts:           UTC-aware datetime of the last persisted candle.
            redis_client: Async Redis client.

        Raises:
            Logs a warning (does not raise) if the Redis write fails so that
            the calling backfill loop can continue.
        """
        key = _CHECKPOINT_KEY_TEMPLATE.format(
            symbol=symbol, exchange=exchange, interval=interval
        )
        # Always store as UTC ISO-8601 with timezone offset.
        value = ts.astimezone(datetime.timezone.utc).isoformat()
        try:
            # No expiry — the checkpoint is persistent.
            await redis_client.set(key, value)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "backfill_checkpoint_write_error",
                component="historical_engine",
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                ts=value,
                error=str(exc),
            )

    # ------------------------------------------------------------------ #
    # Bulk upsert
    # ------------------------------------------------------------------ #

    @staticmethod
    async def bulk_upsert_candles(
        candles: list[dict],
        *,
        db_engine: "AsyncEngine",
        symbol: str,
        exchange: str,
        interval: str,
        provider: str,
        normalisation_version: str = "2.0.0",
    ) -> int:
        """Bulk upsert candles into the ``candle_bar`` table.

        Uses the ON CONFLICT … DO UPDATE pattern from the design (§ Bulk
        Upsert Pattern) so idempotent re-runs do not create duplicates.

        Each row upserted maps to the canonical ``candle_bar`` schema.  The
        ``dataset_version`` is taken from each candle dict if present;
        otherwise defaults to 1.

        Args:
            candles:                List of normalised candle dicts.  Each must
                                    have at least ``time``, ``open``, ``high``,
                                    ``low``, ``close``, ``volume`` fields.
            db_engine:              Async SQLAlchemy engine.
            symbol:                 Trading symbol (used as ``instrument_id``
                                    if the candle dict lacks one).
            exchange:               Exchange identifier.
            interval:               Candle interval string.
            provider:               Provider string (stored as provenance).
            normalisation_version:  Semver string attached to each row.

        Returns:
            Number of rows successfully upserted.

        Raises:
            Logs a warning and re-raises on database errors so the caller can
            handle them (e.g., break out of the chunk loop).
        """
        if not candles:
            return 0

        from sqlalchemy import text  # local import avoids top-level dep

        # Build the list of row dicts for the upsert.
        rows = []
        for c in candles:
            instrument_id = c.get("instrumentId") or f"{exchange}:{symbol}"
            # ``time`` may be a UTC epoch seconds int, a datetime, or an
            # ISO-8601 string — normalise to a datetime.
            candle_time = _coerce_to_datetime(c.get("time"))
            if candle_time is None:
                logger.warning(
                    "bulk_upsert_skipping_invalid_time",
                    component="historical_engine",
                    instrument_id=instrument_id,
                    raw_time=c.get("time"),
                )
                continue

            session_date = candle_time.astimezone(
                datetime.timezone(datetime.timedelta(hours=5, minutes=30))
            ).date()

            rows.append(
                {
                    "instrument_id": instrument_id,
                    "exchange": exchange,
                    "interval_str": interval,
                    "time": candle_time,
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": int(c.get("volume") or 0),
                    "oi": c.get("oi"),
                    "volume_unavailable": bool(c.get("volumeUnavailable", False)),
                    "provider": provider,
                    "source_type": c.get("sourceType", "OPEN_SOURCE_NSE_DERIVED"),
                    "dataset_version": int(c.get("datasetVersion", 1)),
                    "session_date": session_date,
                    "normalisation_version": normalisation_version,
                    "poor_quality": bool(c.get("poorQuality", False)),
                }
            )

        if not rows:
            return 0

        upsert_sql = text(
            """
            INSERT INTO candle_bar (
                instrument_id, exchange, interval_str, time,
                open, high, low, close, volume, oi,
                volume_unavailable, provider, source_type,
                dataset_version, session_date, normalisation_version,
                poor_quality
            ) VALUES (
                :instrument_id, :exchange, :interval_str, :time,
                :open, :high, :low, :close, :volume, :oi,
                :volume_unavailable, :provider, :source_type,
                :dataset_version, :session_date, :normalisation_version,
                :poor_quality
            )
            ON CONFLICT (instrument_id, exchange, interval_str, time)
            DO UPDATE SET
                open                  = EXCLUDED.open,
                high                  = EXCLUDED.high,
                low                   = EXCLUDED.low,
                close                 = EXCLUDED.close,
                volume                = EXCLUDED.volume,
                oi                    = EXCLUDED.oi,
                provider              = EXCLUDED.provider,
                dataset_version       = EXCLUDED.dataset_version,
                normalisation_version = EXCLUDED.normalisation_version,
                poor_quality          = EXCLUDED.poor_quality
            """
        )

        try:
            async with db_engine.begin() as conn:
                for row in rows:
                    await conn.execute(upsert_sql, row)
            logger.info(
                "bulk_upsert_complete",
                component="historical_engine",
                exchange=exchange,
                interval=interval,
                provider=provider,
                rows=len(rows),
            )
            return len(rows)
        except Exception as exc:
            logger.error(
                "bulk_upsert_failed",
                component="historical_engine",
                exchange=exchange,
                interval=interval,
                provider=provider,
                rows=len(rows),
                error=str(exc),
            )
            raise

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _resolve_provider(
        self,
        *,
        instrument_class: str,
        interval: str,
        is_indian_market: bool,
    ) -> ProviderId:
        """Return the primary provider for a given instrument class + interval.

        Routing rules (Requirement 10.3):
          - EQ (equity intraday 1m–1h) → Angel One SmartAPI
          - IDX (index intraday)        → Upstox V3
          - FO + 1d (EOD with OI)       → Jugaad-data
          - FO + intraday               → Angel One (same as EQ)
          - Crypto                      → Binance (passed explicitly by caller)
          - Default / reconciliation    → OpenChart

        Angel One is the primary for equity *and* F&O intraday history (1m–1h).
        For F&O EOD (``interval == "1d"``) Jugaad is used.
        """
        if not is_indian_market:
            # Binance is the sole crypto provider; the caller is expected to
            # pass it explicitly.  Fall back to OpenChart as a safe default.
            return _FALLBACK_PROVIDER

        if instrument_class == "IDX":
            return ProviderId.UPSTOX

        if instrument_class == "FO" and interval == "1d":
            return ProviderId.JUGAAD_DATA

        if instrument_class in ("EQ", "FO"):
            return ProviderId.ANGEL_ONE

        # Everything else (e.g. IDX at EOD, unknown classes) → OpenChart.
        return _FALLBACK_PROVIDER

    @staticmethod
    def _get_chunk_days(*, provider: ProviderId, interval: str) -> int:
        """Return the max chunk size in calendar days for a provider × interval.

        Mirrors the logic in ``capability_matrix.get_chunk_days`` but is kept
        self-contained here to avoid a circular import.
        """
        if provider == ProviderId.ANGEL_ONE:
            return _ANGEL_ONE_CHUNK_DAYS.get(interval, 30)
        if provider == ProviderId.UPSTOX:
            return _UPSTOX_CHUNK_DAYS.get(interval, 7)
        if provider == ProviderId.JUGAAD_DATA:
            return _JUGAAD_CHUNK_DAYS
        if provider == ProviderId.OPENCHART:
            return _OPENCHART_CHUNK_DAYS
        # Default: conservative 30-day chunks for unknown providers.
        return 30

    @staticmethod
    def _validate_candles(
        candles: list[dict],
        *,
        provider: str,
        instrument_id: str,
        interval: str,
    ) -> tuple[list[dict], list[dict]]:
        """Apply OHLCV candle invariant checks to a list of raw candle dicts.

        Delegates to ``src.core.validators.ohlcv.validate_ohlcv_invariants``
        for each candle.  Invalid candles are excluded from the returned list
        and a DataIncident is generated for each.

        Args:
            candles:       Raw candle dicts from the provider.
            provider:      Provider identifier string.
            instrument_id: Canonical instrument ID.
            interval:      Candle interval string.

        Returns:
            ``(valid_candles, incidents)`` where ``valid_candles`` is the
            subset of candles that passed all invariants and ``incidents``
            is a (possibly empty) list of DataIncident dicts.
        """
        # Import here to avoid a circular import at module level.
        from src.core.validators.ohlcv import validate_ohlcv_invariants

        valid: list[dict] = []
        incidents: list[dict] = []

        for candle in candles:
            ok, incident = validate_ohlcv_invariants(
                candle=candle,
                provider=provider,
                instrument_id=instrument_id,
                interval=interval,
                is_indian_market=False,  # 3m already blocked above; pass False
                                         # to avoid double-raising inside validator.
            )
            if ok:
                valid.append(candle)
            else:
                if incident:
                    incidents.append(incident)

        return valid, incidents

    @staticmethod
    def _extract_candle_time(candle: dict) -> Optional[datetime.datetime]:
        """Extract and coerce the ``time`` field of a candle dict to UTC datetime."""
        return _coerce_to_datetime(candle.get("time"))

    async def _fetch_candles(
        self,
        *,
        provider: ProviderId,
        symbol: str,
        exchange: str,
        instrument_class: str,
        interval: str,
        from_ts: datetime.datetime,
        to_ts: datetime.datetime,
    ) -> list[dict]:
        """Fetch raw candles from the provider for the given date range.

        This is a thin dispatch stub.  In the full implementation (Tasks
        4.5–4.8) this method will forward calls to the real provider adapters
        via the ProviderGateway.  Until then it returns an empty list so that
        the backfill loop runs end-to-end without real network calls.

        The timeout wrapper in ``run_backfill`` will cancel this coroutine if
        it exceeds 30 seconds (Requirement 10.2).
        """
        # TODO(task-4.5-4.8): wire real provider adapters via ProviderGateway.
        # Stub returns empty list — zero network I/O.
        logger.debug(
            "backfill_fetch_stub",
            component="historical_engine",
            provider=provider.value,
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            from_ts=from_ts.isoformat(),
            to_ts=to_ts.isoformat(),
        )
        return []


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _coerce_to_datetime(value: Any) -> Optional[datetime.datetime]:
    """Coerce a candle time value to a UTC-aware datetime.

    Handles:
      - ``datetime`` objects (ensures UTC awareness)
      - ``int`` / ``float`` epoch seconds
      - ISO-8601 strings

    Returns ``None`` if the value cannot be parsed.
    """
    if value is None:
        return None

    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc)

    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(
                float(value), tz=datetime.timezone.utc
            )
        except (OSError, OverflowError, ValueError):
            return None

    if isinstance(value, str):
        try:
            dt = datetime.datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except (ValueError, TypeError):
            return None

    return None
