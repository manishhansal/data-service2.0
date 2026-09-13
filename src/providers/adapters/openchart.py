"""
OpenChart provider adapter — Task 4.8.

OpenChart (https://github.com/praveentom/openchart-nse) is a credential-free,
open-source NSE OHLCV provider.  It is used in DATA-SERVICE 2.0 as:

1. An **open-source supplement** that covers all canonical Indian timeframes.
2. A **reconciliation source** — second-pass cross-check for candles acquired
   from authenticated brokers (Angel One / Upstox).
3. A **fallback** when primary authenticated providers are unavailable.

Key constraints
---------------
* Credential-free — no authentication required.
* Supports all nine canonical Indian timeframes:
  ``1m``, ``5m``, ``10m``, ``15m``, ``30m``, ``1h``, ``1d``, ``1w``, ``1M``.
* The ``3m`` interval is permanently blocked (raises ``ValueError``).
* Source type: ``CREDENTIAL_FREE``.
* Rate limit: 5 req/s.

Interval mapping (OpenChart API → canonical label)
---------------------------------------------------
The OpenChart API uses its own interval strings.  This adapter translates the
platform's canonical intervals before making requests:

    1m  → ``1m``
    5m  → ``5m``
    10m → ``10m``
    15m → ``15m``
    30m → ``30m``
    1h  → ``60m``   (OpenChart uses minute-count for hour-level intervals)
    1d  → ``1d``
    1w  → ``1w``
    1M  → ``1M``

Requirements: 5.9, 5.12
"""

from __future__ import annotations

import datetime
from typing import Any

import httpx
import structlog

from src.core.schemas.provider import CANONICAL_INDIAN_TIMEFRAMES, SourceType
from src.observability.logging import get_logger

logger: structlog.stdlib.BoundLogger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "openchart"
BLOCKED_INTERVAL = "3m"
SOURCE_TYPE: SourceType = SourceType.CREDENTIAL_FREE
REQUESTS_PER_SECOND: float = 5.0

# OpenChart unofficial NSE charting API base URL.
_OPENCHART_BASE_URL = "https://charting.nseindia.com/Charts/symbolhistoricaldata/"

# Timeout for individual HTTP requests (seconds).
_HTTP_TIMEOUT_SEC: float = 30.0

# Canonical interval → OpenChart API interval string.
_INTERVAL_MAP: dict[str, str] = {
    "1m":  "1m",
    "5m":  "5m",
    "10m": "10m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "60m",   # OpenChart uses minute-count notation for hours
    "1d":  "1d",
    "1w":  "1w",
    "1M":  "1M",
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OpenChartError(Exception):
    """Raised when the OpenChart adapter encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Interval validator
# ---------------------------------------------------------------------------


def _validate_interval(interval: str) -> None:
    """Raise ``ValueError`` for banned or unsupported intervals.

    The ``3m`` interval is permanently banned for all Indian market data
    (Requirements 1.5, 4.2, 10.11).  Only the nine canonical Indian
    timeframes are supported.

    Args:
        interval: The requested candle interval string.

    Raises:
        ValueError: If ``interval == "3m"`` or not in the canonical set.
    """
    if interval == BLOCKED_INTERVAL:
        raise ValueError(
            "interval 3m is permanently unsupported for Indian market data."
        )
    if interval not in _INTERVAL_MAP:
        raise ValueError(
            f"OpenChartAdapter does not support interval {interval!r}. "
            f"Supported intervals: {sorted(_INTERVAL_MAP.keys())}"
        )


# ---------------------------------------------------------------------------
# Row normaliser
# ---------------------------------------------------------------------------


def _normalise_row(
    raw: dict[str, Any],
    symbol: str,
    exchange: str,
    interval: str,
) -> dict[str, Any]:
    """Convert a raw OpenChart response item into the platform's canonical
    OHLCV dict shape.

    The platform canonical shape (no OI — OpenChart does not provide OI)::

        {
            "time":               int,    # UTC epoch seconds (candle open time)
            "open":               float,
            "high":               float,
            "low":                float,
            "close":              float,
            "volume":             int,    # 0 when unavailable
            "volume_unavailable": bool,
            "oi":                 None,   # OpenChart never provides OI
            "oi_missing":         True,
            "symbol":             str,
            "exchange":           str,
            "interval":           str,
            "source_type":        str,
            "provider":           str,
        }

    OI semantics (Requirement 3.3, 6.2):
    OpenChart does not supply open interest.  ``oi`` is always ``None`` with
    ``oi_missing=True`` — it is **never** populated from any other field.

    Args:
        raw:      A single item from the OpenChart JSON response.
        symbol:   The requested trading symbol.
        exchange: The exchange (NSE / NFO).
        interval: The canonical interval label.

    Returns:
        A normalised canonical OHLCV dict.
    """
    def _float(key: str, default: float = 0.0) -> float:
        try:
            return float(raw.get(key, default))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def _int(key: str, default: int = 0) -> int:
        try:
            return int(float(raw.get(key, default)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    # Timestamp: OpenChart returns Unix seconds in field "t" or "time".
    ts_raw = raw.get("t") or raw.get("time") or raw.get("timestamp")
    try:
        epoch_sec = int(ts_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        epoch_sec = 0

    open_  = _float("o") or _float("open")
    high   = _float("h") or _float("high")
    low    = _float("l") or _float("low")
    close  = _float("c") or _float("close")

    vol_raw = raw.get("v") or raw.get("volume")
    if vol_raw is None:
        volume: int = 0
        volume_unavailable: bool = True
    else:
        try:
            volume = int(float(vol_raw))
            volume_unavailable = False
        except (TypeError, ValueError):
            volume = 0
            volume_unavailable = True

    return {
        "time": epoch_sec,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "volume_unavailable": volume_unavailable,
        # OpenChart does not provide OI (Requirement 3.3, 6.2).
        "oi": None,
        "oi_missing": True,
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "source_type": SOURCE_TYPE.value,
        "provider": PROVIDER_ID,
    }


# ---------------------------------------------------------------------------
# OpenChartAdapter
# ---------------------------------------------------------------------------


class OpenChartAdapter:
    """Credential-free adapter for OpenChart (NSE unofficial charting API).

    This adapter supports all nine canonical Indian timeframes and is used
    as both a supplement and a reconciliation fallback behind authenticated
    primary providers (Angel One, Upstox).

    Usage::

        adapter = OpenChartAdapter()
        rows = await adapter.fetch_historical_ohlcv(
            symbol="NIFTY",
            exchange="NSE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
            interval="1d",
        )

    Rate limiting is enforced at the gateway layer (token-bucket, 5 req/s).
    The adapter itself does not perform internal rate limiting.

    Args:
        http_client: Optional pre-constructed ``httpx.AsyncClient``.  When
            ``None`` a fresh client is created on first use.  In production
            the gateway should inject a shared client with connection pooling.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client
        self._owns_client = http_client is None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def fetch_historical_ohlcv(
        self,
        symbol: str,
        exchange: str,
        from_date: datetime.date,
        to_date: datetime.date,
        interval: str,
    ) -> list[dict[str, Any]]:
        """Fetch historical OHLCV candles from the OpenChart NSE API.

        All nine canonical Indian timeframes are supported.  Requesting
        ``3m`` always raises ``ValueError`` (permanent block).

        The returned list is ordered chronologically (oldest first) as
        returned by the upstream API.  Each element is a normalised
        canonical OHLCV dict; ``oi`` is always ``None`` (OpenChart does
        not publish open interest).

        Args:
            symbol:    NSE/NFO trading symbol, e.g. ``"NIFTY"`` or
                       ``"RELIANCE"``.
            exchange:  Exchange code, e.g. ``"NSE"`` or ``"NFO"``.
            from_date: Start of the requested date range (inclusive).
            to_date:   End of the requested date range (inclusive).
            interval:  Canonical candle interval.  One of:
                       ``1m``, ``5m``, ``10m``, ``15m``, ``30m``,
                       ``1h``, ``1d``, ``1w``, ``1M``.

        Returns:
            List of normalised OHLCV dicts ordered oldest-first.

        Raises:
            ValueError: If ``interval == "3m"`` or is not in the supported
                set.
            OpenChartError: If the upstream request fails in an unrecoverable
                way.
        """
        _validate_interval(interval)

        logger.info(
            "openchart.fetch_historical_ohlcv.start",
            component="openchart_adapter",
            symbol=symbol,
            exchange=exchange,
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            interval=interval,
        )

        raw_items = await self._request(symbol, exchange, from_date, to_date, interval)
        normalised = [_normalise_row(item, symbol, exchange, interval) for item in raw_items]

        logger.info(
            "openchart.fetch_historical_ohlcv.complete",
            component="openchart_adapter",
            symbol=symbol,
            interval=interval,
            row_count=len(normalised),
        )
        return normalised

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        symbol: str,
        exchange: str,
        from_date: datetime.date,
        to_date: datetime.date,
        interval: str,
    ) -> list[dict[str, Any]]:
        """Make the HTTP request to the OpenChart API and return raw items.

        OpenChart's NSE charting endpoint expects query parameters:
        ``symbol``, ``from``, ``to``, ``interval``, and ``type`` (either
        ``"EQ"`` for equities or ``"FUT"``/``"OPT"`` for derivatives).

        The response JSON has the shape::

            {
                "candles": [
                    {"t": <epoch_sec>, "o": .., "h": .., "l": .., "c": .., "v": ..},
                    ...
                ]
            }

        or a flat list in some versions of the unofficial API.

        On HTTP errors or malformed JSON the method logs a warning and
        returns an empty list (non-fatal; callers should fall back to another
        provider via the gateway).

        Args:
            symbol:    Trading symbol.
            exchange:  Exchange code.
            from_date: Start date.
            to_date:   End date.
            interval:  Canonical interval (pre-validated).

        Returns:
            List of raw candle dicts from the API response, or ``[]`` on error.
        """
        api_interval = _INTERVAL_MAP[interval]
        params: dict[str, str] = {
            "symbol": symbol.upper(),
            "from": from_date.strftime("%Y-%m-%d"),
            "to": to_date.strftime("%Y-%m-%d"),
            "interval": api_interval,
            "type": "EQ",   # default; gateway may override for F&O
            "exchange": exchange.upper(),
        }

        try:
            client = await self._get_client()
            response = await client.get(
                _OPENCHART_BASE_URL,
                params=params,
                timeout=_HTTP_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "openchart.request_error",
                component="openchart_adapter",
                symbol=symbol,
                interval=interval,
                error=str(exc),
            )
            return []

        if response.status_code != 200:
            logger.warning(
                "openchart.non_200_response",
                component="openchart_adapter",
                symbol=symbol,
                interval=interval,
                status_code=response.status_code,
            )
            return []

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "openchart.json_parse_error",
                component="openchart_adapter",
                symbol=symbol,
                interval=interval,
                error=str(exc),
            )
            return []

        # Normalise the response shape: some API versions return a dict with a
        # "candles" key; others return a flat list.
        if isinstance(body, list):
            return body  # type: ignore[return-value]
        if isinstance(body, dict):
            return body.get("candles", body.get("data", []))  # type: ignore[return-value]
        return []

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating one if necessary."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": "data-service/2.0 openchart-adapter"},
                timeout=_HTTP_TIMEOUT_SEC,
                follow_redirects=True,
            )
            self._owns_client = True
        return self._client

    # ------------------------------------------------------------------ #
    # Async context manager support
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> OpenChartAdapter:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
