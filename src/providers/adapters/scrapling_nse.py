"""
Scrapling/NSE provider adapter for DATA-SERVICE 2.0.

This adapter is the **only** component that may contact NSE endpoints directly.
It is credential-free and all responses are classified with
``SourceType.OPEN_SOURCE_NSE_DERIVED``.

Responsibilities
----------------
- Fetch live equity/F&O/index quotes from NSE's public JSON API
- Fetch the full NSE option chain snapshot for a given underlying + expiry
- Fetch the NSE instrument master (CSV download / JSON listing)

HTTP strategy
-------------
``curl_cffi`` is used for all outbound HTTP calls with Chrome TLS-fingerprint
impersonation to bypass NSE's WAF.  ``scrapling.Selector`` is used if any
response requires HTML parsing (the NSE charting endpoints return JSON, but
some legacy instrument-master paths return HTML-wrapped data).

Normalisation is **not** done here.  The adapter returns raw dicts; the
Validation Pipeline's Normaliser (task 5.2) maps them to canonical schemas.

Rate limits
-----------
NSE blocks aggressive crawlers.  The adapter enforces a token-bucket at
2 req/s, consistent with the Capability_Matrix entry for ``scrapling_nse``
(Requirement 5.1).  A small jitter is added between consecutive calls to
avoid patterns that trigger WAF rules.

Error mapping
-------------
| HTTP status | Exception raised               | Notes                          |
|-------------|-------------------------------|--------------------------------|
| 403         | ProviderAuthError             | WAF block / geo restriction    |
| 429         | ProviderRateLimitedError      | Retry-After header honoured    |
| 503 / 5xx   | ProviderUnavailableError      | Temporary upstream failure     |
| conn. error | ProviderUnavailableError      | Timeout, DNS failure, etc.     |
| bad JSON    | ProviderDataError             | Malformed response body        |
| empty data  | ProviderMarketClosedError     | Closed-market empty payload    |

Requirements: 5.9, 22.1
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any, Optional
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession, BrowserType

from src.core.schemas.provider import SourceType
from src.observability.logging import get_logger
from src.providers.adapters.base import (
    ProviderAuthError,
    ProviderDataError,
    ProviderMarketClosedError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDER_NAME = "scrapling_nse"

# NSE REST API base URL (all public JSON endpoints live under this host)
_NSE_BASE_URL = "https://www.nseindia.com"

# NSE quote endpoint — returns LTP, OHLC, circuit limits, etc.
_QUOTE_URL = f"{_NSE_BASE_URL}/api/quote-equity"

# NSE F&O quote endpoint (derivatives)
_QUOTE_DERIV_URL = f"{_NSE_BASE_URL}/api/quote-derivative"

# NSE option chain endpoint
_OPTION_CHAIN_URL = f"{_NSE_BASE_URL}/api/option-chain-indices"

# NSE equity option chain endpoint (for stock options)
_OPTION_CHAIN_EQUITIES_URL = f"{_NSE_BASE_URL}/api/option-chain-equities"

# NSE all-equity CSV / JSON listing used to bootstrap the instrument master
_EQUITY_MASTER_URL = f"{_NSE_BASE_URL}/api/master-quote"

# Indices listing — used to populate IDX instruments
_INDICES_URL = f"{_NSE_BASE_URL}/api/allIndices"

# F&O ban list — used to identify derivative instruments
_FNO_BAN_URL = f"{_NSE_BASE_URL}/api/live-analysis-data?index=foBanList"

# Seed URL — NSE requires a valid session cookie obtained from the homepage
# before hitting JSON endpoints; a GET to the homepage seeds the cookie jar.
_NSE_HOMEPAGE_URL = f"{_NSE_BASE_URL}/"

# ---------------------------------------------------------------------------
# Browser-like headers
# ---------------------------------------------------------------------------

NSE_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "DNT": "1",
    "Pragma": "no-cache",
    "Referer": "https://www.nseindia.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

# JSON-specific headers used after the session cookie is seeded
NSE_JSON_HEADERS: dict[str, str] = {
    **NSE_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "X-Requested-With": "XMLHttpRequest",
}

# ---------------------------------------------------------------------------
# Index sets — needed to route to the right option-chain endpoint
# ---------------------------------------------------------------------------

# Indices for which NSE exposes the *indices* option-chain endpoint
_INDEX_UNDERLYINGS = frozenset(
    {
        "NIFTY",
        "BANKNIFTY",
        "FINNIFTY",
        "MIDCPNIFTY",
        "NIFTYNXT50",
    }
)

# ---------------------------------------------------------------------------
# ScraplingNseAdapter
# ---------------------------------------------------------------------------


class ScraplingNseAdapter:
    """Credential-free adapter for NSE public API endpoints.

    Uses ``curl_cffi`` with Chrome TLS-fingerprint impersonation for all HTTP
    requests.  ``scrapling.Selector`` is available via ``_parse_html`` for any
    response that requires HTML parsing.

    All responses are tagged with ``SourceType.OPEN_SOURCE_NSE_DERIVED``.
    The adapter returns raw dicts; normalisation is the pipeline's concern.

    Args:
        requests_per_second: Token-bucket refill rate.  Defaults to 2.0 to
            match the Capability_Matrix value (Requirement 5.1).
        connect_timeout:     Seconds to wait for a connection.
        read_timeout:        Seconds to wait for response data.
        max_retries:         Number of times to retry transient failures
                             (excludes 429 and 403).
    """

    # Source type for all data from this adapter (Requirement 5.9)
    SOURCE_TYPE: SourceType = SourceType.OPEN_SOURCE_NSE_DERIVED

    def __init__(
        self,
        *,
        requests_per_second: float = 2.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 20.0,
        max_retries: int = 2,
    ) -> None:
        self._rps = requests_per_second
        self._min_interval = 1.0 / requests_per_second  # seconds between calls
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_retries = max_retries

        # Shared curl_cffi async session — reuses the TLS connection and
        # maintains the NSE cookie jar across calls.
        self._session: Optional[AsyncSession] = None

        # Tracks the timestamp of the last outbound request (monotonic clock)
        # for lightweight client-side rate enforcement.
        self._last_request_time: float = 0.0

        # Set to True once the NSE homepage has been fetched to seed the
        # session cookie (NSE requires this before JSON endpoints respond).
        self._session_seeded: bool = False

    # ------------------------------------------------------------------ #
    # Session management
    # ------------------------------------------------------------------ #

    async def _get_session(self) -> AsyncSession:
        """Return the shared AsyncSession, creating it on first call."""
        if self._session is None:
            self._session = AsyncSession(
                impersonate=BrowserType.chrome131,
                headers=NSE_JSON_HEADERS,
                timeout=self._connect_timeout + self._read_timeout,
                verify=True,
            )
        return self._session

    async def _ensure_seeded(self) -> None:
        """Fetch the NSE homepage once to obtain a valid session cookie.

        NSE's JSON endpoints reject requests that lack a valid ``nsit`` /
        ``nseappid`` cookie.  A single GET to the homepage populates the
        cookie jar for the lifetime of the session.
        """
        if self._session_seeded:
            return
        session = await self._get_session()
        try:
            resp = await session.get(
                _NSE_HOMEPAGE_URL,
                headers=NSE_HEADERS,
                timeout=self._connect_timeout + self._read_timeout,
            )
            if resp.status_code < 500:
                self._session_seeded = True
                logger.debug(
                    "nse_session_seeded",
                    component=_PROVIDER_NAME,
                    status_code=resp.status_code,
                )
            else:
                logger.warning(
                    "nse_session_seed_failed",
                    component=_PROVIDER_NAME,
                    status_code=resp.status_code,
                )
        except Exception as exc:
            logger.warning(
                "nse_session_seed_error",
                component=_PROVIDER_NAME,
                error=str(exc),
            )
            # Not fatal — some environments can reach JSON endpoints without
            # an explicit cookie; proceed and let the actual request fail if
            # needed.

    async def close(self) -> None:
        """Close the underlying HTTP session and release resources."""
        if self._session is not None:
            await self._session.close()
            self._session = None
            self._session_seeded = False

    # ------------------------------------------------------------------ #
    # Rate limiting
    # ------------------------------------------------------------------ #

    async def _throttle(self) -> None:
        """Enforce the client-side rate limit with a small random jitter.

        Sleeps for the remaining time needed to respect ``_min_interval``
        between consecutive calls, plus a random jitter of up to 10% of the
        interval to avoid synchronised burst patterns.
        """
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        wait_time = self._min_interval - elapsed
        if wait_time > 0:
            # Add up to 10% jitter on top of the base wait
            jitter = random.uniform(0, self._min_interval * 0.1)
            await asyncio.sleep(wait_time + jitter)
        self._last_request_time = asyncio.get_event_loop().time()

    # ------------------------------------------------------------------ #
    # Core HTTP request helper
    # ------------------------------------------------------------------ #

    async def _get(
        self,
        url: str,
        *,
        params: Optional[dict[str, str]] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Perform a GET request to an NSE endpoint and return parsed JSON.

        Applies rate limiting, handles all error-to-exception mapping, and
        retries transient failures up to ``_max_retries`` times.

        Args:
            url:           Full URL to fetch.
            params:        Query-string parameters (URL-encoded by curl_cffi).
            extra_headers: Additional headers to merge into ``NSE_JSON_HEADERS``.

        Returns:
            Parsed JSON response as a ``dict``.

        Raises:
            ProviderAuthError:         HTTP 403.
            ProviderRateLimitedError:  HTTP 429.
            ProviderUnavailableError:  HTTP 5xx or connection error.
            ProviderDataError:         Non-JSON or malformed response body.
        """
        await self._ensure_seeded()
        session = await self._get_session()

        headers = dict(NSE_JSON_HEADERS)
        if extra_headers:
            headers.update(extra_headers)

        last_exc: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 2):  # +2: 1 base + N retries
            await self._throttle()

            try:
                resp = await session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self._connect_timeout + self._read_timeout,
                )
            except Exception as conn_exc:
                logger.warning(
                    "nse_connection_error",
                    component=_PROVIDER_NAME,
                    url=url,
                    attempt=attempt,
                    error=str(conn_exc),
                )
                last_exc = ProviderUnavailableError(
                    f"Connection error fetching {url}: {conn_exc}",
                    provider=_PROVIDER_NAME,
                    status_code=None,
                )
                if attempt <= self._max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                continue

            status = resp.status_code

            # ---- HTTP error mapping ----------------------------------------
            if status == 403:
                raise ProviderAuthError(
                    f"NSE returned HTTP 403 for {url} — WAF block or geo restriction",
                    provider=_PROVIDER_NAME,
                    status_code=403,
                )

            if status == 429:
                retry_after: Optional[int] = None
                raw_ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                if raw_ra:
                    with __import__("contextlib").suppress(ValueError):
                        retry_after = int(raw_ra)
                raise ProviderRateLimitedError(
                    f"NSE returned HTTP 429 for {url}",
                    provider=_PROVIDER_NAME,
                    status_code=429,
                    retry_after_s=retry_after,
                )

            if status >= 500:
                logger.warning(
                    "nse_server_error",
                    component=_PROVIDER_NAME,
                    url=url,
                    status_code=status,
                    attempt=attempt,
                )
                last_exc = ProviderUnavailableError(
                    f"NSE returned HTTP {status} for {url}",
                    provider=_PROVIDER_NAME,
                    status_code=status,
                )
                if attempt <= self._max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                continue

            # ---- Response body parsing -------------------------------------
            content_type = resp.headers.get("content-type", "")
            raw_body = resp.content

            if not raw_body:
                raise ProviderDataError(
                    f"Empty response body from {url}",
                    provider=_PROVIDER_NAME,
                    status_code=status,
                )

            if "application/json" in content_type or _looks_like_json(raw_body):
                try:
                    data = json.loads(raw_body)
                except json.JSONDecodeError as je:
                    raise ProviderDataError(
                        f"Invalid JSON from {url}: {je}",
                        provider=_PROVIDER_NAME,
                        status_code=status,
                    ) from je
                return data  # type: ignore[return-value]

            # HTML content — parse with Scrapling.Selector if needed
            return {"_raw_html": raw_body.decode("utf-8", errors="replace")}

        # All attempts exhausted
        raise last_exc or ProviderUnavailableError(
            f"All {self._max_retries + 1} attempts failed for {url}",
            provider=_PROVIDER_NAME,
        )

    # ------------------------------------------------------------------ #
    # HTML parsing helper
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_html(html: str) -> Any:
        """Parse HTML content using ``scrapling.Selector``.

        ``scrapling.Selector`` is the lightweight HTML-parsing layer of the
        Scrapling library.  It does not require a headless browser and is safe
        to use in async contexts.

        Args:
            html: Raw HTML string.

        Returns:
            A ``scrapling.Selector`` instance with the full parsed DOM.
        """
        try:
            from scrapling import Selector  # noqa: PLC0415 — optional dep
            return Selector(html)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "scrapling_selector_unavailable",
                component=_PROVIDER_NAME,
                error=str(exc),
            )
            return None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def fetch_live_quote(
        self,
        symbol: str,
        exchange: str = "NSE",
    ) -> dict[str, Any]:
        """Fetch a live quote for an NSE equity or derivative instrument.

        Routes the request to the correct NSE endpoint based on ``exchange``:
        - ``NSE`` / equity → ``/api/quote-equity?symbol=<SYMBOL>``
        - ``NFO`` / derivatives → ``/api/quote-derivative?symbol=<SYMBOL>``

        The returned dict is the raw NSE JSON payload.  Fields include (but
        are not limited to): ``priceInfo`` (ltp, open, high, low, pChange,
        totalTradedVolume), ``securityWiseDP`` (upper/lower circuit), and
        ``metadata`` (instrumentType, isin).

        Args:
            symbol:   NSE trading symbol, e.g. ``"NIFTY"`` or ``"RELIANCE"``.
            exchange: Exchange code — ``"NSE"`` for equities/indices,
                      ``"NFO"`` for F&O instruments.

        Returns:
            Raw quote dict from NSE.

        Raises:
            ProviderAuthError:         HTTP 403 (WAF block).
            ProviderRateLimitedError:  HTTP 429.
            ProviderUnavailableError:  HTTP 5xx or connection error.
            ProviderDataError:         Non-JSON or empty response.
            ProviderMarketClosedError: NSE returns an empty/closed payload.
        """
        exchange_upper = exchange.upper()
        if exchange_upper == "NFO":
            url = _QUOTE_DERIV_URL
        else:
            url = _QUOTE_URL

        logger.debug(
            "nse_fetch_live_quote",
            component=_PROVIDER_NAME,
            symbol=symbol,
            exchange=exchange_upper,
        )

        data = await self._get(url, params={"symbol": symbol})

        # NSE returns {"info": {}, "metadata": {}, "priceInfo": {}, ...}
        # An empty dict or a payload with no priceInfo indicates market closed.
        if not data or "priceInfo" not in data:
            logger.info(
                "nse_market_closed_or_empty",
                component=_PROVIDER_NAME,
                symbol=symbol,
            )
            raise ProviderMarketClosedError(
                f"NSE returned empty quote data for {symbol!r} — market may be closed",
                provider=_PROVIDER_NAME,
            )

        # Attach source type metadata for the pipeline
        data["_sourceType"] = SourceType.OPEN_SOURCE_NSE_DERIVED.value
        data["_provider"] = _PROVIDER_NAME

        logger.debug(
            "nse_live_quote_received",
            component=_PROVIDER_NAME,
            symbol=symbol,
            ltp=data.get("priceInfo", {}).get("lastPrice"),
        )

        return data

    async def fetch_option_chain(
        self,
        underlying: str,
        expiry: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch the full NSE option chain snapshot for an underlying.

        Routes to the index or equity option-chain endpoint based on whether
        ``underlying`` is a known NSE index (NIFTY, BANKNIFTY, etc.).

        The returned dict is the raw NSE JSON payload containing a ``filtered``
        block with ``data`` (list of strikes × expiries) and ``PE`` / ``CE``
        aggregate stats.

        Args:
            underlying: The underlying symbol — e.g. ``"NIFTY"``, ``"RELIANCE"``.
            expiry:     Optional ISO-8601 date string (``"YYYY-MM-DD"``) to
                        pre-filter the response on the client side.  NSE's API
                        does not support server-side expiry filtering; the full
                        chain is always downloaded.

        Returns:
            Raw option chain dict from NSE, with ``_sourceType``, ``_provider``,
            and optional ``_requestedExpiry`` metadata fields appended.

        Raises:
            ProviderAuthError:         HTTP 403.
            ProviderRateLimitedError:  HTTP 429.
            ProviderUnavailableError:  HTTP 5xx or connection error.
            ProviderDataError:         Non-JSON or empty response.
            ProviderMarketClosedError: NSE returns an empty option chain.
        """
        underlying_upper = underlying.upper()
        if underlying_upper in _INDEX_UNDERLYINGS:
            url = _OPTION_CHAIN_URL
        else:
            url = _OPTION_CHAIN_EQUITIES_URL

        logger.debug(
            "nse_fetch_option_chain",
            component=_PROVIDER_NAME,
            underlying=underlying_upper,
            expiry=expiry,
        )

        data = await self._get(url, params={"symbol": underlying_upper})

        # NSE option chain has a "filtered" → "data" list for strikes
        if not data or not data.get("filtered", {}).get("data"):
            logger.info(
                "nse_option_chain_empty",
                component=_PROVIDER_NAME,
                underlying=underlying_upper,
            )
            raise ProviderMarketClosedError(
                f"NSE returned empty option chain for {underlying_upper!r} "
                "— market may be closed or no contracts listed",
                provider=_PROVIDER_NAME,
            )

        data["_sourceType"] = SourceType.OPEN_SOURCE_NSE_DERIVED.value
        data["_provider"] = _PROVIDER_NAME
        if expiry is not None:
            data["_requestedExpiry"] = expiry

        logger.debug(
            "nse_option_chain_received",
            component=_PROVIDER_NAME,
            underlying=underlying_upper,
            records=len(data.get("filtered", {}).get("data", [])),
        )

        return data

    async def fetch_instrument_master(self) -> list[dict[str, Any]]:
        """Fetch the NSE instrument master listing.

        Retrieves the full list of NSE-listed instruments from two endpoints:
        1. ``/api/allIndices`` — index instruments (NIFTY, BANKNIFTY, etc.)
        2. ``/api/master-quote`` — equity instruments

        The two lists are merged and returned as a flat list of dicts.  Each
        dict represents one instrument with fields such as ``symbol``,
        ``isin``, ``companyName``, ``series``, and ``refData``.

        Metadata fields ``_sourceType`` and ``_provider`` are injected into
        each row for traceability.

        Returns:
            List of raw instrument dicts from NSE.

        Raises:
            ProviderAuthError:         HTTP 403.
            ProviderRateLimitedError:  HTTP 429.
            ProviderUnavailableError:  HTTP 5xx or connection error.
            ProviderDataError:         Non-JSON or empty response.
        """
        logger.debug("nse_fetch_instrument_master", component=_PROVIDER_NAME)

        instruments: list[dict[str, Any]] = []

        # ---- Equity master -------------------------------------------------
        try:
            eq_data = await self._get(_EQUITY_MASTER_URL)
            eq_list: list[dict[str, Any]] = []

            # NSE returns different shapes depending on the endpoint variant
            if isinstance(eq_data, list):
                eq_list = eq_data
            elif isinstance(eq_data, dict):
                # Try common wrapper keys
                for key in ("data", "result", "symbols", "items"):
                    if key in eq_data and isinstance(eq_data[key], list):
                        eq_list = eq_data[key]
                        break

            for row in eq_list:
                row["_sourceType"] = SourceType.OPEN_SOURCE_NSE_DERIVED.value
                row["_provider"] = _PROVIDER_NAME
                row["_instrumentCategory"] = "EQ"
            instruments.extend(eq_list)

        except (ProviderDataError, ProviderUnavailableError, ProviderAuthError) as exc:
            logger.warning(
                "nse_equity_master_failed",
                component=_PROVIDER_NAME,
                error=str(exc),
            )

        # ---- Index master --------------------------------------------------
        try:
            idx_data = await self._get(_INDICES_URL)
            idx_list: list[dict[str, Any]] = []

            if isinstance(idx_data, list):
                idx_list = idx_data
            elif isinstance(idx_data, dict):
                for key in ("data", "result", "indexDetailsList"):
                    if key in idx_data and isinstance(idx_data[key], list):
                        idx_list = idx_data[key]
                        break

            for row in idx_list:
                row["_sourceType"] = SourceType.OPEN_SOURCE_NSE_DERIVED.value
                row["_provider"] = _PROVIDER_NAME
                row["_instrumentCategory"] = "IDX"
            instruments.extend(idx_list)

        except (ProviderDataError, ProviderUnavailableError, ProviderAuthError) as exc:
            logger.warning(
                "nse_index_master_failed",
                component=_PROVIDER_NAME,
                error=str(exc),
            )

        if not instruments:
            raise ProviderDataError(
                "NSE instrument master fetch returned no instruments from any endpoint",
                provider=_PROVIDER_NAME,
            )

        logger.info(
            "nse_instrument_master_received",
            component=_PROVIDER_NAME,
            count=len(instruments),
        )

        return instruments


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def _looks_like_json(raw: bytes) -> bool:
    """Return True if ``raw`` appears to start with a JSON object or array.

    Used as a fallback when the ``Content-Type`` header is missing or
    incorrect (NSE occasionally serves ``text/html`` with a JSON body).
    """
    stripped = raw.lstrip()
    return stripped[:1] in (b"{", b"[")
