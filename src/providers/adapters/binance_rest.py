"""
Binance REST provider adapter — Task 11.1.

Implements the Binance REST API client for fetching crypto market data:

- OHLCV candlestick klines (spot + futures)
- Current ticker price
- Exchange information
- 24-hour rolling statistics
- Perpetual futures data (mark price, funding rates, open interest)
- Long/short account ratios
- Open interest history

Key design points
-----------------
* The ``3m`` interval is **NOT** banned here.  The 3m ban applies only to
  Indian market data.  Binance natively supports ``3m`` for crypto and this
  adapter explicitly allows it (Requirement 13.1, design: "3m Interval Rule").
* Retry logic: up to 3 attempts on transient HTTP 5xx errors with
  exponential backoff (base 1s, factor 2x).
* HTTP 429 is NOT retried automatically — the caller (gateway / rate limiter)
  is responsible for honouring ``Retry-After``.
* Timeout: 10s per request (configurable).
* The client can operate with or without an API key.  Public endpoints
  (klines, ticker) work credential-free; some endpoints require an API key.

Binance kline array layout (indices)
-------------------------------------
  0  openTime          (ms)
  1  open
  2  high
  3  low
  4  close
  5  volume            (base asset)
  6  closeTime         (ms)
  7  quoteAssetVolume
  8  numberOfTrades
  9  takerBuyBaseAssetVolume
  10 takerBuyQuoteAssetVolume
  11 ignore

Requirements: 13.1, 13.2, 13.3, 13.4, 13.8, 13.9, 13.10
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import structlog

from src.observability.logging import get_logger

logger: structlog.stdlib.BoundLogger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "binance"

# Binance REST API base URLs.
_SPOT_BASE_URL = "https://api.binance.com"
_FUTURES_BASE_URL = "https://fapi.binance.com"

# Default request timeout in seconds.
_DEFAULT_TIMEOUT_SEC: float = 10.0

# Maximum number of retry attempts for transient 5xx errors.
_MAX_RETRIES: int = 3

# Base delay (seconds) for exponential backoff between retries.
_RETRY_BASE_DELAY_SEC: float = 1.0

# Binance-supported crypto intervals (3m IS supported — not banned for crypto).
BINANCE_INTERVALS: frozenset[str] = frozenset(
    ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"]
)

# ---------------------------------------------------------------------------
# Normaliser helpers
# ---------------------------------------------------------------------------


def _normalise_kline(raw: list[Any]) -> dict[str, Any]:
    """Convert a single Binance kline array entry to a canonical dict.

    Binance kline array layout:
        [openTime, open, high, low, close, volume, closeTime, ...]

    The canonical shape returned:

        {
            "time":      int,    # openTime in UTC epoch ms
            "open":      float,
            "high":      float,
            "low":       float,
            "close":     float,
            "volume":    float,  # base-asset volume
            "closeTime": int,    # closeTime in UTC epoch ms
        }

    Args:
        raw: A single Binance kline list (12 elements).

    Returns:
        Canonical OHLCV dict.
    """
    return {
        "time":      int(raw[0]),
        "open":      float(raw[1]),
        "high":      float(raw[2]),
        "low":       float(raw[3]),
        "close":     float(raw[4]),
        "volume":    float(raw[5]),
        "closeTime": int(raw[6]),
    }


# ---------------------------------------------------------------------------
# BinanceClient
# ---------------------------------------------------------------------------


class BinanceClient:
    """Async REST client for the Binance API.

    Covers spot market data (klines, ticker, exchange info, 24hr stats) and
    perpetual futures data (mark price, funding rate, open interest, L/S ratio).

    The client can be used as an async context manager to ensure the
    underlying ``httpx.AsyncClient`` is properly closed::

        async with BinanceClient() as client:
            candles = await client.get_klines("BTCUSDT", "1h")

    Or instantiated directly (caller is responsible for closing)::

        client = BinanceClient(api_key="your-key")
        candles = await client.get_klines("BTCUSDT", "1m", limit=100)
        await client.close()

    Note on ``3m`` interval:
        Unlike Indian market adapters, ``3m`` is fully supported here.
        Binance natively supports the 3m candle interval for crypto data
        (design doc: "3m Interval Rule" exception for crypto).

    Args:
        base_url: Spot API base URL. Defaults to ``https://api.binance.com``.
        futures_base_url: Futures API base URL.
            Defaults to ``https://fapi.binance.com``.
        api_key: Optional Binance API key. Required for private endpoints.
        session: Optional pre-built ``httpx.AsyncClient``. When provided the
            client is NOT closed on ``__aexit__`` / ``close()`` — the caller
            owns its lifecycle.
        timeout: Request timeout in seconds. Defaults to 10.
        max_retries: Number of retry attempts on transient 5xx errors.
            Defaults to 3.
    """

    def __init__(
        self,
        base_url: str = _SPOT_BASE_URL,
        futures_base_url: str = _FUTURES_BASE_URL,
        api_key: str | None = None,
        session: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SEC,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._futures_base_url = futures_base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries

        # If an external session was supplied, we don't own its lifecycle.
        self._session = session
        self._owns_session = session is None

    # ------------------------------------------------------------------ #
    # Public API — Spot
    # ------------------------------------------------------------------ #

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV candlestick data from ``/api/v3/klines``.

        Returns a list of canonical OHLCV dicts ordered oldest-first::

            [
                {
                    "time":      int,    # openTime, UTC epoch ms
                    "open":      float,
                    "high":      float,
                    "low":       float,
                    "close":     float,
                    "volume":    float,  # base-asset volume
                    "closeTime": int,    # closeTime, UTC epoch ms
                },
                ...
            ]

        The ``3m`` interval is fully supported (crypto exception to the
        Indian-market 3m ban — see module docstring).

        Args:
            symbol:   Binance symbol, e.g. ``"BTCUSDT"``.
            interval: Kline interval. One of the values in
                      :data:`BINANCE_INTERVALS`.
            limit:    Number of klines to return. Max 1000. Defaults to 500.
            start_ms: Optional start time as UTC epoch milliseconds.
            end_ms:   Optional end time as UTC epoch milliseconds.

        Returns:
            List of normalised OHLCV dicts.

        Raises:
            httpx.HTTPStatusError: For non-2xx responses after all retries.
            ValueError: If ``interval`` is not a recognised Binance interval.
        """
        if interval not in BINANCE_INTERVALS:
            raise ValueError(
                f"interval {interval!r} is not a supported Binance interval. "
                f"Supported: {sorted(BINANCE_INTERVALS)}"
            )

        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit,
        }
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms

        logger.debug(
            "binance.get_klines.start",
            component="binance_rest_client",
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

        raw: list[Any] = await self._get(
            f"{self._base_url}/api/v3/klines", params=params
        )
        candles = [_normalise_kline(row) for row in raw]

        logger.debug(
            "binance.get_klines.complete",
            component="binance_rest_client",
            symbol=symbol,
            interval=interval,
            count=len(candles),
        )
        return candles

    async def get_ticker_price(self, symbol: str) -> dict[str, Any]:
        """Fetch current price from ``/api/v3/ticker/price``.

        Returns::

            {"symbol": "BTCUSDT", "price": "65123.45"}

        Args:
            symbol: Binance trading symbol, e.g. ``"BTCUSDT"``.

        Returns:
            Dict with ``symbol`` and ``price`` keys.

        Raises:
            httpx.HTTPStatusError: For non-2xx responses.
        """
        params = {"symbol": symbol.upper()}
        return await self._get(  # type: ignore[return-value]
            f"{self._base_url}/api/v3/ticker/price", params=params
        )

    async def get_exchange_info(
        self, symbol: str | None = None
    ) -> dict[str, Any]:
        """Fetch exchange info from ``/api/v3/exchangeInfo``.

        When ``symbol`` is provided, only metadata for that symbol is returned.
        When ``None``, metadata for all symbols is returned (large payload).

        Args:
            symbol: Optional trading symbol filter.

        Returns:
            Exchange info dict from Binance.

        Raises:
            httpx.HTTPStatusError: For non-2xx responses.
        """
        params: dict[str, str] = {}
        if symbol is not None:
            params["symbol"] = symbol.upper()

        return await self._get(  # type: ignore[return-value]
            f"{self._base_url}/api/v3/exchangeInfo", params=params
        )

    async def get_24hr_stats(self, symbol: str) -> dict[str, Any]:
        """Fetch 24-hour rolling window statistics from ``/api/v3/ticker/24hr``.

        Returns the full 24hr ticker stats dict as returned by Binance,
        including ``openPrice``, ``highPrice``, ``lowPrice``, ``lastPrice``,
        ``volume``, ``priceChange``, ``priceChangePercent``, etc.

        Args:
            symbol: Binance trading symbol, e.g. ``"BTCUSDT"``.

        Returns:
            24hr stats dict from Binance.

        Raises:
            httpx.HTTPStatusError: For non-2xx responses.
        """
        params = {"symbol": symbol.upper()}
        return await self._get(  # type: ignore[return-value]
            f"{self._base_url}/api/v3/ticker/24hr", params=params
        )

    # ------------------------------------------------------------------ #
    # Public API — Futures (fapi.binance.com)
    # ------------------------------------------------------------------ #

    async def get_futures_mark_price(self, symbol: str) -> dict[str, Any]:
        """Fetch mark price and funding rate from ``/fapi/v1/premiumIndex``.

        Returns fields: ``symbol``, ``markPrice``, ``indexPrice``,
        ``lastFundingRate``, ``nextFundingTime``, ``interestRate``, ``time``.

        Args:
            symbol: Futures trading symbol, e.g. ``"BTCUSDT"``.

        Returns:
            Mark price dict.

        Raises:
            httpx.HTTPStatusError: For non-2xx responses.
        """
        params = {"symbol": symbol.upper()}
        return await self._get(  # type: ignore[return-value]
            f"{self._futures_base_url}/fapi/v1/premiumIndex", params=params
        )

    async def get_futures_open_interest(self, symbol: str) -> dict[str, Any]:
        """Fetch open interest from ``/fapi/v1/openInterest``.

        Returns fields: ``symbol``, ``openInterest``, ``time``.

        Args:
            symbol: Futures trading symbol.

        Returns:
            Open interest dict.

        Raises:
            httpx.HTTPStatusError: For non-2xx responses.
        """
        params = {"symbol": symbol.upper()}
        return await self._get(  # type: ignore[return-value]
            f"{self._futures_base_url}/fapi/v1/openInterest", params=params
        )

    async def get_futures_oi_history(
        self,
        symbol: str,
        period: str,
        limit: int = 30,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch open interest history from ``/futures/data/openInterestHist``.

        Supported periods: ``5m``, ``15m``, ``30m``, ``1h``, ``2h``, ``4h``,
        ``6h``, ``12h``, ``1d``.

        Each returned item has: ``symbol``, ``sumOpenInterest``,
        ``sumOpenInterestValue``, ``timestamp``.

        Args:
            symbol:   Futures symbol.
            period:   Aggregation period.
            limit:    Number of records. Defaults to 30.
            start_ms: Optional start time (UTC epoch ms).
            end_ms:   Optional end time (UTC epoch ms).

        Returns:
            List of OI history records.

        Raises:
            httpx.HTTPStatusError: For non-2xx responses.
        """
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "period": period,
            "limit": limit,
        }
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms

        return await self._get(  # type: ignore[return-value]
            f"{self._futures_base_url}/futures/data/openInterestHist",
            params=params,
        )

    async def get_long_short_ratio(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Fetch global account long/short ratio from ``/futures/data/globalLongShortAccountRatio``.

        Each item contains: ``symbol``, ``longShortRatio``, ``longAccount``,
        ``shortAccount``, ``timestamp``.

        Args:
            symbol: Futures symbol, e.g. ``"BTCUSDT"``.
            period: Aggregation period. Defaults to ``"5m"``.
            limit:  Number of records. Defaults to 30.

        Returns:
            List of long/short ratio records.

        Raises:
            httpx.HTTPStatusError: For non-2xx responses.
        """
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "period": period,
            "limit": limit,
        }
        return await self._get(  # type: ignore[return-value]
            f"{self._futures_base_url}/futures/data/globalLongShortAccountRatio",
            params=params,
        )

    async def get_funding_rate_history(
        self,
        symbol: str,
        limit: int = 100,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch funding rate history from ``/fapi/v1/fundingRate``.

        Each item contains: ``symbol``, ``fundingRate``, ``fundingTime``.

        Args:
            symbol:   Futures symbol, e.g. ``"BTCUSDT"``.
            limit:    Number of records. Defaults to 100.
            start_ms: Optional start time (UTC epoch ms).
            end_ms:   Optional end time (UTC epoch ms).

        Returns:
            List of funding rate history records.

        Raises:
            httpx.HTTPStatusError: For non-2xx responses.
        """
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "limit": limit,
        }
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms

        return await self._get(  # type: ignore[return-value]
            f"{self._futures_base_url}/fapi/v1/fundingRate", params=params
        )

    # ------------------------------------------------------------------ #
    # Client lifecycle
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owns_session and self._session is not None:
            await self._session.aclose()
            self._session = None

    async def __aenter__(self) -> BinanceClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_headers(self) -> dict[str, str]:
        """Build request headers, injecting API key when present."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "data-service/2.0 binance-rest-client",
        }
        if self._api_key:
            headers["X-MBX-APIKEY"] = self._api_key
        return headers

    async def _get_session(self) -> httpx.AsyncClient:
        """Return the HTTP session, creating one lazily if needed."""
        if self._session is None:
            self._session = httpx.AsyncClient(
                timeout=self._timeout,
                headers=self._build_headers(),
                follow_redirects=True,
            )
            self._owns_session = True
        return self._session

    async def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform a GET request with retry logic for transient 5xx errors.

        Raises ``httpx.HTTPStatusError`` immediately for HTTP 4xx responses
        (client errors are not retried).  Retries up to ``_max_retries``
        times for HTTP 5xx with exponential backoff (base ``_RETRY_BASE_DELAY_SEC``,
        factor 2x).

        HTTP 429 is propagated immediately without retrying — the gateway
        layer is responsible for honouring ``Retry-After`` and not
        incrementing the circuit-breaker failure counter (Requirement 5.5).

        Args:
            url:    Full URL to request.
            params: Optional query parameters.

        Returns:
            Parsed JSON response (dict or list).

        Raises:
            httpx.HTTPStatusError: For non-2xx responses after all retries.
        """
        session = await self._get_session()
        last_exc: httpx.HTTPStatusError | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await session.get(url, params=params)
                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code

                # 4xx and 429 — do not retry; propagate immediately.
                if status < 500 or status == 429:
                    logger.warning(
                        "binance.http_error",
                        component="binance_rest_client",
                        url=url,
                        status_code=status,
                        attempt=attempt,
                    )
                    raise

                # 5xx — transient; retry with backoff.
                last_exc = exc
                delay = _RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                logger.warning(
                    "binance.transient_5xx_retry",
                    component="binance_rest_client",
                    url=url,
                    status_code=status,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    retry_delay_sec=delay,
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(delay)

        # All retries exhausted for 5xx.
        assert last_exc is not None
        logger.error(
            "binance.all_retries_exhausted",
            component="binance_rest_client",
            url=url,
            max_retries=self._max_retries,
        )
        raise last_exc
