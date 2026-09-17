"""
Historical Engine — Task 7.1: Resumable Checkpointed Backfill.

The HistoricalEngine is responsible for:
  - Acquiring historical OHLCV candle data for NSE equities, indices, F&O
    instruments, and Binance crypto.
  - Splitting large date ranges into provider-safe chunks (per Capability Matrix
    chunk limits).
  - Persisting candles to the appropriate canonical table via bulk upsert:
      EQ / IDX  → equity_candle    (TimescaleDB hypertable)
      FO / FUT  → futures_candle   (TimescaleDB hypertable)
      OPT       → options_candle   (TimescaleDB hypertable)
  - NEVER writing to candle_bar — that table is an archive only.
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

Canonical table routing (Requirement: Phase 2-4 cutover):
  INSERT … ON CONFLICT DO UPDATE is used against equity_candle, futures_candle,
  or options_candle — never against candle_bar.

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
# Angel One well-known token map
# Numeric tokens for commonly traded NSE symbols.  Used as a fallback when
# the instrument_master table has not yet been populated via the sync
# endpoint (/v1/admin/instruments/sync).
# These are the official Angel One scrip token IDs for the NSE exchange.
# Reference: https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json
# ---------------------------------------------------------------------------
_ANGEL_ONE_KNOWN_TOKENS: dict[str, str] = {
    # Equity
    "RELIANCE":   "2885",
    "HDFCBANK":   "1333",
    "INFY":       "1594",
    "TCS":        "11536",
    "ICICIBANK":  "4963",
    "SBIN":       "3045",
    "HINDUNILVR": "1394",
    "AXISBANK":   "5900",
    "BAJFINANCE": "317",
    "BAJAJFINSV": "16675",
    "KOTAKBANK":  "1922",
    "LT":         "11483",
    "WIPRO":      "3787",
    "NESTLEIND":  "17963",
    "ASIANPAINT": "236",
    "MARUTI":     "10999",
    "TITAN":      "3506",
    "SUNPHARMA":  "3351",
    "ONGC":       "2475",
    "TATAMOTORS": "3432",
    "TATASTEEL":  "3499",
    "NTPC":       "11630",
    "POWERGRID":  "14977",
    "ADANIPORTS": "15083",
    "ADANIENT":   "25",
    "DIVISLAB":   "10940",
    "CIPLA":      "694",
    "DRREDDY":    "881",
    "EICHERMOT":  "910",
    "HEROMOTOCO": "1348",
    "JSWSTEEL":   "11723",
    "HINDALCO":   "1363",
    "COALINDIA":  "20374",
    "BRITANNIA":  "547",
    "BAJAJ-AUTO": "16669",
    "BPCL":       "526",
    "GRASIM":     "1232",
    "TECHM":      "13538",
    "ULTRACEMCO": "11532",
    "HCLTECH":    "7229",
    "M&M":        "2031",
    "UPL":        "11287",
    "SHREECEM":   "3103",
    "TATACONSUM": "3432",
    "INDUSINDBK": "5258",
    # Indices (NSE)
    "NIFTY":      "99926000",
    "BANKNIFTY":  "99926009",
    "FINNIFTY":   "99926037",
    "MIDCPNIFTY": "99926074",
    "SENSEX":     "99919000",
}

# ---------------------------------------------------------------------------
# Upstox instrument key map
# Upstox V2 uses two formats:
#   - Equities:  NSE_EQ|{ISIN}          (ISIN = SEBI-assigned 12-char code)
#   - Indices:   NSE_INDEX|{Index Name} (exact Upstox display name)
#
# Equities: ISINs are stable and publicly registered with SEBI/NSE.
# Indices:  Upstox uses human-readable names — must match exactly.
#
# Verified 2026-09-14 via Upstox V2 API with real access token.
# Reference: https://upstox.com/developer/api-documentation/instruments
# ---------------------------------------------------------------------------
_UPSTOX_INSTRUMENT_KEYS: dict[str, str] = {
    # Indices (NSE)
    "NIFTY":        "NSE_INDEX|Nifty 50",
    "BANKNIFTY":    "NSE_INDEX|Nifty Bank",
    "FINNIFTY":     "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY":   "NSE_INDEX|Nifty Midcap Select",
    "NIFTYNEXT50":  "NSE_INDEX|Nifty Next 50",
    "INDIAVIX":     "NSE_INDEX|India VIX",
    "NIFTYIT":      "NSE_INDEX|Nifty IT",
    "NIFTYAUTO":    "NSE_INDEX|Nifty Auto",
    "NIFTYPHARMA":  "NSE_INDEX|Nifty Pharma",
    "NIFTYFMCG":    "NSE_INDEX|Nifty FMCG",
    "NIFTYMETAL":   "NSE_INDEX|Nifty Metal",
    "NIFTYENERGY":  "NSE_INDEX|Nifty Energy",
    "NIFTYREALTY":  "NSE_INDEX|Nifty Realty",
    "NIFTYPSUBANK": "NSE_INDEX|Nifty PSU Bank",
    # Equities — NSE_EQ|{ISIN}
    "RELIANCE":     "NSE_EQ|INE002A01018",
    "HDFCBANK":     "NSE_EQ|INE040A01034",
    "INFY":         "NSE_EQ|INE009A01021",
    "TCS":          "NSE_EQ|INE467B01029",
    "ICICIBANK":    "NSE_EQ|INE090A01021",
    "SBIN":         "NSE_EQ|INE062A01020",
    "HINDUNILVR":   "NSE_EQ|INE030A01027",
    "AXISBANK":     "NSE_EQ|INE238A01034",
    "BAJFINANCE":   "NSE_EQ|INE296A01024",
    "BAJAJFINSV":   "NSE_EQ|INE918I01026",
    "KOTAKBANK":    "NSE_EQ|INE237A01028",
    "LT":           "NSE_EQ|INE018A01030",
    "WIPRO":        "NSE_EQ|INE075A01022",
    "NESTLEIND":    "NSE_EQ|INE239A01016",
    "ASIANPAINT":   "NSE_EQ|INE021A01026",
    "MARUTI":       "NSE_EQ|INE585B01010",
    "TITAN":        "NSE_EQ|INE280A01028",
    "SUNPHARMA":    "NSE_EQ|INE044A01036",
    "ONGC":         "NSE_EQ|INE213A01029",
    "TATAMOTORS":   "NSE_EQ|INE155A01022",
    "TATASTEEL":    "NSE_EQ|INE081A01020",
    "NTPC":         "NSE_EQ|INE733E01010",
    "POWERGRID":    "NSE_EQ|INE752E01010",
    "ADANIPORTS":   "NSE_EQ|INE742F01042",
    "ADANIENT":     "NSE_EQ|INE423A01024",
    "DIVISLAB":     "NSE_EQ|INE361B01024",
    "CIPLA":        "NSE_EQ|INE059A01026",
    "DRREDDY":      "NSE_EQ|INE089A01031",
    "EICHERMOT":    "NSE_EQ|INE066A01021",
    "HEROMOTOCO":   "NSE_EQ|INE158A01026",
    "JSWSTEEL":     "NSE_EQ|INE019A01038",
    "HINDALCO":     "NSE_EQ|INE038A01020",
    "COALINDIA":    "NSE_EQ|INE522F01014",
    "BRITANNIA":    "NSE_EQ|INE216A01030",
    "BAJAJ-AUTO":   "NSE_EQ|INE917I01010",
    "BPCL":         "NSE_EQ|INE029A01011",
    "GRASIM":       "NSE_EQ|INE047A01021",
    "TECHM":        "NSE_EQ|INE669C01036",
    "ULTRACEMCO":   "NSE_EQ|INE481G01011",
    "HCLTECH":      "NSE_EQ|INE860A01027",
    "UPL":          "NSE_EQ|INE628A01036",
    "INDUSINDBK":   "NSE_EQ|INE095A01012",
    "BHARTIARTL":   "NSE_EQ|INE397D01024",
    "M&M":          "NSE_EQ|INE101A01026",
    "TATACONSUM":   "NSE_EQ|INE192A01025",
    "SHREECEM":     "NSE_EQ|INE070A01015",
}

# All intervals supported by the Upstox V3 historical candle API.
# V3 supports all canonical intervals on all plans — the old V2 basic-plan
# restriction (UDAPI1020 on 5m/10m/15m/1h) no longer applies.
_UPSTOX_V3_SUPPORTED_INTERVALS: frozenset[str] = frozenset(
    {"1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"}
)
# Backward-compatible alias kept so any external code referencing the old name
# still compiles, but it now points to the full V3 set.
_UPSTOX_V2_SUPPORTED_INTERVALS: frozenset[str] = _UPSTOX_V3_SUPPORTED_INTERVALS

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

        # Optional pre-authenticated provider adapters.  When set, _fetch_candles
        # reuses the shared instance instead of constructing + authenticating a
        # fresh one per chunk — critical for bulk backfills where repeated TOTP
        # logins trigger Angel One's rate limiter (HTTP 403).
        # Injected by the backfill script or the server lifespan after auth.
        self._angel_one_adapter: Optional["AngelOneAdapter"] = None  # type: ignore[name-defined]  # noqa: F821
        self._upstox_adapter: Optional["UpstoxAdapter"] = None  # type: ignore[name-defined]  # noqa: F821

        # DB engine reference — stored here so _fetch_candles can resolve
        # Angel One tokens from instrument_master for F&O symbols.
        # Set by run_backfill() when db_engine is provided.
        self._db_engine: Optional["AsyncEngine"] = None

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

        # Store db_engine on self so _fetch_candles can do token lookups
        self._db_engine = db_engine

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
                # Fetch candles from provider.
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
                # Track the actual provider that produced the data (may differ
                # from the primary when a fallback is used).
                actual_provider = provider
                # Fallback: if primary returns nothing for EQ/IDX 1d, try Yahoo Finance
                if not candles and interval == "1d" \
                        and instrument_class in ("EQ", "IDX", "EQ_IDX") \
                        and provider != ProviderId.YAHOO_FINANCE:
                    logger.info(
                        "backfill_fallback_to_yahoo",
                        component="historical_engine",
                        symbol=symbol,
                        primary_provider=provider.value,
                    )
                    candles = await asyncio.wait_for(
                        self._fetch_candles(
                            provider=ProviderId.YAHOO_FINANCE,
                            symbol=symbol,
                            exchange=exchange,
                            instrument_class=instrument_class,
                            interval=interval,
                            from_ts=chunk_start,
                            to_ts=chunk_end,
                        ),
                        timeout=_PROVIDER_TIMEOUT_SEC,
                    )
                    if candles:
                        actual_provider = ProviderId.YAHOO_FINANCE

                # Validate each candle against OHLCV invariants.
                valid_candles, chunk_incidents = self._validate_candles(
                    candles=candles,
                    provider=actual_provider.value,
                    instrument_id=instrument_id,
                    interval=interval,
                )
                incidents.extend(chunk_incidents)

                # Bulk upsert valid candles to the canonical table.
                if valid_candles:
                    await self.bulk_upsert_candles(
                        candles=valid_candles,
                        db_engine=db_engine,
                        symbol=symbol,
                        exchange=exchange,
                        interval=interval,
                        provider=actual_provider.value,
                        instrument_class=instrument_class,
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


    @staticmethod
    async def clear_checkpoint(
        *,
        symbol: str,
        exchange: str,
        interval: str,
        redis_client: "AsyncRedis",
    ) -> None:
        """Delete the Redis checkpoint for a symbol/exchange/interval tuple.

        Used by the ``force=True`` backfill path to ensure the full requested
        date range is re-fetched, bypassing any checkpoint that would otherwise
        skip historical dates earlier than the last persisted candle.

        Safe to call when no checkpoint exists (Redis DEL on missing key is
        a no-op).

        Args:
            symbol:       Instrument trading symbol.
            exchange:     Exchange identifier.
            interval:     Candle interval string.
            redis_client: Async Redis client.
        """
        key = _CHECKPOINT_KEY_TEMPLATE.format(
            symbol=symbol, exchange=exchange, interval=interval
        )
        try:
            await redis_client.delete(key)
            logger.info(
                "backfill_checkpoint_cleared",
                component="historical_engine",
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                key=key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "backfill_checkpoint_clear_error",
                component="historical_engine",
                symbol=symbol,
                exchange=exchange,
                interval=interval,
                error=str(exc),
            )

    # ------------------------------------------------------------------ #
    # Bulk upsert
    # ------------------------------------------------------------------ #

    @staticmethod
    def _canonical_table_for(
        instrument_class: str,
        exchange: str,
    ) -> str:
        """Return the canonical write table name for the given instrument class.

        Routing (non-negotiable — candle_bar is archive-only):
          EQ / IDX / ETF (NSE/BSE cash)  → equity_candle
          FO / FUT                        → futures_candle
          OPT / OPTIDX / OPTSTK          → options_candle

        Crypto (BINANCE, DELTA) is NOT routed here — those writers
        (BinancePersistenceLayer, DeltaPersistenceLayer) manage their own
        persistence to candle_bar until a crypto_candle table is created.

        Args:
            instrument_class:  One of "EQ", "IDX", "ETF", "FO", "FUT",
                               "OPT", "OPTIDX", "OPTSTK", or exchange-derived.
            exchange:          Exchange string — used as a secondary signal
                               when instrument_class is ambiguous.

        Returns:
            Table name string: "equity_candle" | "futures_candle" | "options_candle"
        """
        cls = instrument_class.upper().strip()
        exch = exchange.upper().strip()

        # Options — check before FO because OPTIDX/OPTSTK are sub-types of FO
        if cls in ("OPT", "OPTIDX", "OPTSTK"):
            return "options_candle"

        # Futures
        if cls in ("FO", "FUT", "FUTSTK", "FUTIDX"):
            return "futures_candle"

        # Equities + Indices + ETFs
        if cls in ("EQ", "IDX", "ETF", "EQ_IDX"):
            return "equity_candle"

        # Exchange-derived fallback: NFO/BFO without explicit class → futures
        if exch in ("NFO", "BFO", "MCX"):
            return "futures_candle"

        # Default: equity_candle for unknown Indian-market classes
        logger.warning(
            "canonical_table_unknown_class",
            component="historical_engine",
            instrument_class=instrument_class,
            exchange=exchange,
            fallback="equity_candle",
        )
        return "equity_candle"

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
        instrument_class: str = "EQ",
    ) -> int:
        """Bulk upsert candles into the appropriate canonical table.

        Routing (candle_bar is archive-only — never written by this method):
          EQ / IDX / ETF  → equity_candle       (TimescaleDB hypertable)
          FO / FUT        → futures_candle       (TimescaleDB hypertable)
          OPT             → options_candle       (TimescaleDB hypertable)

        Uses the ON CONFLICT … DO UPDATE pattern so idempotent re-runs do
        not create duplicates.

        Args:
            candles:                List of normalised candle dicts.
            db_engine:              Async SQLAlchemy engine.
            symbol:                 Trading symbol (used as ``instrument_id``
                                    if the candle dict lacks one).
            exchange:               Exchange identifier.
            interval:               Candle interval string.
            provider:               Provider string (stored as provenance).
            normalisation_version:  Semver string attached to each row.
            instrument_class:       Used to select the canonical target table.
                                    Defaults to "EQ" (equity_candle).

        Returns:
            Number of rows successfully upserted.

        Raises:
            Logs a warning and re-raises on database errors so the caller can
            handle them (e.g., break out of the chunk loop).
        """
        if not candles:
            return 0

        from sqlalchemy import text  # local import avoids top-level dep

        # Determine the canonical target table for this instrument class.
        target_table = HistoricalEngine._canonical_table_for(
            instrument_class=instrument_class,
            exchange=exchange,
        )

        # Segment label (EQ | IDX | ETF) used only by equity_candle.
        segment = "IDX" if instrument_class.upper() in ("IDX",) else "EQ"

        # For F&O tables, resolve expiry and underlying_id from instrument_master.
        # The Angel One API OHLCV response does not include expiry in each bar;
        # it must be looked up once per instrument and injected into every row.
        _fno_expiry: "datetime.date | None" = None
        _fno_underlying: "str | None" = None
        if target_table in ("futures_candle", "options_candle"):
            _canonical_id = f"{exchange}:{symbol}"
            try:
                async with db_engine.connect() as _conn:
                    _im_row = (await _conn.execute(
                        text(
                            "SELECT expiry, underlying FROM instrument_master "
                            "WHERE instrument_id = :iid LIMIT 1"
                        ),
                        {"iid": _canonical_id},
                    )).mappings().first()
                    if _im_row:
                        _fno_expiry = _im_row.get("expiry")
                        _underlying = _im_row.get("underlying")
                        if _underlying:
                            # underlying in instrument_master is bare symbol (e.g. "NIFTY")
                            # Qualify it with exchange prefix for canonical ID
                            _fno_underlying = f"NSE:{_underlying}" if ":" not in _underlying else _underlying
            except Exception as _exc:  # noqa: BLE001
                logger.warning(
                    "fno_expiry_lookup_failed",
                    component="historical_engine",
                    symbol=symbol,
                    error=str(_exc),
                )


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
                    "oi": c.get("oi"),          # used by futures/options only
                    "volume_unavailable": bool(c.get("volumeUnavailable", False)),
                    "provider": provider,
                    "source_type": c.get("sourceType", "OPEN_SOURCE_NSE_DERIVED"),
                    "dataset_version": int(c.get("datasetVersion", 1)),
                    "session_date": session_date,
                    "normalisation_version": normalisation_version,
                    "poor_quality": bool(c.get("poorQuality", False)),
                    # equity_candle-specific
                    "segment": segment,
                    # futures/options-specific (None for equity)
                    "expiry": c.get("expiry") or _fno_expiry,
                    "strike": c.get("strike"),
                    "option_type": c.get("optionType"),
                    # provenance fields — populated when available
                    "underlying_id": c.get("underlyingId") or _fno_underlying,
                    "source_timestamp": _coerce_to_datetime(c.get("sourceTimestamp")) if c.get("sourceTimestamp") else None,
                }
            )

        if not rows:
            return 0

        # ── equity_candle upsert ───────────────────────────────────────────
        if target_table == "equity_candle":
            upsert_sql = text(
                """
                INSERT INTO equity_candle (
                    instrument_id, exchange, segment, interval_str, time,
                    open, high, low, close, volume,
                    volume_unavailable, provider, source_type,
                    dataset_version, session_date, normalisation_version,
                    poor_quality, data_origin, quality_status,
                    source_timestamp
                ) VALUES (
                    :instrument_id, :exchange, :segment, :interval_str, :time,
                    :open, :high, :low, :close, :volume,
                    :volume_unavailable, :provider, :source_type,
                    :dataset_version, :session_date, :normalisation_version,
                    :poor_quality, 'PROVIDER',
                    CASE WHEN :poor_quality THEN 'POOR_QUALITY' ELSE 'TRUSTED' END,
                    :source_timestamp
                )
                ON CONFLICT (instrument_id, exchange, interval_str, time)
                DO UPDATE SET
                    open                  = EXCLUDED.open,
                    high                  = EXCLUDED.high,
                    low                   = EXCLUDED.low,
                    close                 = EXCLUDED.close,
                    volume                = EXCLUDED.volume,
                    provider              = EXCLUDED.provider,
                    dataset_version       = EXCLUDED.dataset_version,
                    normalisation_version = EXCLUDED.normalisation_version,
                    poor_quality          = EXCLUDED.poor_quality,
                    quality_status        = EXCLUDED.quality_status,
                    source_timestamp      = COALESCE(EXCLUDED.source_timestamp, equity_candle.source_timestamp)
                """
            )
        # ── futures_candle upsert ─────────────────────────────────────────
        elif target_table == "futures_candle":
            upsert_sql = text(
                """
                INSERT INTO futures_candle (
                    instrument_id, exchange, interval_str, time,
                    open, high, low, close, volume, open_interest,
                    provider, source_type,
                    dataset_version, session_date, normalisation_version,
                    poor_quality, data_origin, quality_status,
                    expiry, underlying_id, source_timestamp
                ) VALUES (
                    :instrument_id, :exchange, :interval_str, :time,
                    :open, :high, :low, :close, :volume, :oi,
                    :provider, :source_type,
                    :dataset_version, :session_date, :normalisation_version,
                    :poor_quality, 'PROVIDER',
                    CASE WHEN :poor_quality THEN 'POOR_QUALITY' ELSE 'TRUSTED' END,
                    :expiry, :underlying_id, :source_timestamp
                )
                ON CONFLICT (instrument_id, exchange, interval_str, time)
                DO UPDATE SET
                    open                  = EXCLUDED.open,
                    high                  = EXCLUDED.high,
                    low                   = EXCLUDED.low,
                    close                 = EXCLUDED.close,
                    volume                = EXCLUDED.volume,
                    open_interest         = EXCLUDED.open_interest,
                    provider              = EXCLUDED.provider,
                    dataset_version       = EXCLUDED.dataset_version,
                    normalisation_version = EXCLUDED.normalisation_version,
                    poor_quality          = EXCLUDED.poor_quality,
                    quality_status        = EXCLUDED.quality_status,
                    underlying_id         = COALESCE(EXCLUDED.underlying_id, futures_candle.underlying_id),
                    source_timestamp      = COALESCE(EXCLUDED.source_timestamp, futures_candle.source_timestamp)
                """
            )
        # ── options_candle upsert ─────────────────────────────────────────
        else:  # options_candle
            upsert_sql = text(
                """
                INSERT INTO options_candle (
                    instrument_id, exchange, interval_str, time,
                    open, high, low, close, volume, open_interest,
                    provider, source_type,
                    dataset_version, session_date, normalisation_version,
                    poor_quality, data_origin, quality_status,
                    expiry, strike, option_type, underlying_id, source_timestamp
                ) VALUES (
                    :instrument_id, :exchange, :interval_str, :time,
                    :open, :high, :low, :close, :volume, :oi,
                    :provider, :source_type,
                    :dataset_version, :session_date, :normalisation_version,
                    :poor_quality, 'PROVIDER',
                    CASE WHEN :poor_quality THEN 'POOR_QUALITY' ELSE 'TRUSTED' END,
                    :expiry, :strike, :option_type, :underlying_id, :source_timestamp
                )
                ON CONFLICT (instrument_id, exchange, interval_str, time)
                DO UPDATE SET
                    open                  = EXCLUDED.open,
                    high                  = EXCLUDED.high,
                    low                   = EXCLUDED.low,
                    close                 = EXCLUDED.close,
                    volume                = EXCLUDED.volume,
                    open_interest         = EXCLUDED.open_interest,
                    provider              = EXCLUDED.provider,
                    dataset_version       = EXCLUDED.dataset_version,
                    normalisation_version = EXCLUDED.normalisation_version,
                    poor_quality          = EXCLUDED.poor_quality,
                    quality_status        = EXCLUDED.quality_status,
                    underlying_id         = COALESCE(EXCLUDED.underlying_id, options_candle.underlying_id),
                    source_timestamp      = COALESCE(EXCLUDED.source_timestamp, options_candle.source_timestamp)
                """
            )

        try:
            async with db_engine.begin() as conn:
                for row in rows:
                    await conn.execute(upsert_sql, row)
            logger.info(
                "bulk_upsert_complete",
                component="historical_engine",
                target_table=target_table,
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
                target_table=target_table,
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
            # Upstox is the designated IDX intraday provider, but requires
            # OAuth credentials not yet configured.  Angel One covers NSE
            # indices (NIFTY, BANKNIFTY, etc.) via its own token IDs and is
            # available when authenticated — prefer it as the practical primary.
            from src.core.settings import get_settings  # noqa: PLC0415
            settings = get_settings()
            angel_available = bool(settings.angel_one_api_key and settings.angel_one_mpin)
            upstox_available = bool(settings.upstox_access_token or settings.upstox_analytics_key)
            if upstox_available and interval in _UPSTOX_V3_SUPPORTED_INTERVALS:
                return ProviderId.UPSTOX
            if angel_available:
                return ProviderId.ANGEL_ONE
            return ProviderId.UPSTOX

        if instrument_class == "FO" and interval == "1d":
            # Jugaad-data F&O bhavcopy is broken for dates after 2024-07-08
            # (NSE changed the format). Use Angel One for F&O EOD instead.
            from src.core.settings import get_settings  # noqa: PLC0415
            settings = get_settings()
            if settings.angel_one_api_key and settings.angel_one_mpin:
                return ProviderId.ANGEL_ONE
            # Upstox as secondary option for F&O EOD
            if settings.upstox_access_token:
                return ProviderId.UPSTOX
            return ProviderId.JUGAAD_DATA

        if instrument_class in ("EQ", "FO"):
            # Angel One is primary for intraday (1m–1h).
            # For EOD (1d, 1w, 1M) Upstox V3 is equally good — use it when
            # an access token OR the long-lived analytics token is available,
            # as V3 returns full ISIN-keyed data with OI at index 6.
            # If Upstox not configured, fall back to Jugaad (stock_df) for EQ 1d.
            from src.core.settings import get_settings  # noqa: PLC0415
            settings = get_settings()
            upstox_available = bool(settings.upstox_access_token or settings.upstox_analytics_key)
            if interval in ("1d", "1w", "1M") and upstox_available:
                return ProviderId.UPSTOX
            if interval in ("1d",) and instrument_class == "EQ":
                # Jugaad (stock_df) works for EQ 1d when Upstox not configured
                return ProviderId.JUGAAD_DATA
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
        from_date = from_ts.date()
        to_date = to_ts.date()
        from_str = from_ts.strftime("%Y-%m-%d %H:%M")
        to_str = to_ts.strftime("%Y-%m-%d %H:%M")

        try:
            if provider == ProviderId.ANGEL_ONE:
                from src.providers.adapters.angel_one import AngelOneAdapter  # noqa: PLC0415
                from src.core.settings import get_settings  # noqa: PLC0415
                settings = get_settings()
                if not (settings.angel_one_api_key and settings.angel_one_client_id
                        and settings.angel_one_totp_secret):
                    logger.debug("angel_one_not_configured", component="historical_engine")
                    return []

                # Resolve numeric Angel One token.
                # Priority:
                #   1) instrument_provider_mapping table (covers F&O and EQ)
                #   2) instrument_master table (EQ/IDX fallback)
                #   3) well-known static token map
                angel_token: str = symbol  # fallback: plain symbol (may fail)
                base_symbol = symbol.split(":")[1] if ":" in symbol else symbol
                instrument_id = f"{exchange}:{base_symbol}"

                # Priority 1: Look up from instrument_provider_mapping (correct for F&O)
                # This table has provider_instrument_id = Angel One numeric token
                # keyed by canonical instrument_id + provider = "angel_one"
                if self._db_engine is not None:
                    from sqlalchemy import text as _text  # noqa: PLC0415
                    try:
                        async with self._db_engine.connect() as _conn:
                            # First try instrument_provider_mapping — covers F&O contracts
                            _row = (await _conn.execute(
                                _text("""
                                    SELECT provider_instrument_id
                                    FROM instrument_provider_mapping
                                    WHERE instrument_id = :iid
                                      AND provider = 'angel_one'
                                    LIMIT 1
                                """),
                                {"iid": instrument_id},
                            )).mappings().first()
                            if _row and _row.get("provider_instrument_id"):
                                angel_token = _row["provider_instrument_id"]
                                logger.debug(
                                    "angel_one_token_resolved_from_mapping",
                                    component="historical_engine",
                                    symbol=symbol,
                                    instrument_id=instrument_id,
                                    token=angel_token,
                                )
                            else:
                                # Fallback: instrument_master.angel_token (EQ/IDX)
                                _row2 = (await _conn.execute(
                                    _text("SELECT angel_token FROM instrument_master WHERE instrument_id=:iid OR (trading_symbol=:sym AND exchange=:exch) LIMIT 1"),
                                    {"iid": instrument_id, "sym": base_symbol, "exch": exchange},
                                )).mappings().first()
                                if _row2 and _row2.get("angel_token"):
                                    angel_token = _row2["angel_token"]
                                    logger.debug(
                                        "angel_one_token_resolved_from_master",
                                        component="historical_engine",
                                        symbol=symbol,
                                        token=angel_token,
                                    )
                    except Exception as _exc:  # noqa: BLE001
                        logger.debug(
                            "angel_one_token_db_lookup_failed",
                            component="historical_engine",
                            symbol=symbol,
                            error=str(_exc),
                        )

                # Priority 2: Fall back to well-known token map
                if angel_token == symbol and base_symbol in _ANGEL_ONE_KNOWN_TOKENS:
                    angel_token = _ANGEL_ONE_KNOWN_TOKENS[base_symbol]
                    logger.debug(
                        "angel_one_token_resolved_from_map",
                        component="historical_engine",
                        symbol=symbol,
                        token=angel_token,
                    )
                elif angel_token == symbol:
                    logger.warning(
                        "angel_one_token_unknown",
                        component="historical_engine",
                        symbol=symbol,
                        hint="Token not in instrument_provider_mapping, instrument_master, or _ANGEL_ONE_KNOWN_TOKENS. Run /v1/admin/instruments/sync to populate.",
                    )

                # Reuse a pre-authenticated shared adapter when available
                # (avoids repeated TOTP logins that trigger HTTP 403 under load).
                _owns_adapter = False
                if self._angel_one_adapter is not None:
                    adapter = self._angel_one_adapter
                    await adapter.ensure_authenticated()
                else:
                    adapter = AngelOneAdapter(
                        api_key=settings.angel_one_api_key,
                        client_id=settings.angel_one_client_id,
                        totp_secret=settings.angel_one_totp_secret,
                        mpin=settings.angel_one_mpin,
                    )
                    await adapter.ensure_authenticated()
                    _owns_adapter = True

                candles = await adapter.fetch_historical_ohlcv(
                    symbol=symbol, token=angel_token, from_date=from_str,
                    to_date=to_str, interval=interval, exchange=exchange,
                )
                if _owns_adapter:
                    await adapter.close()

                # If Angel One returns empty and this is an EQ/IDX 1d request,
                # fall back to Yahoo Finance so the backfill still succeeds.
                if not candles and interval == "1d":
                    logger.info(
                        "angel_one_empty_fallback_yahoo",
                        component="historical_engine",
                        symbol=symbol, interval=interval,
                    )
                    from src.providers.adapters.yahoo_finance import YahooFinanceAdapter  # noqa: PLC0415
                    async with YahooFinanceAdapter() as yf:
                        yf_type = "IDX" if instrument_class == "IDX" else "EQ"
                        candles = await yf.fetch_historical_ohlcv(
                            symbol=base_symbol,
                            exchange=exchange,
                            from_date=from_date.date() if hasattr(from_date, "date") else from_date,
                            to_date=to_date.date() if hasattr(to_date, "date") else to_date,
                            instrument_type=yf_type,
                        )

                return candles

            elif provider == ProviderId.UPSTOX:
                from src.providers.adapters.upstox import UpstoxAdapter  # noqa: PLC0415
                from src.core.settings import get_settings  # noqa: PLC0415
                settings = get_settings()
                if not (settings.upstox_access_token or settings.upstox_analytics_key):
                    logger.debug("upstox_not_configured", component="historical_engine")
                    return []

                # Resolve Upstox instrument key.
                # Priority:
                #   1) instrument_provider_mapping (covers F&O with correct ISIN/key)
                #   2) _UPSTOX_INSTRUMENT_KEYS static map (EQ/IDX)
                base_symbol = symbol.split(":")[1] if ":" in symbol else symbol
                instrument_id_key = f"{exchange}:{base_symbol}"
                upstox_key: Optional[str] = None

                # Priority 1: DB lookup from instrument_provider_mapping
                if self._db_engine is not None:
                    from sqlalchemy import text as _text  # noqa: PLC0415
                    try:
                        async with self._db_engine.connect() as _conn:
                            _row = (await _conn.execute(
                                _text("""
                                    SELECT provider_instrument_id
                                    FROM instrument_provider_mapping
                                    WHERE instrument_id = :iid
                                      AND provider = 'upstox'
                                    LIMIT 1
                                """),
                                {"iid": instrument_id_key},
                            )).mappings().first()
                            if _row and _row.get("provider_instrument_id"):
                                upstox_key = _row["provider_instrument_id"]
                                logger.debug(
                                    "upstox_key_resolved_from_mapping",
                                    component="historical_engine",
                                    symbol=symbol,
                                    upstox_key=upstox_key,
                                )
                    except Exception as _exc:  # noqa: BLE001
                        logger.debug(
                            "upstox_key_db_lookup_failed",
                            component="historical_engine",
                            symbol=symbol,
                            error=str(_exc),
                        )

                # Priority 2: static map
                if upstox_key is None:
                    upstox_key = _UPSTOX_INSTRUMENT_KEYS.get(base_symbol)

                if upstox_key is None:
                    logger.warning(
                        "upstox_instrument_key_unknown",
                        component="historical_engine",
                        symbol=symbol,
                        hint="Add ISIN-based key to instrument_provider_mapping or _UPSTOX_INSTRUMENT_KEYS",
                    )
                    return []

                # Check if this interval is supported on V3
                if interval not in _UPSTOX_V3_SUPPORTED_INTERVALS:
                    logger.warning(
                        "upstox_interval_not_supported",
                        component="historical_engine",
                        symbol=symbol,
                        interval=interval,
                        supported=sorted(_UPSTOX_V3_SUPPORTED_INTERVALS),
                        hint="Interval not in Upstox V3 supported set",
                    )
                    return []

                # Reuse injected adapter when available to avoid
                # rebuilding the OAuth session on every chunk.
                _owns_upstox = False
                if self._upstox_adapter is not None:
                    upstox_adapter = self._upstox_adapter
                else:
                    upstox_adapter = UpstoxAdapter(
                        api_key=settings.upstox_api_key or "",
                        api_secret=settings.upstox_api_secret or "",
                        redirect_uri=settings.upstox_redirect_uri or "http://localhost:8200/v1/auth/upstox/callback",
                    )
                    # Set whichever token is available:
                    # analytics_key works for all historical/quote calls
                    # access_token needed for intraday and WS calls
                    if settings.upstox_analytics_key:
                        await upstox_adapter.set_analytics_token(settings.upstox_analytics_key)
                    if settings.upstox_access_token:
                        await upstox_adapter.set_access_token(settings.upstox_access_token)
                    _owns_upstox = True

                logger.debug(
                    "upstox_key_resolved",
                    component="historical_engine",
                    symbol=symbol,
                    instrument_key=upstox_key,
                    interval=interval,
                )
                # Upstox expects YYYY-MM-DD strings
                from_str_date = from_ts.strftime("%Y-%m-%d")
                to_str_date   = to_ts.strftime("%Y-%m-%d")
                candles = await upstox_adapter.fetch_historical_ohlcv(
                    instrument_key=upstox_key,
                    from_date=from_str_date,
                    to_date=to_str_date,
                    interval=interval,
                )
                if _owns_upstox:
                    await upstox_adapter.aclose()

                # Upstox adapter now returns dicts with key "timestamp" (not "time").
                # The canonical engine key is "time", so we normalise here.
                # We also handle the legacy list/tuple format for any older code paths.
                normalized = []
                for raw in candles:
                    if isinstance(raw, (list, tuple)) and len(raw) >= 6:
                        normalized.append({
                            "time":       raw[0],   # ISO-8601 string e.g. "2024-09-02T15:29:00+05:30"
                            "open":       raw[1],
                            "high":       raw[2],
                            "low":        raw[3],
                            "close":      raw[4],
                            "volume":     raw[5],
                            "oi":         raw[6] if len(raw) > 6 else None,
                            "provider":   "upstox",
                            "sourceType": "BROKER_AUTHENTICATED",
                        })
                    elif isinstance(raw, dict):
                        # BUG FIX: Upstox adapter returns "timestamp" key; engine expects "time".
                        # Remap "timestamp" → "time" and "open_interest" → "oi" so
                        # bulk_upsert_candles can parse the candle time correctly.
                        if "timestamp" in raw and "time" not in raw:
                            raw = dict(raw)  # shallow copy to avoid mutating the adapter output
                            raw["time"] = raw.pop("timestamp")
                        if "open_interest" in raw and "oi" not in raw:
                            raw = dict(raw) if not isinstance(raw, dict) else raw
                            raw["oi"] = raw.get("open_interest")
                        normalized.append(raw)
                    else:
                        logger.warning(
                            "upstox_unexpected_candle_shape",
                            component="historical_engine",
                            symbol=symbol,
                            raw=str(raw)[:100],
                        )
                return normalized

            elif provider == ProviderId.JUGAAD_DATA:
                from src.providers.adapters.jugaad_data import JugaadDataAdapter  # noqa: PLC0415
                async with JugaadDataAdapter() as adapter:
                    if instrument_class == "IDX":
                        # indices: use index_df path
                        base_sym = symbol.split(":")[1] if ":" in symbol else symbol
                        return await adapter.fetch_idx_eod(
                            symbol=base_sym,
                            from_date=from_date.date() if hasattr(from_date, "date") else from_date,
                            to_date=to_date.date() if hasattr(to_date, "date") else to_date,
                            interval=interval,
                        )
                    elif instrument_class in ("EQ",):
                        # equities: use stock_df path
                        base_sym = symbol.split(":")[1] if ":" in symbol else symbol
                        return await adapter.fetch_eq_eod(
                            symbol=base_sym,
                            from_date=from_date.date() if hasattr(from_date, "date") else from_date,
                            to_date=to_date.date() if hasattr(to_date, "date") else to_date,
                            interval=interval,
                        )
                    else:
                        # F&O: use fo_eod (works for dates < 2024-07-08)
                        base_sym = symbol.split(":")[1] if ":" in symbol else symbol
                        return await adapter.fetch_fo_eod(
                            symbol=base_sym,
                            from_date=from_date.date() if hasattr(from_date, "date") else from_date,
                            to_date=to_date.date() if hasattr(to_date, "date") else to_date,
                            interval=interval,
                        )

            elif provider == ProviderId.OPENCHART:
                from src.providers.adapters.openchart import OpenChartAdapter  # noqa: PLC0415
                async with OpenChartAdapter() as adapter:
                    return await adapter.fetch_historical_ohlcv(
                        symbol=symbol, exchange=exchange,
                        from_date=from_date.date() if hasattr(from_date, "date") else from_date,
                        to_date=to_date.date() if hasattr(to_date, "date") else to_date,
                        interval=interval,
                    )

            elif provider == ProviderId.YAHOO_FINANCE:
                from src.providers.adapters.yahoo_finance import YahooFinanceAdapter  # noqa: PLC0415
                async with YahooFinanceAdapter() as adapter:
                    # Yahoo supports 1d for equities and indices.
                    # Adapter expects date objects, engine provides datetimes — convert.
                    yf_instrument_type = "IDX" if instrument_class == "IDX" else "EQ"
                    return await adapter.fetch_historical_ohlcv(
                        symbol=symbol,
                        exchange=exchange,
                        from_date=from_date.date() if hasattr(from_date, "date") else from_date,
                        to_date=to_date.date() if hasattr(to_date, "date") else to_date,
                        instrument_type=yf_instrument_type,
                    )

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "historical_fetch_failed",
                component="historical_engine",
                provider=provider.value if hasattr(provider, "value") else str(provider),
                symbol=symbol, interval=interval, error=str(exc),
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
