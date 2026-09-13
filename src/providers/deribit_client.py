"""
Deribit REST provider adapter — Task 12.1.

Implements the Deribit REST API client for fetching crypto options and
futures market data:

- Instruments list (options, futures, spot) per currency
- Order book / option chain data per instrument
- OHLCV candlestick data (TradingView chart data format)
- Index price per currency
- Ticker data (mark price, IV, best bid/ask, open interest)

Key design points
-----------------
* Deribit uses JSON-RPC 2.0 over HTTPS.  Every response is wrapped in::

      {"jsonrpc": "2.0", "id": N, "result": <payload>}

  This client unwraps the ``"result"`` field automatically.  Error
  responses have the form::

      {"jsonrpc": "2.0", "id": N, "error": {"code": N, "message": "..."}}

  These are converted to :class:`ValueError` with the error message.

* The ``3m`` interval ban does **NOT** apply here.  The ban applies only
  to Indian market data.  Deribit crypto data is exempt.

* Retry logic: up to ``max_retries`` attempts on transient HTTP 5xx errors
  with exponential backoff (base 1 s, factor 2×).

* HTTP 4xx (including 429) is raised immediately without retrying.  The
  gateway layer is responsible for honouring ``Retry-After`` and not
  incrementing the circuit-breaker failure counter for 429s.

* OHLCV normalisation: Deribit returns TradingView chart data as parallel
  arrays::

      {
          "ticks":  [<timestamp_ms>, ...],
          "open":   [<price>, ...],
          "high":   [<price>, ...],
          "low":    [<price>, ...],
          "close":  [<price>, ...],
          "volume": [<volume>, ...],
          "status": "ok"
      }

  These are normalised into a list of dicts with keys:
  ``{"time", "open", "high", "low", "close", "volume"}``.

Requirements: 14.1, 14.2, 14.5, 14.8
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from src.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "deribit"

_DEFAULT_BASE_URL = "https://www.deribit.com/api/v2"
_DEFAULT_TIMEOUT_SEC: float = 10.0
_MAX_RETRIES: int = 3
_RETRY_BASE_DELAY_SEC: float = 1.0

# Supported currencies for crypto options / futures.
DERIBIT_CURRENCIES: frozenset[str] = frozenset(["BTC", "ETH", "SOL"])

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _normalise_ohlcv(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert Deribit TradingView chart data to a list of canonical OHLCV dicts.

    Deribit returns parallel arrays keyed by field name::

        {
            "ticks":  [ts_ms, ...],
            "open":   [float, ...],
            "high":   [float, ...],
            "low":    [float, ...],
            "close":  [float, ...],
            "volume": [float, ...],
            "status": "ok"
        }

    The canonical shape returned per candle::

        {
            "time":   int,    # candle open time, UTC epoch ms
            "open":   float,
            "high":   float,
            "low":    float,
            "close":  float,
            "volume": float,
        }

    Args:
        raw: The ``result`` payload from ``/public/get_tradingview_chart_data``.

    Returns:
        List of canonical OHLCV dicts ordered oldest-first.

    Raises:
        ValueError: If the required array fields are absent or mismatched.
    """
    ticks = raw.get("ticks") or []
    opens = raw.get("open") or []
    highs = raw.get("high") or []
    lows = raw.get("low") or []
    closes = raw.get("close") or []
    volumes = raw.get("volume") or []

    lengths = {len(ticks), len(opens), len(highs), len(lows), len(closes), len(volumes)}
    if len(lengths) > 1:
        raise ValueError(
            f"Deribit OHLCV arrays have mismatched lengths: "
            f"ticks={len(ticks)}, open={len(opens)}, high={len(highs)}, "
            f"low={len(lows)}, close={len(closes)}, volume={len(volumes)}"
        )

    return [
        {
            "time":   int(ticks[i]),
            "open":   float(opens[i]),
            "high":   float(highs[i]),
            "low":    float(lows[i]),
            "close":  float(closes[i]),
            "volume": float(volumes[i]),
        }
        for i in range(len(ticks))
    ]


# ---------------------------------------------------------------------------
# DeribitClient
# ---------------------------------------------------------------------------


class DeribitClient:
    """Async REST client for the Deribit API.

    Covers public endpoints used for crypto options analytics:
    instruments list, order book, OHLCV (TradingView format), index price,
    and full ticker data.

    The client can be used as an async context manager::

        async with DeribitClient() as client:
            instruments = await client.get_instruments("BTC", kind="option")

    Or instantiated directly (caller closes)::

        client = DeribitClient()
        price = await client.get_index_price("btc_usd")
        await client.close()

    Note on the ``3m`` interval:
        The 3m ban applies only to Indian market data.  This client does not
        enforce any interval restriction.

    Args:
        base_url:    Deribit API base URL.
                     Defaults to ``"https://www.deribit.com/api/v2"``.
        api_key:     Optional API key for authenticated endpoints.
                     Public endpoints used by this client do not require one.
        api_secret:  Optional API secret (paired with ``api_key``).
        session:     Optional pre-built ``httpx.AsyncClient``.  When supplied,
                     the caller owns its lifecycle — ``close()`` will not
                     close it.
        timeout:     Per-request timeout in seconds.  Defaults to 10.
        max_retries: Number of retry attempts on transient 5xx errors.
                     Defaults to 3.
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
    # Public API
    # ------------------------------------------------------------------ #

    async def get_instruments(
        self,
        currency: str,
        kind: str = "option",
    ) -> list[dict[str, Any]]:
        """Fetch all instruments from ``/public/get_instruments``.

        Returns a list of instrument dicts as returned by Deribit, each
        containing fields such as ``instrument_name``, ``kind``,
        ``strike``, ``option_type``, ``expiration_timestamp``, etc.

        Args:
            currency: Currency code, e.g. ``"BTC"``, ``"ETH"``, ``"SOL"``.
                      Case-insensitive; will be uppercased before request.
            kind:     Instrument kind: ``"option"``, ``"future"``,
                      ``"spot"``, etc.  Defaults to ``"option"``.

        Returns:
            List of instrument dicts.

        Raises:
            ValueError: If Deribit returns an error response.
            httpx.HTTPStatusError: For non-2xx HTTP responses after all retries.
        """
        params: dict[str, Any] = {
            "currency": currency.upper(),
            "kind": kind,
        }
        logger.debug(
            "deribit.get_instruments.start",
            component="deribit_client",
            currency=currency.upper(),
            kind=kind,
        )
        result: list[dict[str, Any]] = await self._get(
            f"{self._base_url}/public/get_instruments", params=params
        )
        logger.debug(
            "deribit.get_instruments.complete",
            component="deribit_client",
            currency=currency.upper(),
            kind=kind,
            count=len(result),
        )
        return result

    async def get_order_book(self, instrument_name: str) -> dict[str, Any]:
        """Fetch order book and option chain data from ``/public/get_order_book``.

        Returns the full order book dict from Deribit, including fields
        such as ``mark_price``, ``index_price``, ``mark_iv``,
        ``underlying_price``, ``open_interest``, ``bids``, ``asks``, etc.

        Null fields (``mark_iv``, ``open_interest``, ``underlying_price``)
        are preserved as-is — no zero substitution is applied.

        Args:
            instrument_name: Deribit instrument name,
                e.g. ``"BTC-27DEC24-100000-C"``.

        Returns:
            Order book dict.

        Raises:
            ValueError: If Deribit returns an error response.
            httpx.HTTPStatusError: For non-2xx HTTP responses after all retries.
        """
        params: dict[str, Any] = {"instrument_name": instrument_name}
        logger.debug(
            "deribit.get_order_book.start",
            component="deribit_client",
            instrument_name=instrument_name,
        )
        return await self._get(  # type: ignore[return-value]
            f"{self._base_url}/public/get_order_book", params=params
        )

    async def get_ohlcv(
        self,
        instrument_name: str,
        resolution: str,
        start_ts: int,
        end_ts: int,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV data from ``/public/get_tradingview_chart_data``.

        Deribit returns data in TradingView format (parallel arrays).  This
        method normalises the response to a list of canonical OHLCV dicts::

            [
                {
                    "time":   int,    # candle open time, UTC epoch ms
                    "open":   float,
                    "high":   float,
                    "low":    float,
                    "close":  float,
                    "volume": float,
                },
                ...
            ]

        Args:
            instrument_name: Deribit instrument name,
                e.g. ``"BTC-PERPETUAL"``.
            resolution:      Candle resolution in minutes as a string, or
                ``"1D"`` / ``"1W"`` / ``"1M"`` for daily/weekly/monthly.
                Deribit supports: ``"1"``, ``"3"``, ``"5"``, ``"10"``,
                ``"15"``, ``"30"``, ``"60"``, ``"120"``, ``"180"``,
                ``"360"``, ``"720"``, ``"1D"``.
            start_ts:        Start timestamp as UTC epoch **milliseconds**.
            end_ts:          End timestamp as UTC epoch **milliseconds**.

        Returns:
            List of canonical OHLCV dicts ordered oldest-first.

        Raises:
            ValueError: If Deribit returns an error response or arrays are
                mismatched.
            httpx.HTTPStatusError: For non-2xx HTTP responses after all retries.
        """
        params: dict[str, Any] = {
            "instrument_name": instrument_name,
            "resolution": resolution,
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
        }
        logger.debug(
            "deribit.get_ohlcv.start",
            component="deribit_client",
            instrument_name=instrument_name,
            resolution=resolution,
        )
        raw: dict[str, Any] = await self._get(
            f"{self._base_url}/public/get_tradingview_chart_data", params=params
        )
        candles = _normalise_ohlcv(raw)
        logger.debug(
            "deribit.get_ohlcv.complete",
            component="deribit_client",
            instrument_name=instrument_name,
            resolution=resolution,
            count=len(candles),
        )
        return candles

    async def get_index_price(self, index_name: str) -> dict[str, Any]:
        """Fetch index price from ``/public/get_index_price``.

        Returns a dict with ``"index_name"`` and ``"index_price"`` keys.

        Args:
            index_name: Deribit index name, e.g. ``"btc_usd"``,
                ``"eth_usd"``, ``"sol_usd"``.

        Returns:
            Dict with ``index_name`` (str) and ``index_price`` (float).

        Raises:
            ValueError: If Deribit returns an error response.
            httpx.HTTPStatusError: For non-2xx HTTP responses after all retries.
        """
        params: dict[str, Any] = {"index_name": index_name}
        logger.debug(
            "deribit.get_index_price.start",
            component="deribit_client",
            index_name=index_name,
        )
        result: dict[str, Any] = await self._get(
            f"{self._base_url}/public/get_index_price", params=params
        )
        # Attach index_name to the result for caller convenience (Deribit
        # does not always include it in the result object).
        if "index_name" not in result:
            result = dict(result)
            result["index_name"] = index_name
        return result

    async def get_ticker(self, instrument_name: str) -> dict[str, Any]:
        """Fetch ticker data from ``/public/ticker``.

        Returns the full ticker dict from Deribit, including:
        ``mark_price``, ``mark_iv`` (IV, null when not available),
        ``best_bid_price``, ``best_ask_price``,
        ``open_interest`` (null when not available),
        ``last_price``, ``index_price``, ``underlying_price``,
        ``timestamp``, etc.

        Null fields are preserved — no zero substitution is applied
        (Requirement 14.5).

        Args:
            instrument_name: Deribit instrument name,
                e.g. ``"BTC-27DEC24-100000-C"``.

        Returns:
            Ticker dict.

        Raises:
            ValueError: If Deribit returns an error response.
            httpx.HTTPStatusError: For non-2xx HTTP responses after all retries.
        """
        params: dict[str, Any] = {"instrument_name": instrument_name}
        logger.debug(
            "deribit.get_ticker.start",
            component="deribit_client",
            instrument_name=instrument_name,
        )
        return await self._get(  # type: ignore[return-value]
            f"{self._base_url}/public/ticker", params=params
        )

    # ------------------------------------------------------------------ #
    # Client lifecycle
    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        """Close the underlying HTTP session if we own it."""
        if self._owns_session and self._session is not None:
            await self._session.aclose()
            self._session = None

    async def __aenter__(self) -> DeribitClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_headers(self) -> dict[str, str]:
        """Build base request headers."""
        return {
            "Accept": "application/json",
            "User-Agent": "data-service/2.0 deribit-rest-client",
        }

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
        """Perform a GET request, unwrapping the JSON-RPC result.

        Deribit wraps every response in a JSON-RPC 2.0 envelope::

            {"jsonrpc": "2.0", "id": N, "result": <payload>}

        On success, this method returns the unwrapped ``result`` value.

        On a Deribit application error::

            {"jsonrpc": "2.0", "id": N, "error": {"code": N, "message": "..."}}

        A :class:`ValueError` is raised with the Deribit error message.

        Retry policy (matches BinanceClient):
        - HTTP 4xx (including 429): raised immediately, no retry.
        - HTTP 5xx: retried up to ``_max_retries`` times with exponential
          backoff (base ``_RETRY_BASE_DELAY_SEC``, factor 2×).

        Args:
            url:    Full URL to GET.
            params: Optional query parameters.

        Returns:
            The ``result`` field from the Deribit JSON-RPC response.

        Raises:
            ValueError: For Deribit application errors (``error`` key present).
            httpx.HTTPStatusError: For non-2xx HTTP responses after all retries.
        """
        session = await self._get_session()
        last_exc: httpx.HTTPStatusError | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await session.get(url, params=params)
                response.raise_for_status()

                body: dict[str, Any] = response.json()

                # Deribit application-level error (HTTP 200 with error payload).
                if "error" in body:
                    error = body["error"]
                    code = error.get("code", "unknown")
                    message = error.get("message", str(error))
                    logger.warning(
                        "deribit.api_error",
                        component="deribit_client",
                        url=url,
                        error_code=code,
                        error_message=message,
                    )
                    raise ValueError(
                        f"Deribit API error {code}: {message}"
                    )

                return body.get("result")

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code

                # 4xx and 429 — do not retry; propagate immediately.
                if status < 500 or status == 429:
                    logger.warning(
                        "deribit.http_error",
                        component="deribit_client",
                        url=url,
                        status_code=status,
                        attempt=attempt,
                    )
                    raise

                # 5xx — transient; retry with backoff.
                last_exc = exc
                delay = _RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1))
                logger.warning(
                    "deribit.transient_5xx_retry",
                    component="deribit_client",
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
            "deribit.all_retries_exhausted",
            component="deribit_client",
            url=url,
            max_retries=self._max_retries,
        )
        raise last_exc
