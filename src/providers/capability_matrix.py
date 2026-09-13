"""
Capability Matrix for DATA-SERVICE 2.0.

This module is the single source of truth for what each provider can serve:
which data types, which instrument classes, which intervals, and at what
chunk granularity and rate-limit budget.

Design constraints encoded here
---------------------------------
* The ``3m`` interval is **permanently unsupported** for any Indian market
  instrument class (EQ, FO, IDX).  Only crypto instrument classes may carry
  ``3m`` in their ``intervalSupport`` list (Binance natively supports it).
* Provider routing priority: lower ``priority`` value = higher priority.
* ``maxChunkDays = 0`` means the concept of "chunk days" is not applicable
  (e.g. live-only or streaming-only capabilities).

Requirements: 5.1, 10.3, 10.4
"""

from __future__ import annotations

from typing import Optional

from src.core.schemas.provider import (
    CANONICAL_INDIAN_TIMEFRAMES,
    CRYPTO_INSTRUMENT_CLASSES,
    DataType,
    ProviderId,
    ProviderCapability,
    SourceType,
)

# ---------------------------------------------------------------------------
# 3m guard helper
# ---------------------------------------------------------------------------

_BLOCKED_INDIAN_INTERVAL = "3m"


def _indian_intervals(*extra_intervals: str) -> list[str]:
    """Return the canonical Indian timeframe list, optionally augmented.

    ``3m`` is always excluded from any Indian market interval list.  This
    function is a single choke-point so the block is enforced at definition
    time rather than relying on callers to filter.
    """
    base = list(CANONICAL_INDIAN_TIMEFRAMES)
    for interval in extra_intervals:
        if interval == _BLOCKED_INDIAN_INTERVAL:
            raise ValueError(
                "3m interval is permanently unsupported for Indian market data "
                "and must never appear in an Indian market ProviderCapability."
            )
        if interval not in base:
            base.append(interval)
    return base


# ---------------------------------------------------------------------------
# Full Capability Matrix definition
# ---------------------------------------------------------------------------
#
# Each row is one ProviderCapability record.  Multiple rows for the same
# provider are expected (different data types / instrument classes).
#
# Ordering within the list is NOT significant — callers use get_providers_for()
# which sorts by `priority`.

_MATRIX: list[ProviderCapability] = [

    # =========================================================================
    # Angel One SmartAPI
    # Authenticated (TOTP + JWT).
    # Primary for: multi-day intraday equity/F&O history (1m–1h).
    # Rate limit: 3 req/s (NSE proxy constraint).
    # NOT the primary for index tokens — use Upstox for IDX intraday.
    # =========================================================================

    # Live quotes — equity and F&O
    ProviderCapability(
        provider=ProviderId.ANGEL_ONE,
        dataType=DataType.LIVE_QUOTE,
        instrumentClass="EQ",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=3.0,
        intervalSupport=[],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=1,
    ),
    ProviderCapability(
        provider=ProviderId.ANGEL_ONE,
        dataType=DataType.LIVE_QUOTE,
        instrumentClass="FO",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=3.0,
        intervalSupport=[],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=1,
    ),

    # Historical OHLCV — equity: primary source for 1m–1h
    # Angel One chunk limits per the design table:
    #   1m  → 30 calendar days
    #   5m, 15m → 90 calendar days
    #   30m, 1h → treated as 90d (same tier per design)
    # We encode a single row per instrument class with the tightest
    # constraint (1m=30d) stored in maxChunkDays; callers that need the
    # per-interval limit can consult ANGEL_ONE_CHUNK_DAYS below.
    ProviderCapability(
        provider=ProviderId.ANGEL_ONE,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="EQ",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=30,          # tightest constraint (1m interval)
        requestsPerSecond=3.0,
        intervalSupport=["1m", "5m", "15m", "30m", "1h"],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=1,
    ),
    ProviderCapability(
        provider=ProviderId.ANGEL_ONE,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="FO",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=30,
        requestsPerSecond=3.0,
        intervalSupport=["1m", "5m", "15m", "30m", "1h"],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=1,
    ),

    # Broker analytics (PCR, OI buildup, gainers/losers) — Angel One only
    ProviderCapability(
        provider=ProviderId.ANGEL_ONE,
        dataType=DataType.BROKER_ANALYTICS,
        instrumentClass="FO",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=3.0,
        intervalSupport=[],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=1,
    ),

    # =========================================================================
    # Upstox V2/V3
    # Authenticated (OAuth).
    # Primary for: index intraday history; intervals not natively in Angel One.
    # Rate limit: 10 req/s.
    # Chunk limits per design: 1m→7d, 5m/15m→30d, 1d→365d.
    # =========================================================================

    ProviderCapability(
        provider=ProviderId.UPSTOX,
        dataType=DataType.LIVE_QUOTE,
        instrumentClass="EQ",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=10.0,
        intervalSupport=[],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=2,
    ),
    ProviderCapability(
        provider=ProviderId.UPSTOX,
        dataType=DataType.LIVE_QUOTE,
        instrumentClass="FO",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=10.0,
        intervalSupport=[],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=2,
    ),
    ProviderCapability(
        provider=ProviderId.UPSTOX,
        dataType=DataType.LIVE_QUOTE,
        instrumentClass="IDX",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=10.0,
        intervalSupport=[],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=1,   # PRIMARY for index live
    ),

    # Historical OHLCV — index: Upstox is PRIMARY (priority=1)
    ProviderCapability(
        provider=ProviderId.UPSTOX,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="IDX",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=7,           # tightest (1m)
        requestsPerSecond=10.0,
        intervalSupport=["1m", "5m", "10m", "15m", "30m", "1h", "1d"],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=1,
    ),

    # Historical OHLCV — equity (secondary to Angel One)
    ProviderCapability(
        provider=ProviderId.UPSTOX,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="EQ",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=7,
        requestsPerSecond=10.0,
        intervalSupport=["1m", "5m", "10m", "15m", "30m", "1h", "1d"],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=2,
    ),

    # Historical OHLCV — F&O (secondary to Angel One)
    ProviderCapability(
        provider=ProviderId.UPSTOX,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="FO",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=7,
        requestsPerSecond=10.0,
        intervalSupport=["1m", "5m", "10m", "15m", "30m", "1h", "1d"],
        sourceType=SourceType.BROKER_AUTHENTICATED,
        priority=2,
    ),

    # =========================================================================
    # Scrapling / NSE
    # Credential-free; WAF bypass via curl_cffi / Scrapling.
    # Used for: live quotes, option chain snapshots, instrument master.
    # No chunk concept (live/snapshot only).
    # =========================================================================

    ProviderCapability(
        provider=ProviderId.SCRAPLING_NSE,
        dataType=DataType.LIVE_QUOTE,
        instrumentClass="EQ",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=2.0,
        intervalSupport=[],
        sourceType=SourceType.OPEN_SOURCE_NSE_DERIVED,
        priority=3,
    ),
    ProviderCapability(
        provider=ProviderId.SCRAPLING_NSE,
        dataType=DataType.LIVE_QUOTE,
        instrumentClass="FO",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=2.0,
        intervalSupport=[],
        sourceType=SourceType.OPEN_SOURCE_NSE_DERIVED,
        priority=3,
    ),
    ProviderCapability(
        provider=ProviderId.SCRAPLING_NSE,
        dataType=DataType.LIVE_QUOTE,
        instrumentClass="IDX",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=2.0,
        intervalSupport=[],
        sourceType=SourceType.OPEN_SOURCE_NSE_DERIVED,
        priority=2,
    ),
    ProviderCapability(
        provider=ProviderId.SCRAPLING_NSE,
        dataType=DataType.OPTION_CHAIN,
        instrumentClass="FO",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=2.0,
        intervalSupport=[],
        sourceType=SourceType.OPEN_SOURCE_NSE_DERIVED,
        priority=1,
    ),
    ProviderCapability(
        provider=ProviderId.SCRAPLING_NSE,
        dataType=DataType.OPTION_CHAIN,
        instrumentClass="IDX",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=2.0,
        intervalSupport=[],
        sourceType=SourceType.OPEN_SOURCE_NSE_DERIVED,
        priority=1,
    ),
    ProviderCapability(
        provider=ProviderId.SCRAPLING_NSE,
        dataType=DataType.INSTRUMENT_MASTER,
        instrumentClass="ALL",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=2.0,
        intervalSupport=[],
        sourceType=SourceType.OPEN_SOURCE_NSE_DERIVED,
        priority=1,
    ),

    # =========================================================================
    # Jugaad-data
    # Credential-free.
    # PRIMARY for: F&O EOD historical data with OI.
    # Chunk: up to 3650 calendar days (10 years).
    # =========================================================================

    ProviderCapability(
        provider=ProviderId.JUGAAD_DATA,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="FO",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=3650,
        requestsPerSecond=1.0,
        intervalSupport=["1d"],
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=1,
    ),

    # =========================================================================
    # OpenChart
    # Credential-free; open-source supplement.
    # Covers all canonical Indian timeframes for all instrument classes.
    # Used for reconciliation fallback and supplemental history.
    # Chunk: 365 calendar days for any interval.
    # =========================================================================

    ProviderCapability(
        provider=ProviderId.OPENCHART,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="EQ",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=365,
        requestsPerSecond=5.0,
        intervalSupport=list(CANONICAL_INDIAN_TIMEFRAMES),
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=3,
    ),
    ProviderCapability(
        provider=ProviderId.OPENCHART,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="FO",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=365,
        requestsPerSecond=5.0,
        intervalSupport=list(CANONICAL_INDIAN_TIMEFRAMES),
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=3,
    ),
    ProviderCapability(
        provider=ProviderId.OPENCHART,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="IDX",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=365,
        requestsPerSecond=5.0,
        intervalSupport=list(CANONICAL_INDIAN_TIMEFRAMES),
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=3,
    ),

    # =========================================================================
    # Yahoo Finance
    # Credential-free; SECONDARY_FALLBACK only.
    # Restricted to equity EOD historical data.
    # Maximum quality grade B; always tagged SECONDARY_FALLBACK.
    # =========================================================================

    ProviderCapability(
        provider=ProviderId.YAHOO_FINANCE,
        dataType=DataType.HISTORICAL_OHLCV,
        instrumentClass="EQ",
        supported=True,
        liveSupported=False,
        historySupported=True,
        maxChunkDays=365,
        requestsPerSecond=1.0,
        intervalSupport=["1d"],
        sourceType=SourceType.SECONDARY_FALLBACK,
        priority=10,   # Lowest priority — absolute last resort
    ),

    # =========================================================================
    # Binance
    # Credential-free REST + WebSocket.
    # Crypto spot and perpetual futures for BTC/ETH/SOL.
    # The ``3m`` interval IS allowed here — Binance natively supports it.
    # Rate limit: 20 req/s (REST).
    # =========================================================================

    # Spot klines
    ProviderCapability(
        provider=ProviderId.BINANCE,
        dataType=DataType.CRYPTO_KLINES,
        instrumentClass="CRYPTO_SPOT",
        supported=True,
        liveSupported=True,
        historySupported=True,
        maxChunkDays=365,
        requestsPerSecond=20.0,
        # 3m is explicitly permitted for Binance crypto (by design exception)
        intervalSupport=["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"],
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=1,
    ),

    # Perpetual futures klines
    ProviderCapability(
        provider=ProviderId.BINANCE,
        dataType=DataType.CRYPTO_KLINES,
        instrumentClass="CRYPTO_FUTURES",
        supported=True,
        liveSupported=True,
        historySupported=True,
        maxChunkDays=365,
        requestsPerSecond=20.0,
        intervalSupport=["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"],
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=1,
    ),

    # Futures market data (mark price, funding rate, OI, L/S ratio)
    ProviderCapability(
        provider=ProviderId.BINANCE,
        dataType=DataType.CRYPTO_FUTURES,
        instrumentClass="CRYPTO_FUTURES",
        supported=True,
        liveSupported=True,
        historySupported=True,
        maxChunkDays=30,
        requestsPerSecond=20.0,
        intervalSupport=["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"],
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=1,
    ),

    # =========================================================================
    # Deribit
    # Credential-free REST.
    # Crypto options (book summary, index price) for BTC/ETH/SOL.
    # Rate limit: 5 req/s.
    # =========================================================================

    ProviderCapability(
        provider=ProviderId.DERIBIT,
        dataType=DataType.CRYPTO_OPTIONS,
        instrumentClass="CRYPTO_OPTIONS",
        supported=True,
        liveSupported=True,
        historySupported=False,
        maxChunkDays=0,
        requestsPerSecond=5.0,
        intervalSupport=[],
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=1,
    ),

    # =========================================================================
    # Delta Exchange India
    # Credential-free public REST + WebSocket.
    # INR-settled perpetual futures for BTC/ETH/SOL (BTCUSD, ETHUSD, SOLUSD).
    # AlphaForge default active broker (ACTIVE_BROKER=delta).
    # Rate limit: 10 req/s (conservative; Delta has generous public limits).
    # Priority: 2 (Binance is primary; Delta is fallback / primary for India).
    # DS2-RCA-001 fix.
    # =========================================================================

    # Spot + futures OHLCV klines
    ProviderCapability(
        provider=ProviderId.DELTA,
        dataType=DataType.CRYPTO_KLINES,
        instrumentClass="CRYPTO_SPOT",
        supported=True,
        liveSupported=True,
        historySupported=True,
        maxChunkDays=365,
        requestsPerSecond=10.0,
        # 3m is explicitly permitted for Delta crypto (same crypto exception as Binance)
        intervalSupport=["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"],
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=2,
    ),
    ProviderCapability(
        provider=ProviderId.DELTA,
        dataType=DataType.CRYPTO_KLINES,
        instrumentClass="CRYPTO_FUTURES",
        supported=True,
        liveSupported=True,
        historySupported=True,
        maxChunkDays=365,
        requestsPerSecond=10.0,
        intervalSupport=["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"],
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=2,
    ),

    # Futures market data (mark price, funding, OI history)
    # Note: Delta India has NO public long/short ratio endpoint.
    ProviderCapability(
        provider=ProviderId.DELTA,
        dataType=DataType.CRYPTO_FUTURES,
        instrumentClass="CRYPTO_FUTURES",
        supported=True,
        liveSupported=True,
        historySupported=True,
        maxChunkDays=30,
        requestsPerSecond=10.0,
        intervalSupport=["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"],
        sourceType=SourceType.CREDENTIAL_FREE,
        priority=2,
    ),
]


# ---------------------------------------------------------------------------
# Per-interval chunk-day overrides for Angel One
# (used by the Historical Engine acquisition planner)
# ---------------------------------------------------------------------------

ANGEL_ONE_CHUNK_DAYS: dict[str, int] = {
    "1m": 30,
    "5m": 90,
    "10m": 90,
    "15m": 90,
    "30m": 90,
    "1h": 90,
    "1d": 365,
    "1w": 365,
    "1M": 365,
}

# Per-interval chunk-day overrides for Upstox
UPSTOX_CHUNK_DAYS: dict[str, int] = {
    "1m": 7,
    "5m": 30,
    "10m": 30,
    "15m": 30,
    "30m": 30,
    "1h": 30,
    "1d": 365,
    "1w": 365,
    "1M": 365,
}


# ---------------------------------------------------------------------------
# Public query API
# ---------------------------------------------------------------------------


def get_providers_for(
    data_type: DataType,
    instrument_class: str,
    interval: Optional[str] = None,
) -> list[ProviderCapability]:
    """Return all supported capabilities for the given data type and instrument class.

    The result is sorted by ``priority`` ascending (lower value = higher
    priority) so callers can iterate in preference order.

    ``3m`` interval: if ``interval`` is ``"3m"`` and ``instrument_class``
    is an Indian market class (EQ, FO, IDX), no providers are returned —
    the 3m interval is permanently unsupported for Indian market data.

    Args:
        data_type:        The requested data category.
        instrument_class: The instrument class string (e.g. "EQ", "CRYPTO_SPOT").
        interval:         Optional candle interval to filter on.

    Returns:
        List of matching ProviderCapability records, sorted by priority.
    """
    # 3m hard block for Indian markets
    if (
        interval == "3m"
        and instrument_class not in CRYPTO_INSTRUMENT_CLASSES
    ):
        return []

    results = [
        cap
        for cap in _MATRIX
        if (
            cap.dataType == data_type
            and (cap.instrumentClass == instrument_class or cap.instrumentClass == "ALL")
            and cap.supported
            and (interval is None or interval in cap.intervalSupport or not cap.intervalSupport)
        )
    ]

    return sorted(results, key=lambda c: c.priority)


def get_capability(
    provider_id: ProviderId,
    data_type: DataType,
    instrument_class: Optional[str] = None,
) -> Optional[ProviderCapability]:
    """Return the first matching capability for a specific provider and data type.

    If ``instrument_class`` is supplied, the match is additionally filtered
    to that class (or "ALL").  Returns ``None`` if no match is found.

    Args:
        provider_id:      The provider to look up.
        data_type:        The data type to look up.
        instrument_class: Optional instrument class filter.

    Returns:
        A matching ProviderCapability, or None.
    """
    for cap in _MATRIX:
        if cap.provider != provider_id or cap.dataType != data_type:
            continue
        if instrument_class is not None:
            if cap.instrumentClass not in (instrument_class, "ALL"):
                continue
        return cap
    return None


def get_chunk_days(
    provider_id: ProviderId,
    interval: str,
) -> int:
    """Return the per-interval chunk size for a provider.

    Falls back to the general ``maxChunkDays`` from the first matching
    capability if no per-interval override is defined.

    Args:
        provider_id: The provider identifier.
        interval:    The candle interval string.

    Returns:
        Maximum chunk size in calendar days.
    """
    if provider_id == ProviderId.ANGEL_ONE:
        return ANGEL_ONE_CHUNK_DAYS.get(interval, 30)
    if provider_id == ProviderId.UPSTOX:
        return UPSTOX_CHUNK_DAYS.get(interval, 7)

    # For other providers, find the first matching capability and use its
    # maxChunkDays (interval-independent for those providers).
    for cap in _MATRIX:
        if cap.provider == provider_id and cap.supported and cap.historySupported:
            return cap.maxChunkDays
    return 0
