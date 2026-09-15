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
impersonation (Chrome 131) to bypass NSE's Akamai WAF.

NSE WAF behavior (as of Sep 2026)
-----------------------------------
NSE uses Akamai Bot Manager with both TLS fingerprinting AND JavaScript
challenges.  The following endpoints are accessible without JS execution:
  - ``/api/marketStatus``   — market open/closed status ✅
  - ``/api/allIndices``     — all index live data (NIFTY, BANKNIFTY, etc.) ✅

The following endpoints require ``nseappid`` / ``nsit`` cookies that are set
by Akamai's JavaScript challenge and are NOT obtainable without a real browser
or JS engine:
  - ``/api/quote-equity``         — equity live quotes ❌ (requires JS cookies)
  - ``/api/option-chain-indices`` — option chain ❌ (moved/removed)
  - ``/api/quote-derivative``     — F&O quotes ❌ (requires JS cookies)

Strategy:
  1. ``allIndices`` gives live prices for all 139+ NSE indices (NIFTY, BANKNIFTY,
     FINNIFTY, etc.) without JS.  Index quotes are served from this endpoint.
  2. For equity quotes, we fall through to a 403 with a clear error.
  3. Session warm-up (homepage + option-chain page) is kept to maximize
     the chance that future NSE endpoint changes are handled gracefully.

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
# NOTE: As of Sep 2026, this requires Akamai JS-challenge cookies (nseappid).
# Returns HTTP 403 without a real browser session.
_QUOTE_URL = f"{_NSE_BASE_URL}/api/quote-equity"

# NSE F&O quote endpoint (derivatives) — same JS-challenge restriction
_QUOTE_DERIV_URL = f"{_NSE_BASE_URL}/api/quote-derivative"

# NSE option chain endpoints — moved/removed as of Sep 2026
_OPTION_CHAIN_URL = f"{_NSE_BASE_URL}/api/option-chain-indices"
_OPTION_CHAIN_EQUITIES_URL = f"{_NSE_BASE_URL}/api/option-chain-equities"

# Working endpoints (no JS challenge required) ─────────────────────────────

# All indices live data — returns NIFTY 50, NIFTY BANK, FINNIFTY, etc.
# This endpoint returns live prices for 139+ indices without JS cookies.
# Also used in fetch_instrument_master() to populate the IDX instrument list.
_ALL_INDICES_URL = f"{_NSE_BASE_URL}/api/allIndices"

# Market status — always accessible
_MARKET_STATUS_URL = f"{_NSE_BASE_URL}/api/marketStatus"

# NSE all-equity CSV / JSON listing used to bootstrap the instrument master
_EQUITY_MASTER_URL = f"{_NSE_BASE_URL}/api/master-quote"

# F&O ban list — used to identify derivative instruments
_FNO_BAN_URL = f"{_NSE_BASE_URL}/api/live-analysis-data?index=foBanList"

# Seed URL — NSE requires a valid session cookie obtained from the homepage
# before hitting JSON endpoints; a GET to the homepage seeds the cookie jar.
_NSE_HOMEPAGE_URL = f"{_NSE_BASE_URL}/"

# Option chain page — visiting this warms up the session for option data
_NSE_OC_PAGE_URL = f"{_NSE_BASE_URL}/option-chain"

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
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "DNT": "1",
    "Pragma": "no-cache",
    "Referer": "https://www.nseindia.com/",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
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
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Referer": "https://www.nseindia.com/",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
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
        """Fetch the NSE homepage and option-chain page to obtain session cookies.

        NSE uses Akamai Bot Manager.  A multi-step warm-up is required:
          1. Homepage GET (seeds _abck, ak_bmsc, bm_sz Akamai cookies)
          2. Option-chain page GET (seeds bm_sv, bm_mi cookies)

        Even with these cookies, the ``quote-equity`` endpoint returns 403
        because it additionally requires the ``nseappid`` cookie that is set
        by Akamai's JavaScript challenge (unavailable without a real browser).

        The warm-up remains valuable because it maximises the chance of
        accessing other endpoints (allIndices, marketStatus) that do not
        require the JS cookie.
        """
        if self._session_seeded:
            return
        session = await self._get_session()
        html_headers = {
            **NSE_JSON_HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        html_headers.pop("X-Requested-With", None)

        # Step 1: Homepage (Akamai sets _abck, ak_bmsc, bm_sz)
        try:
            resp1 = await session.get(
                _NSE_HOMEPAGE_URL,
                headers=html_headers,
                timeout=self._connect_timeout + self._read_timeout,
            )
            if resp1.status_code < 500:
                logger.debug(
                    "nse_homepage_seeded",
                    component=_PROVIDER_NAME,
                    status_code=resp1.status_code,
                    cookie_count=len(session.cookies),
                )
        except Exception as exc:
            logger.warning("nse_homepage_seed_error", component=_PROVIDER_NAME, error=str(exc))

        # Step 2: Option-chain page (adds bm_sv, bm_mi cookies)
        await asyncio.sleep(1.2)
        try:
            nav_headers = {**html_headers, "Sec-Fetch-Site": "same-origin",
                           "Referer": _NSE_HOMEPAGE_URL}
            await session.get(
                _NSE_OC_PAGE_URL,
                headers=nav_headers,
                timeout=self._connect_timeout + self._read_timeout,
            )
        except Exception as exc:
            logger.debug("nse_oc_page_seed_error", component=_PROVIDER_NAME, error=str(exc))

        await asyncio.sleep(0.8)
        self._session_seeded = True
        logger.info(
            "nse_session_seeded",
            component=_PROVIDER_NAME,
            cookie_count=len(session.cookies),
            cookie_names=list(session.cookies.keys()),
        )

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

    async def fetch_all_indices(self) -> dict[str, dict]:
        """Fetch live data for all NSE indices from /api/allIndices.

        This endpoint is accessible without Akamai JS cookies and returns
        live prices for 139+ indices including NIFTY 50, NIFTY BANK,
        FINNIFTY, NIFTY IT, MIDCPNIFTY, INDIA VIX, etc.

        Returns:
            Dict keyed by index name → raw index dict.
            Each dict contains: ``last`` (LTP), ``percentChange``,
            ``open``, ``high``, ``low``, ``previousClose``, ``yearHigh``,
            ``yearLow``, ``pe``, ``advance``, ``decline``, ``unchanged``.

        Raises:
            ProviderUnavailableError: Network or HTTP error.
            ProviderDataError:        Malformed response.
        """
        data = await self._get(_ALL_INDICES_URL)
        indices_list = data.get("data", [])
        if not isinstance(indices_list, list):
            raise ProviderDataError(
                "NSE allIndices response 'data' is not a list",
                provider=_PROVIDER_NAME,
            )
        result: dict[str, dict] = {}
        for entry in indices_list:
            name = entry.get("index") or entry.get("indexSymbol", "")
            if name:
                entry["_sourceType"] = SourceType.OPEN_SOURCE_NSE_DERIVED.value
                entry["_provider"] = _PROVIDER_NAME
                result[name] = entry

        logger.debug(
            "nse_all_indices_received",
            component=_PROVIDER_NAME,
            count=len(result),
        )
        return result

    async def fetch_live_quote(
        self,
        symbol: str,
        exchange: str = "NSE",
    ) -> dict[str, Any]:
        """Fetch a live quote for an NSE equity or derivative instrument.

        Routing:
        - Index symbols (NIFTY, BANKNIFTY, FINNIFTY, etc.) → ``/api/allIndices``
          This works reliably without JS cookies.
        - Equity symbols → ``/api/quote-equity`` (requires Akamai JS cookies;
          returns HTTP 403 without a real browser session as of Sep 2026).
        - NFO symbols → ``/api/quote-derivative`` (same JS restriction).

        The returned dict is the raw NSE JSON payload.

        Args:
            symbol:   NSE trading symbol.
            exchange: ``"NSE"`` for equities/indices, ``"NFO"`` for F&O.

        Returns:
            Raw quote dict with ``priceInfo`` for equities/indices or the
            raw allIndices entry for index symbols.

        Raises:
            ProviderAuthError:         HTTP 403 (WAF block — JS cookies needed).
            ProviderRateLimitedError:  HTTP 429.
            ProviderUnavailableError:  HTTP 5xx or connection error.
            ProviderDataError:         Non-JSON or empty response.
            ProviderMarketClosedError: NSE returns an empty/closed payload.
        """
        exchange_upper = exchange.upper()
        symbol_upper = symbol.upper()

        # ── Index fast path via allIndices (no JS cookies needed) ────────
        # Map common trading symbols to allIndices "index" field names
        _INDEX_SYMBOL_MAP: dict[str, str] = {
            "NIFTY":       "NIFTY 50",
            "NIFTY50":     "NIFTY 50",
            "NIFTY 50":    "NIFTY 50",
            "BANKNIFTY":   "NIFTY BANK",
            "NIFTY BANK":  "NIFTY BANK",
            "FINNIFTY":    "NIFTY FINANCIAL SERVICES",
            "NIFTY FIN SERVICE": "NIFTY FINANCIAL SERVICES",
            "MIDCPNIFTY":  "NIFTY MIDCAP SELECT",
            "NIFTY MIDCAP SELECT": "NIFTY MIDCAP SELECT",
            "NIFTYNXT50":  "NIFTY NEXT 50",
            "NIFTY NEXT 50": "NIFTY NEXT 50",
            "INDIAVIX":    "INDIA VIX",
            "INDIA VIX":   "INDIA VIX",
            "NIFTY IT":    "NIFTY IT",
            "NIFTYIT":     "NIFTY IT",
            "NIFTY AUTO":  "NIFTY AUTO",
            "NIFTYAUTO":   "NIFTY AUTO",
            "NIFTY FMCG":  "NIFTY FMCG",
            "NIFTYFMCG":   "NIFTY FMCG",
            "NIFTY PHARMA": "NIFTY PHARMA",
            "NIFTY REALTY": "NIFTY REALTY",
            "NIFTY METAL":  "NIFTY METAL",
            "NIFTY PSU BANK": "NIFTY PSU BANK",
            "NIFTY ENERGY": "NIFTY ENERGY",
            "NIFTY INFRA":  "NIFTY INDIA CONSUMPTION",
        }
        index_name = _INDEX_SYMBOL_MAP.get(symbol_upper)

        if index_name or symbol_upper in _INDEX_UNDERLYINGS:
            lookup = index_name or f"NIFTY {symbol_upper}" if symbol_upper not in ("NIFTY 50",) else symbol_upper
            all_indices = await self.fetch_all_indices()
            entry = all_indices.get(lookup) or all_indices.get(symbol_upper)
            # Fuzzy match: try partial name match
            if entry is None:
                for k, v in all_indices.items():
                    if symbol_upper in k.upper() or k.upper() in symbol_upper:
                        entry = v
                        break
            if entry is not None:
                ltp = entry.get("last") or entry.get("lastPrice")
                # Normalise to priceInfo shape so callers get consistent field names
                result = {
                    "priceInfo": {
                        "lastPrice": ltp,
                        "open": entry.get("open"),
                        "high": entry.get("high"),
                        "low": entry.get("low"),
                        "previousClose": entry.get("previousClose"),
                        "pChange": entry.get("percentChange"),
                        "change": entry.get("change"),
                        "totalTradedVolume": entry.get("totalTradedVolume") or 0,
                        "yearHigh": entry.get("yearHigh"),
                        "yearLow": entry.get("yearLow"),
                    },
                    "metadata": {
                        "symbol": symbol_upper,
                        "instrumentType": "INDEX",
                        "indexName": lookup or symbol_upper,
                    },
                    "ltp": ltp,
                    "_sourceType": SourceType.OPEN_SOURCE_NSE_DERIVED.value,
                    "_provider": _PROVIDER_NAME,
                    "_source": "allIndices",
                }
                logger.debug(
                    "nse_index_quote_from_allIndices",
                    component=_PROVIDER_NAME,
                    symbol=symbol,
                    ltp=ltp,
                )
                return result
            # Index not found in allIndices
            raise ProviderMarketClosedError(
                f"Index {symbol!r} not found in NSE allIndices response",
                provider=_PROVIDER_NAME,
            )

        # ── Equity / F&O path (requires Akamai JS cookies) ──────────────
        if exchange_upper == "NFO":
            url = _QUOTE_DERIV_URL
        else:
            url = _QUOTE_URL

        logger.debug(
            "nse_fetch_live_quote",
            component=_PROVIDER_NAME,
            symbol=symbol,
            exchange=exchange_upper,
            note="equity/FNO quotes require Akamai JS cookies (nseappid); may get 403",
        )

        data = await self._get(url, params={"symbol": symbol})

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
            idx_data = await self._get(_ALL_INDICES_URL)
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
