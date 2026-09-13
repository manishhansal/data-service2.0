"""
Provider schema types for DATA-SERVICE 2.0.

Defines all enumerations and the ProviderCapability Pydantic model used
by the Capability_Matrix, Circuit Breaker, Rate Limiter, and Provider
Gateway components.

Requirements: 5.1, 10.3, 10.4
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ProviderId(str, Enum):
    """Canonical provider identifiers."""

    ANGEL_ONE = "angel_one"
    UPSTOX = "upstox"
    SCRAPLING_NSE = "scrapling_nse"
    JUGAAD_DATA = "jugaad_data"
    OPENCHART = "openchart"
    YAHOO_FINANCE = "yahoo_finance"
    BINANCE = "binance"
    DERIBIT = "deribit"
    DELTA = "delta"  # Delta Exchange India — INR-settled perpetuals


class DataType(str, Enum):
    """Data categories served by providers."""

    LIVE_QUOTE = "LIVE_QUOTE"
    HISTORICAL_OHLCV = "HISTORICAL_OHLCV"
    OPTION_CHAIN = "OPTION_CHAIN"
    INSTRUMENT_MASTER = "INSTRUMENT_MASTER"
    FUTURES_DATA = "FUTURES_DATA"
    CRYPTO_KLINES = "CRYPTO_KLINES"
    CRYPTO_FUTURES = "CRYPTO_FUTURES"
    CRYPTO_OPTIONS = "CRYPTO_OPTIONS"
    BROKER_ANALYTICS = "BROKER_ANALYTICS"


class SourceType(str, Enum):
    """Authentication / credential classification for a provider source."""

    BROKER_AUTHENTICATED = "BROKER_AUTHENTICATED"
    OPEN_SOURCE_NSE_DERIVED = "OPEN_SOURCE_NSE_DERIVED"
    CREDENTIAL_FREE = "CREDENTIAL_FREE"
    SECONDARY_FALLBACK = "SECONDARY_FALLBACK"


class CircuitState(str, Enum):
    """Circuit-breaker state values (per-provider × per-capability)."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class ProviderStatus(str, Enum):
    """Operational health status of a provider."""

    UP = "UP"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Canonical instrument class constants
# ---------------------------------------------------------------------------

INSTRUMENT_CLASS_EQ: str = "EQ"
INSTRUMENT_CLASS_FO: str = "FO"
INSTRUMENT_CLASS_IDX: str = "IDX"
INSTRUMENT_CLASS_CRYPTO_SPOT: str = "CRYPTO_SPOT"
INSTRUMENT_CLASS_CRYPTO_FUTURES: str = "CRYPTO_FUTURES"
INSTRUMENT_CLASS_CRYPTO_OPTIONS: str = "CRYPTO_OPTIONS"
INSTRUMENT_CLASS_ALL: str = "ALL"

VALID_INSTRUMENT_CLASSES: frozenset[str] = frozenset(
    {
        INSTRUMENT_CLASS_EQ,
        INSTRUMENT_CLASS_FO,
        INSTRUMENT_CLASS_IDX,
        INSTRUMENT_CLASS_CRYPTO_SPOT,
        INSTRUMENT_CLASS_CRYPTO_FUTURES,
        INSTRUMENT_CLASS_CRYPTO_OPTIONS,
        INSTRUMENT_CLASS_ALL,
    }
)

# Instrument classes that represent Indian market data; used to enforce the
# 3m interval block (3m is only valid for crypto).
INDIAN_INSTRUMENT_CLASSES: frozenset[str] = frozenset(
    {
        INSTRUMENT_CLASS_EQ,
        INSTRUMENT_CLASS_FO,
        INSTRUMENT_CLASS_IDX,
    }
)

CRYPTO_INSTRUMENT_CLASSES: frozenset[str] = frozenset(
    {
        INSTRUMENT_CLASS_CRYPTO_SPOT,
        INSTRUMENT_CLASS_CRYPTO_FUTURES,
        INSTRUMENT_CLASS_CRYPTO_OPTIONS,
    }
)

# Canonical Indian-market timeframes (3m is permanently excluded).
CANONICAL_INDIAN_TIMEFRAMES: tuple[str, ...] = (
    "1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M",
)

# Binance-native crypto timeframes (3m IS allowed here).
CANONICAL_CRYPTO_TIMEFRAMES: tuple[str, ...] = (
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d",
)


# ---------------------------------------------------------------------------
# ProviderCapability model
# ---------------------------------------------------------------------------


class ProviderCapability(BaseModel):
    """Declares a single provider × data-type × instrument-class capability.

    The Capability_Matrix is built from a list of these records.  Each row
    describes what intervals and chunk sizes a provider supports for a
    specific combination of data type and instrument class.

    Attributes:
        provider:         The provider identifier.
        dataType:         The kind of data this row covers.
        instrumentClass:  The instrument class (EQ, FO, IDX, CRYPTO_*, ALL).
        supported:        Whether the provider supports this combination at all.
        liveSupported:    Whether real-time / streaming data is available.
        historySupported: Whether historical (OHLCV) data is available.
        maxChunkDays:     Maximum calendar-day range per single API request.
                          0 means "not applicable" (e.g. live-only providers).
        requestsPerSecond: Token-bucket refill rate for this provider.
        intervalSupport:  Supported candle intervals (e.g. ["1m","5m"]).
        sourceType:       Credential / authentication classification.
        priority:         Routing priority — lower value = higher priority.
    """

    provider: ProviderId
    dataType: DataType
    instrumentClass: Annotated[str, Field(pattern=r"^(EQ|FO|IDX|CRYPTO_SPOT|CRYPTO_FUTURES|CRYPTO_OPTIONS|ALL)$")]
    supported: bool
    liveSupported: bool
    historySupported: bool
    maxChunkDays: Annotated[int, Field(ge=0)]
    requestsPerSecond: Annotated[float, Field(gt=0.0)]
    intervalSupport: list[str]
    sourceType: SourceType
    priority: Annotated[int, Field(ge=0)]

    model_config = {"frozen": True}
