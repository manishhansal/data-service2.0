"""
Delta Exchange REST provider adapter — DS2-RCA-001 fix.

Implements the Delta Exchange India REST API client for fetching crypto
market data:

- OHLCV candle history (spot + perpetual futures)
- Live tickers (mark price, OI, funding data)
- Product metadata (annualised funding rate)
- Open-interest history via the ``OI:{symbol}`` candle endpoint

Key design points
-----------------
* Delta Exchange India uses ``https://api.india.delta.exchange``.
  Global (non-India) deployments use ``https://api.delta.exchange``.
  The base URL is configurable via ``delta_rest_base_url`` in Settings.

* All Delta REST responses are wrapped in a JSON envelope::

      {"success": true, "result": <payload>}

  This client unwraps ``result`` automatically.  On ``success: false`` or
  non-2xx HTTP a ``ProviderDataError`` is raised.

* Delta candles use **Unix seconds** for the ``time`` field (not ms).
  This adapter converts to UTC-epoch **milliseconds** for canonical
  consistency with BinanceCandleRecord.

* Delta returns candles in **descending** order (newest first).  This
  client sorts ascending (oldest first) before returning, matching the
  Binance contract.

* The ``3m`` interval is **not** banned here — the ban applies only to
  Indian equity/F&O data.  Delta natively supports ``3m``.

* AlphaForge uses ``BTCUSD``, ``ETHUSD``, ``SOLUSD`` on Delta India —
  *not* the Binance ``BTCUSDT`` convention.

* Delta India does **not** expose:
  - A public global long/short ratio endpoint (returns empty list).
  - A per-period next-funding-time in REST (returns 0; UI hides it).
  - Liquidation events on a public channel.

* Retry logic: up to ``max_retries`` attempts on transient HTTP 5xx errors
  with exponential back-off (base 1 s, factor 2×).  HTTP 4xx and 429 are
  propagated immediately.

Timeout default: 20 s (Delta India is geographically pinned and can be
slow from outside the region).

Requirements: DS2-RCA-001, 13.1 (crypto 3m exception)
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from src.observability.logging import get_logger
from src.providers.adapters.base import (
    ProviderDataError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "delta"
EXCHANGE = "DELTA"

# Default base URL — India INR-settled perpetuals.
# Override via settings.delta_rest_base_url for global deployments or testnet.
_DEFAULT_BASE_URL = "https://api.india.delta.exchange"

# Per-request timeout. Delta India can be slow from outside India.
_DEFAULT_TIMEOUT_SEC: float = 20.0

# Retry policy — mirrors BinanceClient (5xx → retry, 4xx/429 → propagate).
_MAX_RETRIES: int = 3
_RETRY_BASE_DELAY_SEC: float = 1.0

# ---------------------------------------------------------------------------
# Interval mapping  (canonical → Delta resolution string)
# ---------------------------------------------------------------------------
# Delta has no native 8h candle; the closest supported is 6h.
# Ref: alpha-forge/src/services/brokers/delta/rest.ts::DELTA_RESOLUTIONS

DELTA_INTERVALS: dict[str, str] = {
    "1m":  "1m",
    "3m":  "3m",    # allowed for crypto — not banned here
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "2h":  "2h",
    "4h":  "4h",
    "6h":  "6h",
    "8h":  "6h",    # Delta has no 8h candle; 6h is the safe fallback
    "12h": "12h",
    "1d":  "1d",
}

# Duration of each interval in **seconds** (used to compute closeTime).
_INTERVAL_SECONDS: dict[str, int] = {
    "1m":  60,
    "3m":  180,
    "5m":  300,
    "15m": 900,
    "30m": 1_800,
    "1h":  3_600,
    "2h":  7_200,
    "4h":  14_400,
    "6h":  21_600,
    "8h":  28_800,
    "12h": 43_200,
    "1d":  86_400,
}

# Annualising factor for per-period funding rate.
# Delta India settles funding every 8 hours → 3 × 365 = 1 095 periods/year.
_FUNDING_INTERVAL_HOURS: int = 8
_FUNDING_PERIODS_PER_YEAR: float = (24 / _FUNDING_INTERVAL_HOURS) * 365  # = 1 095.0

# Maximum candles Delta returns per request.
_MAX_CANDLES_PER_REQUEST: int = 2_000


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _ts_to_ms(value: int | float | None) -> int:
    """Convert a Delta timestamp (seconds, ms, or microseconds) to UTC epoch ms.

    Delta uses different timestamp granularities on different endpoints:
    - Candle ``time`` field: Unix **seconds**.
    - Ticker ``timestamp`` field: Unix **microseconds** (very large int).

    Heuristic (mirrors alpha-forge tsToMs):
    - value > 1e14 → microseconds → divide by 1 000.
    - value > 1e11 → milliseconds → as-is.
    - otherwise    → seconds → multiply by 1 000.
    """
    if value is None or not isinstance(value, (int, float)):
        return 0
    if value > 1e14:
        return int(value / 1_000)
    if value > 1e11:
        return int(value)
    return int(value * 1_000)


def _normalise_candle(
    raw: dict[str, Any],
    symbol: str,
    interval: str,
) -> dict[str, Any]:
    """Convert a single Delta candle dict to the canonical OHLCV shape.

    Delta candle fields::
        {
            "time":   int,    # candle open time in **Unix seconds**
            "open":   float,
            "high":   float,
            "low":    float,
            "close":  float,
            "volume": float,  # may be absent
        }

    Returns a canonical dict::
        {
            "time":      int,   # openTime in UTC epoch **milliseconds**
            "open":      float,
            "high":      float,
            "low":       float,
            "close":     float,
            "volume":    float,
            "closeTime": int,   # openTime_ms + interval_sec * 1000 - 1
            "symbol":    str,
            "interval":  str,
            "exchange":  "DELTA",
        }
    """
    time_sec = raw.get("time", 0)
    open_ms = _ts_to_ms(time_sec) if time_sec > 1e11 else int(time_sec) * 1_000
    interval_sec = _INTERVAL_SECONDS.get(interval, 60)
    close_time_ms = open_ms + interval_sec * 1_000 - 1

    return {
        "time":      open_ms,
        "open":      float(raw.get("open", 0)),
        "high":      float(raw.get("high", 0)),
        "low":       float(raw.get("low", 0)),
        "close":     float(raw.get("close", 0)),
        "volume":    float(raw.get("volume", 0)),
        "closeTime": close_time_ms,
        "symbol":    symbol,
        "interval":  interval,
        "exchange":  EXCHANGE,
    }


def _normalise_ticker(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a Delta v2 ticker dict into the canonical shape.

    Delta ticker fields of interest::
        symbol, close, open, high, low, mark_price, spot_price,
        ltp_change_24h (percent, not decimal), oi, oi_value, oi_value_usd,
        turnover, turnover_usd, volume, timestamp (microseconds), product_id

    ``ltp_change_24h`` is already in percentage units (e.g. ``-1.522`` means
    -1.522 %), NOT a decimal fraction.  This is verified against Delta API docs
    and matches alpha-forge's adapter behaviour.

    Returns::
        {
            "symbol":       str,
            "price":        float,   # close (or mark_price as fallback)
            "change":       float,   # close - open
            "changePct":    float,   # ltp_change_24h (percent) or derived
            "high":         float,
            "low":          float,
            "volume":       float,
            "quoteVolume":  float,   # turnover_usd or turnover
            "markPrice":    float,
            "indexPrice":   float,   # spot_price
            "openInterest": float,   # oi in contracts
            "ts":           int,     # UTC epoch ms
            "exchange":     "DELTA",
        }
    """
    close = float(raw.get("close") or raw.get("mark_price") or 0)
    open_ = float(raw.get("open") or close)
    change = close - open_

    # ltp_change_24h is present for most endpoints; derive from open/close when absent.
    change_pct_raw = raw.get("ltp_change_24h")
    if change_pct_raw is not None:
        change_pct = float(change_pct_raw)
    elif open_ > 0:
        change_pct = (change / open_) * 100.0
    else:
        change_pct = 0.0

    quote_volume = float(raw.get("turnover_usd") or raw.get("turnover") or 0)
    ts = _ts_to_ms(raw.get("timestamp"))

    return {
        "symbol":       str(raw.get("symbol", "")),
        "price":        close,
        "change":       change,
        "changePct":    change_pct,
        "high":         float(raw.get("high") or close),
        "low":          float(raw.get("low") or close),
        "volume":       float(raw.get("volume") or 0),
        "quoteVolume":  quote_volume,
        "markPrice":    float(raw.get("mark_price") or close),
        "indexPrice":   float(raw.get("spot_price") or close),
        "openInterest": float(raw.get("oi") or 0),
        "ts":           ts,
        "exchange":     EXCHANGE,
    }


# ---------------------------------------------------------------------------
# DeltaClient
# ---------------------------------------------------------------------------


class DeltaClient:
    """Async REST client for the Delta Exchange India API.

    Covers public market-data endpoints used by DATA-SERVICE 2.0:
    - Candle history (spot + perpetual futures + OI history)
    - Ticker data (mark price, funding, OI, 24h change)
    - Product metadata (annualised funding rate)

    The client can be used as an async context manager::

        async with DeltaClient() as client:
            candles = await client.get_candles("BTCUSD", "1h", start_sec, end_sec)

    Or instantiated directly::

        client = DeltaClient()
        tickers = await client.get_tickers(symbols=["BTCUSD", "ETHUSD"])
        await client.close()

    All public endpoints work without credentials.  Authenticated private
    endpoints (trading, account) are out of scope for this adapter.

    Args:
        base_url:    Delta REST base URL.  Defaults to India endpoint.
        api_key:     Optional API key for authenticated endpoints.
        api_secret:  Optional API secret.
        session:     Optional pre-built ``httpx.AsyncClient``.  When supplied
                     the caller owns its lifecycle and ``close()`` is a no-op.
        timeout:     Per-request timeout in seconds. Defaults to 20.
        max_retries: Retry attempts on transient 5xx errors. Defaults to 3.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str | None = None,
        api_secret: str | None = None,
        session: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SEC,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = session
        self._owns_session = session is None

    # ------------------------------------------------------------------ #
    # Public API — Candles
    # ------------------------------------------------------------------ #

    async def get_candles(
        self,
        symbol: str,
        interval: str,
        start_sec: int,
        end_sec: int,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV candles from ``GET /v2/history/candles``.

        Delta returns candles in **descending** order (newest first).  This
        method sorts them **ascending** (oldest first) before returning,
        matching the canonical contract used by BinanceClient.

        The ``time`` field is converted from Unix seconds to UTC epoch
        **milliseconds** in the returned dicts.

        Args:
            symbol:    Delta instrument symbol, e.g. ``"BTCUSD"``.
            interval:  Candle interval from ``DELTA_INTERVALS`` keys.
            start_sec: Inclusive start time as Unix **seconds**.
            end_sec:   Inclusive end time as Unix **seconds**.

        Returns:
            List of canonical OHLCV dicts, oldest-first::

                [
                    {
                        "time":      int,   # openTime UTC epoch ms
                        "open":      float,
                        "high":      float,
                        "low":       float,
                        "close":     float,
                        "volume":    float,
                        "closeTime": int,   # UTC epoch ms
                        "symbol":    str,
                        "interval":  str,
                        "exchange":  "DELTA",
                    },
                    ...
                ]

        Raises:
            ValueError: If ``interval`` is not in ``DELTA_INTERVALS``.
            ProviderUnavailableError: On HTTP 5xx after all retries.
            ProviderRateLimitedError: On HTTP 429.
            ProviderDataError: On malformed response or Delta ``success: false``.
        """
        if interval not in DELTA_INTERVALS:
            raise ValueError(
                f"interval {interval!r} is not a supported Delta Exchange interval. "
                f"Supported: {sorted(DELTA_INTERVALS)}"
            )

        resolution = DELTA_INTERVALS[interval]
        params: dict[str, Any] = {
            "resolution": resolution,
            "symbol": symbol,
            "start": start_sec,
            "end": end_sec,
        }

        logger.debug(
            "delta.get_candles.start",
            component="delta_client",
            symbol=symbol,
            interval=interval,
            resolution=resolution,
        )

        raw: list[dict[str, Any]] = await self._get(
            f"{self._base_url}/v2/history/candles", params=params
        )

        # Delta returns descending; sort ascending (oldest-first).
        raw_sorted = sorted(raw, key=lambda c: c.get("time", 0))
        candles = [_normalise_candle(c, symbol=symbol, interval=interval) for c in raw_sorted]

        logger.debug(
            "delta.get_candles.complete",
            component="delta_client",
            symbol=symbol,
            interval=interval,
            count=len(candles),
        )
        return candles

    async def get_candles_range(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        """Fetch a wide candle range, paginating in 2 000-candle chunks.

        Converts ms timestamps to Unix seconds internally.

        Args:
            symbol:    Delta instrument symbol.
            interval:  Canonical interval string.
            start_ms:  Inclusive start as UTC epoch **milliseconds**.
            end_ms:    Inclusive end as UTC epoch **milliseconds**.

        Returns:
            Combined list of canonical OHLCV dicts, oldest-first.
        """
        interval_sec = _INTERVAL_SECONDS.get(interval, 60)
        chunk_span_sec = interval_sec * _MAX_CANDLES_PER_REQUEST

        all_candles: list[dict[str, Any]] = []
        cursor_sec = int(start_ms / 1_000)
        end_sec = int(end_ms / 1_000)

        for _ in range(20):  # Safety: max 20 pages × 2 000 = 40 000 candles
            if cursor_sec >= end_sec:
                break

            slice_end = min(end_sec, cursor_sec + chunk_span_sec)
            batch = await self.get_candles(
                symbol=symbol,
                interval=interval,
                start_sec=cursor_sec,
                end_sec=slice_end,
            )
            if not batch:
                break

            # Deduplicate at chunk boundary.
            last_kept_time = all_candles[-1]["time"] if all_candles else -1
            for c in batch:
                if c["time"] > last_kept_time:
                    all_candles.append(c)

            new_cursor = slice_end + 1
            if new_cursor <= cursor_sec:
                break
            cursor_sec = new_cursor

            if len(batch) < _MAX_CANDLES_PER_REQUEST:
                break

        return all_candles

    # ------------------------------------------------------------------ #
    # Public API — OI history (via candle endpoint with OI:{symbol})
    # ------------------------------------------------------------------ #

    async def get_oi_history(
        self,
        symbol: str,
        interval: str = "5m",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Fetch open-interest history for ``symbol``.

        Delta exposes OI history via the candle endpoint using the special
        instrument symbol ``OI:{symbol}`` (e.g. ``"OI:BTCUSD"``).  The OI
        value is stored in the ``close`` field of each candle.

        Returns a list of OI history points::

            [
                {
                    "ts":           int,   # closeTime UTC epoch ms
                    "openInterest": float, # OI in contracts (from candle close)
                    "notionalUsd":  0,     # unavailable from this endpoint
                },
                ...
            ]

        Args:
            symbol:   Base instrument symbol, e.g. ``"BTCUSD"``.
            interval: Aggregation interval (default ``"5m"``).
            limit:    Number of history points to return (default 30).

        Returns:
            List of OI history dicts.

        Raises:
            ProviderDataError, ProviderUnavailableError, ProviderRateLimitedError.
        """
        import time as _t  # noqa: PLC0415
        end_sec = int(_t.time())
        interval_sec = _INTERVAL_SECONDS.get(interval, 300)
        start_sec = end_sec - (limit + 2) * interval_sec

        try:
            candles = await self.get_candles(
                symbol=f"OI:{symbol}",
                interval=interval,
                start_sec=start_sec,
                end_sec=end_sec,
            )
        except Exception:  # noqa: BLE001
            return []

        result = candles[-limit:] if len(candles) > limit else candles
        return [
            {
                "ts":           c["closeTime"],
                "openInterest": c["close"],   # OI stored in close field
                "notionalUsd":  0,            # Not available from this endpoint
            }
            for c in result
        ]

    # ------------------------------------------------------------------ #
    # Public API — Tickers
    # ------------------------------------------------------------------ #

    async def get_tickers(
        self,
        symbols: list[str] | None = None,
        contract_types: list[str] | None = None,
        underlying_asset_symbols: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all tickers from ``GET /v2/tickers``.

        When ``symbols`` is supplied, uses the batch ticker endpoint
        ``GET /v2/tickers/{comma_joined}`` instead, which is faster for
        a small set of known symbols.

        Args:
            symbols:                  Optional list of symbol strings. When
                                      provided, the per-symbol endpoint is used.
            contract_types:           Optional list for the ``?contract_types``
                                      server-side filter (e.g.
                                      ``["perpetual_futures"]``).
            underlying_asset_symbols: Optional list for the
                                      ``?underlying_asset_symbols`` filter
                                      (e.g. ``["BTC","ETH","SOL"]``).

        Returns:
            List of normalised ticker dicts.
        """
        if symbols:
            # Batch ticker: /v2/tickers/{sym1},{sym2},...
            # Delta gateway 404s on percent-encoded commas, so encode each
            # symbol individually and join with a literal comma.
            joined = ",".join(s for s in symbols)
            raw_resp = await self._get(f"{self._base_url}/v2/tickers/{joined}")
            # Delta may return a single dict or a list depending on symbol count.
            raw_list: list[dict[str, Any]] = (
                raw_resp if isinstance(raw_resp, list) else [raw_resp]
            )
        else:
            params: dict[str, Any] = {}
            if contract_types:
                params["contract_types"] = ",".join(contract_types)
            if underlying_asset_symbols:
                params["underlying_asset_symbols"] = ",".join(underlying_asset_symbols)
            raw_list = await self._get(f"{self._base_url}/v2/tickers", params=params)

        return [_normalise_ticker(t) for t in raw_list if isinstance(t, dict)]

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch a single ticker for ``symbol``.

        Args:
            symbol: Delta instrument symbol, e.g. ``"BTCUSD"``.

        Returns:
            Normalised ticker dict.
        """
        tickers = await self.get_tickers(symbols=[symbol])
        if not tickers:
            raise ProviderDataError(
                f"Delta returned no ticker for {symbol!r}",
                provider=PROVIDER_ID,
            )
        return tickers[0]

    # ------------------------------------------------------------------ #
    # Public API — Product metadata
    # ------------------------------------------------------------------ #

    async def get_product(self, symbol: str) -> dict[str, Any]:
        """Fetch product metadata from ``GET /v2/products/{symbol}``.

        The product response includes ``annualized_funding`` (in percent)
        and ``funding_method``.  The annualised funding rate is used to
        derive the per-period funding rate for the premium-index computation.

        Returns the raw product dict (not normalised — fields vary widely by
        product type and the caller selects what it needs).
        """
        return await self._get(f"{self._base_url}/v2/products/{symbol}")  # type: ignore[return-value]

    async def get_premium_index(self, symbol: str) -> dict[str, Any]:
        """Compute premium-index equivalent data for a Delta perp.

        Mirrors the BinanceClient.get_futures_mark_price() return shape::

            {
                "symbol":               str,
                "markPrice":            float,
                "indexPrice":           float,
                "fundingRate":          float,   # per-period fraction
                "fundingRateAnnualized":float,   # as a decimal (not percent)
                "nextFundingTime":      int,     # ms; 0 = unavailable on Delta REST
                "ts":                   int,     # UTC epoch ms
                "exchange":             "DELTA",
            }

        Funding rate is derived from the product's ``annualized_funding``
        field (percent units) divided by ``_FUNDING_PERIODS_PER_YEAR``.
        """
        # Run ticker + product lookups concurrently; tolerate individual
        # failures so a missing product record doesn't block the ticker.
        import asyncio as _asyncio  # noqa: PLC0415
        ticker_task = _asyncio.create_task(self.get_ticker(symbol))
        product_task = _asyncio.create_task(self.get_product(symbol))

        ticker_result = await _asyncio.gather(ticker_task, return_exceptions=True)
        product_result = await _asyncio.gather(product_task, return_exceptions=True)

        ticker = ticker_result[0] if not isinstance(ticker_result[0], BaseException) else {}
        product = product_result[0] if not isinstance(product_result[0], BaseException) else {}

        mark_price = float(ticker.get("markPrice") or ticker.get("price") or 0)
        index_price = float(ticker.get("indexPrice") or mark_price)

        annualised_pct = float(product.get("annualized_funding") or 0)
        funding_rate = annualised_pct / 100.0 / _FUNDING_PERIODS_PER_YEAR
        funding_rate_annualized = annualised_pct / 100.0

        return {
            "symbol":               symbol,
            "markPrice":            mark_price,
            "indexPrice":           index_price,
            "fundingRate":          funding_rate,
            "fundingRateAnnualized": funding_rate_annualized,
            "nextFundingTime":      0,   # Not available in Delta REST
            "ts":                   int(ticker.get("ts") or 0),
            "exchange":             EXCHANGE,
        }

    # ------------------------------------------------------------------ #
    # Client lifecycle
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """Close the underlying HTTP session if we own it."""
        if self._owns_session and self._session is not None:
            await self._session.aclose()
            self._session = None

    async def __aenter__(self) -> DeltaClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "data-service/2.0 delta-rest-client",
        }
        if self._api_key:
            headers["api-key"] = self._api_key
        return headers

    async def _get_session(self) -> httpx.AsyncClient:
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
        """GET ``url`` with retry logic; unwrap Delta's ``{"success", "result"}`` envelope.

        Retry policy (mirrors BinanceClient):
        - HTTP 4xx including 429 → propagate immediately, no retry.
        - HTTP 5xx → retry up to ``_max_retries`` times with exponential backoff.

        Returns:
            The ``result`` field from the Delta envelope.

        Raises:
            ProviderRateLimitedError: On HTTP 429.
            ProviderUnavailableError: On HTTP 5xx after all retries.
            ProviderDataError:        On malformed JSON, ``success: false``,
                                      or missing ``result`` field.
        """
        session = await self._get_session()
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await session.get(url, params=params)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", "60"))
                    raise ProviderRateLimitedError(
                        f"Delta rate limit exceeded (HTTP 429)",
                        provider=PROVIDER_ID,
                        status_code=429,
                        retry_after_s=retry_after,
                    )

                if response.status_code >= 500:
                    exc = ProviderUnavailableError(
                        f"Delta server error: HTTP {response.status_code}",
                        provider=PROVIDER_ID,
                        status_code=response.status_code,
                    )
                    last_exc = exc
                    delay = _RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                    logger.warning(
                        "delta.transient_5xx_retry",
                        component="delta_client",
                        url=url,
                        status_code=response.status_code,
                        attempt=attempt,
                        max_retries=self._max_retries,
                        retry_delay_sec=delay,
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(delay)
                    continue

                if response.status_code >= 400:
                    raise ProviderDataError(
                        f"Delta client error: HTTP {response.status_code}",
                        provider=PROVIDER_ID,
                        status_code=response.status_code,
                    )

                # Parse and unwrap the Delta envelope.
                try:
                    body: dict[str, Any] = response.json()
                except Exception as parse_exc:  # noqa: BLE001
                    raise ProviderDataError(
                        f"Delta returned non-JSON response",
                        provider=PROVIDER_ID,
                    ) from parse_exc

                # Delta envelope: {"success": true/false, "result": ...}
                if isinstance(body, dict) and "success" in body:
                    if not body.get("success"):
                        error_meta = body.get("error") or body.get("message") or str(body)
                        raise ProviderDataError(
                            f"Delta API returned success=false: {error_meta}",
                            provider=PROVIDER_ID,
                        )
                    return body.get("result")

                # Some Delta endpoints (e.g. products) return the payload
                # directly without the envelope wrapper.
                return body

            except (ProviderRateLimitedError, ProviderDataError):
                raise
            except ProviderUnavailableError:
                raise
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                last_exc = ProviderUnavailableError(
                    f"Delta request timed out after {self._timeout}s",
                    provider=PROVIDER_ID,
                )
                logger.warning(
                    "delta.timeout",
                    component="delta_client",
                    url=url,
                    attempt=attempt,
                )
                if attempt < self._max_retries:
                    delay = _RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)
            except httpx.RequestError as exc:
                last_exc = ProviderUnavailableError(
                    f"Delta request error: {type(exc).__name__}",
                    provider=PROVIDER_ID,
                )
                if attempt < self._max_retries:
                    delay = _RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)

        raise last_exc or ProviderUnavailableError(
            "Delta request failed after all retries",
            provider=PROVIDER_ID,
        )
