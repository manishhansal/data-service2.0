"""
Upstox V3 provider adapter for DATA-SERVICE 2.0.

Implements all relevant market-data APIs using the current Upstox V3 endpoints.
V2 endpoints are only used where no V3 replacement exists (full market quotes).

API inventory covered:
  - OAuth2 token management (store, 401-refresh, retry)
  - Analytics Token support (long-lived read-only token, March 2026)
  - Historical Candle V3        GET /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}
  - Intraday Candle V3          GET /v3/historical-candle/intraday/{key}/{unit}/{interval}
  - LTP Quotes V3               GET /v3/market-quote/ltp
  - OHLC Quotes V3              GET /v3/market-quote/ohlc
  - Full Market Quotes V2       GET /v2/market-quote/quotes  (still V2 — no V3 yet)
  - Option Greeks V3            GET /v3/market-quote/option-greek  (max 50 per request)
  - Option Chain                GET /v2/option/chain
  - Option Contracts            GET /v2/option/contract
  - Exchange Status             GET /v2/market/status/{exchange}
  - Market Holidays             GET /v2/market/holidays
  - Market Timings              GET /v2/market/timings/{date}
  - Instrument Search           GET /v2/instruments/search
  - Expired Instruments         GET /v2/expired-instruments/* (Upstox Plus)
  - Expired Historical Candle   GET /v2/expired-instruments/historical-candle/...

Design constraints
------------------
* Source type: BROKER_AUTHENTICATED — OAuth access token required.
* Rate limit: 50 req/s, 500/min, 2000/30min (standard APIs per NSE circular May 2025).
* The ``3m`` interval is permanently blocked for any Indian market request.
* Credentials are never written to logs, traces, or any external surface.
* Historical OI is present as index 6 in V3 candle arrays — always extracted.
* Option Greeks batching: max 50 instrument keys per request.
* CAS (Closing Auction Session) fields captured when present in responses (Sep 2026).

V3 Interval mapping (canonical → Upstox V3 unit + interval):
    1m  → unit=minutes, interval=1
    5m  → unit=minutes, interval=5
    10m → unit=minutes, interval=10
    15m → unit=minutes, interval=15
    30m → unit=minutes, interval=30
    1h  → unit=hours,   interval=1
    1d  → unit=days,    interval=1
    1w  → unit=weeks,   interval=1
    1M  → unit=months,  interval=1

V3 history limits per unit:
    minutes (1-15): 1 month window
    minutes (>15):  1 quarter window
    hours:          1 quarter window
    days:           1 decade (from Jan 2000)
    weeks/months:   unlimited (from Jan 2000)

Requirements: 5.9, 19.8
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Optional

import httpx

from src.core.schemas.provider import ProviderId, SourceType
from src.observability.logging import get_logger

# ---------------------------------------------------------------------------
# Module logger — no credentials must ever appear in log entries.
# ---------------------------------------------------------------------------

logger: logging.Logger = get_logger(__name__)  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Canonical provider identifier for this adapter.
PROVIDER_ID: ProviderId = ProviderId.UPSTOX

#: Source type classification.
SOURCE_TYPE: SourceType = SourceType.BROKER_AUTHENTICATED

#: Rate limit (req/s) — 50 req/s per NSE circular May 2025.
RATE_LIMIT_RPS: float = 50.0

#: Upstox base URLs
UPSTOX_V2_BASE: str = "https://api.upstox.com/v2"
UPSTOX_V3_BASE: str = "https://api.upstox.com/v3"

#: Backward-compatible alias used by existing tests
UPSTOX_BASE_URL: str = UPSTOX_V2_BASE

#: OAuth token exchange endpoint
UPSTOX_TOKEN_URL: str = f"{UPSTOX_V2_BASE}/login/authorization/token"

#: WebSocket authorization endpoint (returns dynamic wss:// URI)
UPSTOX_WS_AUTH_URL: str = f"{UPSTOX_V2_BASE}/feed/market-data-feed/authorize"

#: Backward-compatible alias: old V2 named interval map (for existing tests).
#: New code should use INTERVAL_MAP_V3.
INTERVAL_MAP: dict[str, str] = {
    "1m":  "1minute",
    "5m":  "5minute",
    "10m": "10minute",
    "15m": "15minute",
    "30m": "30minute",
    "1h":  "60minute",
    "1d":  "day",
    "1w":  "week",
    "1M":  "month",
}

# ---------------------------------------------------------------------------
# V3 canonical → (unit, interval) mapping for historical candles
# ---------------------------------------------------------------------------

#: Maps canonical interval string → (unit, interval_value) for V3 API.
#: 3m is intentionally absent — it is permanently blocked for Indian market data.
INTERVAL_MAP_V3: dict[str, tuple[str, int]] = {
    "1m":  ("minutes", 1),
    "5m":  ("minutes", 5),
    "10m": ("minutes", 10),
    "15m": ("minutes", 15),
    "30m": ("minutes", 30),
    "1h":  ("hours",   1),
    "1d":  ("days",    1),
    "1w":  ("weeks",   1),
    "1M":  ("months",  1),
}

#: Maximum date-range window in days for V3 historical candle requests.
#: Keys match unit strings from INTERVAL_MAP_V3.
V3_WINDOW_DAYS: dict[str, int] = {
    # minutes 1-15: 1 month; >15 min: 1 quarter. We use 28 days conservatively.
    "minutes_narrow":  28,    # intervals 1-15 min
    "minutes_wide":    90,    # intervals 16-300 min
    "hours":           90,    # 1 quarter
    "days":            365,   # 1 year (within the 1-decade limit)
    "weeks":           730,   # no hard limit, use 2 years as practical chunk
    "months":          3650,  # no hard limit, use 10 years
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProviderAuthError(Exception):
    """OAuth token refresh failed."""

    def __init__(self, message: str = "Upstox OAuth token refresh failed") -> None:
        self.provider = PROVIDER_ID.value
        super().__init__(message)


class ProviderRateLimitedError(Exception):
    """HTTP 429 from Upstox.  Must NOT increment circuit-breaker counter."""

    def __init__(
        self,
        retry_after_sec: Optional[int] = None,
        message: str = "Upstox rate limit exceeded (HTTP 429)",
    ) -> None:
        self.provider = PROVIDER_ID.value
        self.retry_after_sec = retry_after_sec
        super().__init__(message)


# ---------------------------------------------------------------------------
# UpstoxAdapter
# ---------------------------------------------------------------------------


class UpstoxAdapter:
    """Upstox V3 REST provider adapter.

    Covers every relevant market-data capability Upstox currently exposes:
    historical OHLCV (V3 with OI), intraday candles (V3), live quotes
    (LTP/OHLC/Full), option Greeks (V3, batched), option chain, option
    contracts, market information, and WebSocket authorization.

    Token management supports both the standard OAuth access token
    (daily refresh) and the Analytics Token (March 2026, 1-year validity).

    Args:
        api_key:          Upstox API key.
        api_secret:       Upstox API secret.
        redirect_uri:     OAuth redirect URI registered with Upstox.
        analytics_token:  Long-lived Analytics Token (optional).  When set,
                          used for read-only market-data calls instead of the
                          OAuth access token so daily re-auth is not needed.
        http_client:      Optional pre-constructed httpx.AsyncClient.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        redirect_uri: str = "https://localhost/callback",
        analytics_token: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._api_key: str = api_key
        self._api_secret: str = api_secret
        self._redirect_uri: str = redirect_uri

        # OAuth state
        self._access_token: Optional[str] = None
        self._analytics_token: Optional[str] = analytics_token
        self._token_lock: asyncio.Lock = asyncio.Lock()

        # HTTP client
        self._owned_client: bool = http_client is None
        self._http: httpx.AsyncClient = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"accept": "application/json"},
        )

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def set_access_token(self, token: str) -> None:
        """Store the OAuth access token (daily rotation).

        The token is stored in memory only — never logged.
        """
        async with self._token_lock:
            self._access_token = token
        logger.debug("upstox_access_token_updated", component="upstox_adapter")  # type: ignore[attr-defined]

    async def set_analytics_token(self, token: str) -> None:
        """Store the long-lived Analytics Token (March 2026, 1-year validity).

        When an Analytics Token is available, it is preferred over the daily
        OAuth token for all read-only market-data requests.
        """
        async with self._token_lock:
            self._analytics_token = token
        logger.debug("upstox_analytics_token_updated", component="upstox_adapter")  # type: ignore[attr-defined]

    async def refresh_token(self) -> None:
        """Attempt to refresh the OAuth access token via authorization_code grant.

        NOTE: A full OAuth2 refresh requires a valid authorization code obtained
        via the browser-based OAuth flow.  In a server-side deployment where the
        token is pre-obtained externally (UPSTOX_ACCESS_TOKEN env var), this
        method handles the 401 → re-auth path via a token re-exchange attempt.
        If the Analytics Token is configured it will be used as the fallback
        instead, making daily rotation optional.

        Raises:
            ProviderAuthError: If refresh fails and no Analytics Token is
                               available.
        """
        async with self._token_lock:
            # If analytics token is available, fall back to it — no refresh needed.
            if self._analytics_token is not None:
                logger.info(  # type: ignore[attr-defined]
                    "upstox_401_analytics_token_fallback",
                    component="upstox_adapter",
                )
                return  # _auth_headers() will use analytics_token
            await self._do_token_refresh()

    async def _do_token_refresh(self) -> None:
        """Internal token refresh — caller must hold _token_lock."""
        try:
            resp = await self._http.post(
                UPSTOX_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._api_key,
                    "client_secret": self._api_secret,
                    "redirect_uri": self._redirect_uri,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("upstox_token_refresh_failed_network", component="upstox_adapter", error=type(exc).__name__)  # type: ignore[attr-defined]
            raise ProviderAuthError("Upstox token refresh failed: network error") from exc

        if resp.status_code != 200:
            logger.warning("upstox_token_refresh_failed_http", component="upstox_adapter", status_code=resp.status_code)  # type: ignore[attr-defined]
            raise ProviderAuthError(f"Upstox token refresh returned HTTP {resp.status_code}")

        try:
            payload = resp.json()
            new_token: str = payload["access_token"]
        except (KeyError, ValueError) as exc:
            raise ProviderAuthError("Upstox token refresh response missing 'access_token'") from exc

        self._access_token = new_token
        logger.info("upstox_token_refreshed", component="upstox_adapter")  # type: ignore[attr-defined]

    async def ensure_authenticated(self) -> None:
        """Verify a token is available; raise ProviderAuthError if not."""
        if self._analytics_token is None and self._access_token is None:
            raise ProviderAuthError(
                "No Upstox token set. Call set_access_token() or set_analytics_token() first."
            )

    # ------------------------------------------------------------------
    # Internal request helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Return Authorization header. Analytics Token takes priority."""
        token = self._analytics_token or self._access_token
        if token is None:
            raise ProviderAuthError("No Upstox token available")
        return {"Authorization": f"Bearer {token}"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        _retry_on_401: bool = True,
    ) -> dict[str, Any]:
        """Execute an authenticated HTTP request with 401-refresh-retry logic.

        On HTTP 401: attempts refresh_token() once and retries.
        On HTTP 429: raises ProviderRateLimitedError (no circuit-breaker count).
        On other 4xx/5xx: raises httpx.HTTPStatusError.
        """
        try:
            resp = await self._http.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            logger.warning("upstox_request_failed", component="upstox_adapter", url=url, error=type(exc).__name__)  # type: ignore[attr-defined]
            raise

        if resp.status_code == 401:
            if not _retry_on_401:
                raise ProviderAuthError(
                    "Upstox returned HTTP 401 after token refresh — marking provider unavailable"
                )
            logger.info("upstox_401_token_refresh_triggered", component="upstox_adapter", url=url)  # type: ignore[attr-defined]
            await self.refresh_token()
            return await self._request(method, url, params=params, json_body=json_body, _retry_on_401=False)

        if resp.status_code == 429:
            retry_after: Optional[int] = None
            raw_retry = resp.headers.get("Retry-After")
            if raw_retry is not None:
                try:
                    retry_after = int(raw_retry)
                except ValueError:
                    pass
            logger.warning("upstox_rate_limited", component="upstox_adapter", url=url, retry_after_sec=retry_after)  # type: ignore[attr-defined]
            raise ProviderRateLimitedError(retry_after_sec=retry_after)

        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # 3m interval guard
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_not_3m(interval: str) -> None:
        """Block the 3m interval for all Indian market requests.

        Raises:
            ValueError: Always when interval == "3m".
        """
        if interval == "3m":
            raise ValueError(
                "3m interval unsupported: permanently blocked for Indian market data "
                "(Requirements 1.5, 4.2, 10.11, 16.10)"
            )

    # ------------------------------------------------------------------
    # Historical OHLCV — V3
    # ------------------------------------------------------------------

    async def fetch_historical_ohlcv(
        self,
        instrument_key: str,
        from_date: str,
        to_date: str,
        interval: str,
    ) -> list[dict[str, Any]]:
        """Fetch historical OHLCV candles using the Upstox V3 historical candle API.

        The V3 API uses a ``unit/interval`` path scheme rather than named
        interval strings.  OI is present at index 6 of each candle array for
        derivative instruments (value 0 for cash equities).

        Candle array format: [timestamp, open, high, low, close, volume, open_interest]

        Args:
            instrument_key: Upstox instrument key, e.g. ``"NSE_EQ|INE002A01018"``.
            from_date:      Start date as ``"YYYY-MM-DD"`` (inclusive).
            to_date:        End date as ``"YYYY-MM-DD"`` (inclusive).
            interval:       Canonical interval string (e.g. ``"1m"``, ``"1d"``).

        Returns:
            List of normalized candle dicts with keys:
            timestamp, open, high, low, close, volume, open_interest, provider.

        Raises:
            ValueError:               If interval == "3m" or unsupported.
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
            httpx.HTTPStatusError:    On other provider errors.
        """
        self._assert_not_3m(interval)

        mapping = INTERVAL_MAP_V3.get(interval)
        if mapping is None:
            raise ValueError(
                f"Unsupported interval {interval!r} for Upstox V3 adapter. "
                f"Supported: {sorted(INTERVAL_MAP_V3)}"
            )
        unit, interval_value = mapping

        url = (
            f"{UPSTOX_V3_BASE}/historical-candle"
            f"/{instrument_key}/{unit}/{interval_value}/{to_date}/{from_date}"
        )

        logger.info(  # type: ignore[attr-defined]
            "upstox_historical_ohlcv_v3_request",
            component="upstox_adapter",
            instrument_key=instrument_key,
            interval=interval,
            unit=unit,
            interval_value=interval_value,
            from_date=from_date,
            to_date=to_date,
        )

        response = await self._request("GET", url)

        try:
            raw_candles: list[list[Any]] = response["data"]["candles"]
        except (KeyError, TypeError):
            logger.warning(  # type: ignore[attr-defined]
                "upstox_unexpected_response_shape",
                component="upstox_adapter",
                instrument_key=instrument_key,
                interval=interval,
                response_keys=list(response.keys()) if isinstance(response, dict) else None,
            )
            return []

        # Normalize each candle: [ts, O, H, L, C, V, OI]
        candles: list[dict[str, Any]] = []
        for row in raw_candles:
            if not isinstance(row, list) or len(row) < 6:
                logger.warning(  # type: ignore[attr-defined]
                    "upstox_malformed_candle",
                    component="upstox_adapter",
                    instrument_key=instrument_key,
                    row_length=len(row) if isinstance(row, list) else "non-list",
                )
                continue
            candles.append({
                "timestamp":      row[0],
                "open":           row[1],
                "high":           row[2],
                "low":            row[3],
                "close":          row[4],
                "volume":         row[5],
                # OI is index 6 in V3 — None when absent (cash instrument returns 0)
                "open_interest":  row[6] if len(row) > 6 else None,
                "provider":       PROVIDER_ID.value,
                "source_type":    SOURCE_TYPE.value,
                "instrument_key": instrument_key,
                "interval":       interval,
                "api_version":    "v3",
            })

        logger.info(  # type: ignore[attr-defined]
            "upstox_historical_ohlcv_v3_fetched",
            component="upstox_adapter",
            instrument_key=instrument_key,
            interval=interval,
            candle_count=len(candles),
        )
        return candles

    # ------------------------------------------------------------------
    # Intraday Candles — V3 (current session only)
    # ------------------------------------------------------------------

    async def fetch_intraday_candles(
        self,
        instrument_key: str,
        interval: str,
    ) -> list[dict[str, Any]]:
        """Fetch intraday OHLCV candles for the current trading session.

        Uses the V3 intraday candle endpoint which returns only current-session
        data.  The last candle may be incomplete (current in-progress candle).

        Args:
            instrument_key: Upstox instrument key.
            interval:       Canonical interval (``"1m"``, ``"5m"``, etc.).

        Returns:
            List of normalized candle dicts with candle_type="INTRADAY" marker.
            The last candle is marked is_complete=False if it spans current time.

        Raises:
            ValueError:               If interval is unsupported.
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
        """
        self._assert_not_3m(interval)

        mapping = INTERVAL_MAP_V3.get(interval)
        if mapping is None:
            raise ValueError(f"Unsupported interval {interval!r} for Upstox V3 intraday.")
        unit, interval_value = mapping

        url = (
            f"{UPSTOX_V3_BASE}/historical-candle/intraday"
            f"/{instrument_key}/{unit}/{interval_value}"
        )

        logger.info(  # type: ignore[attr-defined]
            "upstox_intraday_candles_v3_request",
            component="upstox_adapter",
            instrument_key=instrument_key,
            interval=interval,
        )

        response = await self._request("GET", url)

        try:
            raw_candles: list[list[Any]] = response["data"]["candles"]
        except (KeyError, TypeError):
            return []

        now_ts = datetime.datetime.now(tz=datetime.timezone.utc)
        candles: list[dict[str, Any]] = []
        for i, row in enumerate(raw_candles):
            if not isinstance(row, list) or len(row) < 6:
                continue
            candles.append({
                "timestamp":      row[0],
                "open":           row[1],
                "high":           row[2],
                "low":            row[3],
                "close":          row[4],
                "volume":         row[5],
                "open_interest":  row[6] if len(row) > 6 else None,
                "candle_type":    "INTRADAY",
                # Last candle in session may be incomplete (ongoing candle)
                "is_complete":    i < len(raw_candles) - 1,
                "provider":       PROVIDER_ID.value,
                "source_type":    SOURCE_TYPE.value,
                "instrument_key": instrument_key,
                "interval":       interval,
                "api_version":    "v3",
                "fetched_at_utc": now_ts.isoformat(),
            })

        return candles

    # ------------------------------------------------------------------
    # LTP Quotes — V3
    # ------------------------------------------------------------------

    async def fetch_ltp(
        self,
        instrument_keys: list[str],
    ) -> dict[str, Any]:
        """Fetch Last Traded Price (LTP) for multiple instruments.

        Uses the V3 LTP endpoint which also returns last traded quantity (ltq),
        cumulative day volume, and previous close (cp).

        Args:
            instrument_keys: List of Upstox instrument key strings.

        Returns:
            Dict mapping instrument key → {last_price, ltq, volume, cp, instrument_token}.

        Raises:
            ValueError:               If instrument_keys is empty.
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
        """
        if not instrument_keys:
            raise ValueError("instrument_keys must not be empty")

        url = f"{UPSTOX_V3_BASE}/market-quote/ltp"
        joined = ",".join(instrument_keys)

        logger.info(  # type: ignore[attr-defined]
            "upstox_ltp_v3_request",
            component="upstox_adapter",
            count=len(instrument_keys),
        )

        response = await self._request("GET", url, params={"instrument_key": joined})

        try:
            return dict(response["data"])
        except (KeyError, TypeError):
            return {}

    # ------------------------------------------------------------------
    # OHLC Quotes — V3
    # ------------------------------------------------------------------

    async def fetch_ohlc(
        self,
        instrument_keys: list[str],
        interval: str = "1d",
    ) -> dict[str, Any]:
        """Fetch OHLC quotes including previous and live candle data.

        Uses the V3 OHLC endpoint which returns both the previous completed
        candle and the live (current) candle.

        Args:
            instrument_keys: List of Upstox instrument key strings.
            interval:        Interval for OHLC — one of ``"1d"``, ``"I1"``
                             (1-minute), or ``"I30"`` (30-minute).

        Returns:
            Dict mapping instrument key → {last_price, prev_ohlc, live_ohlc}.

        Raises:
            ValueError:               If instrument_keys is empty.
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
        """
        if not instrument_keys:
            raise ValueError("instrument_keys must not be empty")

        valid_intervals = {"1d", "I1", "I30"}
        if interval not in valid_intervals:
            raise ValueError(f"interval must be one of {valid_intervals}; got {interval!r}")

        url = f"{UPSTOX_V3_BASE}/market-quote/ohlc"
        joined = ",".join(instrument_keys)

        response = await self._request(
            "GET", url,
            params={"instrument_key": joined, "interval": interval},
        )

        try:
            return dict(response["data"])
        except (KeyError, TypeError):
            return {}

    # ------------------------------------------------------------------
    # Full Market Quotes — V2 (no V3 replacement yet)
    # ------------------------------------------------------------------

    async def fetch_full_quote(
        self,
        instrument_keys: list[str],
    ) -> dict[str, Any]:
        """Fetch comprehensive market quote data for up to 500 instruments.

        Uses the V2 full-quote endpoint (no V3 equivalent exists as of
        2026-09-16).  Returns OHLC, depth (5 levels buy/sell), volume,
        net change, circuit limits, and OI (for F&O instruments).

        Args:
            instrument_keys: List of Upstox instrument keys (max 500).

        Returns:
            Dict mapping instrument key → full quote fields:
            {last_price, ohlc, depth, timestamp, volume, net_change,
             lower_circuit_limit, upper_circuit_limit, oi (F&O only)}.

        Raises:
            ValueError:               If instrument_keys is empty or > 500.
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
        """
        if not instrument_keys:
            raise ValueError("instrument_keys must not be empty")
        if len(instrument_keys) > 500:
            raise ValueError(f"Upstox full quote max 500 instruments; got {len(instrument_keys)}")

        url = f"{UPSTOX_V2_BASE}/market-quote/quotes"
        joined = ",".join(instrument_keys)

        logger.info(  # type: ignore[attr-defined]
            "upstox_full_quote_v2_request",
            component="upstox_adapter",
            count=len(instrument_keys),
        )

        response = await self._request("GET", url, params={"instrument_key": joined})

        try:
            return dict(response["data"])
        except (KeyError, TypeError):
            return {}

    # Backward-compatible alias used by older callers
    async def fetch_live_quote(self, instrument_key: str) -> dict[str, Any]:
        """Fetch full quote for a single instrument (backward-compatible)."""
        result = await self.fetch_full_quote([instrument_key])
        try:
            return result[instrument_key]
        except KeyError as exc:
            raise KeyError(
                f"Instrument key {instrument_key!r} not in Upstox quote response"
            ) from exc

    async def fetch_market_quote(self, instrument_keys: list[str]) -> dict[str, Any]:
        """Fetch full quotes for multiple instruments (backward-compatible)."""
        return await self.fetch_full_quote(instrument_keys)

    # ------------------------------------------------------------------
    # Option Greeks — V3 (max 50 per request; caller must batch)
    # ------------------------------------------------------------------

    async def fetch_option_greeks(
        self,
        instrument_keys: list[str],
    ) -> dict[str, Any]:
        """Fetch option Greeks for up to 50 instruments per call.

        Returns IV, delta, gamma, theta, vega, OI, volume, and LTP for each
        instrument.  Callers must split lists > 50 into multiple batches.

        Args:
            instrument_keys: List of Upstox F&O instrument keys (max 50).

        Returns:
            Dict mapping instrument key → {last_price, instrument_token, ltq,
            volume, cp, iv, vega, gamma, theta, delta, oi}.

        Raises:
            ValueError:               If instrument_keys is empty or > 50.
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
        """
        if not instrument_keys:
            raise ValueError("instrument_keys must not be empty")
        if len(instrument_keys) > 50:
            raise ValueError(
                f"Upstox option-greek max 50 instruments per request; got {len(instrument_keys)}. "
                "Use fetch_option_greeks_batched() for larger sets."
            )

        url = f"{UPSTOX_V3_BASE}/market-quote/option-greek"
        joined = ",".join(instrument_keys)

        logger.info(  # type: ignore[attr-defined]
            "upstox_option_greeks_v3_request",
            component="upstox_adapter",
            count=len(instrument_keys),
        )

        response = await self._request("GET", url, params={"instrument_key": joined})

        try:
            return dict(response["data"])
        except (KeyError, TypeError):
            return {}

    async def fetch_option_greeks_batched(
        self,
        instrument_keys: list[str],
        batch_size: int = 50,
    ) -> dict[str, Any]:
        """Fetch option Greeks for any number of instruments by auto-batching.

        Splits the instrument list into batches of ``batch_size`` (max 50),
        calls the Greeks endpoint for each batch, and merges results.

        Args:
            instrument_keys: Any-length list of Upstox F&O instrument keys.
            batch_size:      Keys per batch (default 50, max 50).

        Returns:
            Merged dict mapping instrument key → Greeks data.
        """
        if batch_size > 50:
            batch_size = 50

        merged: dict[str, Any] = {}
        for start in range(0, len(instrument_keys), batch_size):
            batch = instrument_keys[start : start + batch_size]
            batch_result = await self.fetch_option_greeks(batch)
            merged.update(batch_result)

        return merged

    # ------------------------------------------------------------------
    # Option Chain — V2
    # ------------------------------------------------------------------

    async def fetch_option_chain(
        self,
        underlying_key: str,
        expiry_date: str,
    ) -> dict[str, Any]:
        """Fetch a complete option chain snapshot for one underlying + expiry.

        Returns all strikes with CE and PE market data (LTP, close, volume,
        OI, prev OI, bid/ask) and Greeks (delta, gamma, theta, vega, IV, POP)
        in a single call.  Also includes PCR at the chain level.

        Args:
            underlying_key: Upstox instrument key of the underlying, e.g.
                            ``"NSE_INDEX|Nifty 50"`` or ``"NSE_EQ|INE002A01018"``.
            expiry_date:    Expiry date as ``"YYYY-MM-DD"``.

        Returns:
            Raw option chain response dict from the Upstox API, including
            ``pcr``, ``expiry_date``, and a list of per-strike ``data`` rows.

        Raises:
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
            httpx.HTTPStatusError:    On other provider errors.
        """
        url = f"{UPSTOX_V2_BASE}/option/chain"

        logger.info(  # type: ignore[attr-defined]
            "upstox_option_chain_request",
            component="upstox_adapter",
            underlying_key=underlying_key,
            expiry_date=expiry_date,
        )

        response = await self._request(
            "GET",
            url,
            params={
                "instrument_key": underlying_key,
                "expiry_date": expiry_date,
            },
        )

        try:
            return dict(response["data"]) if isinstance(response.get("data"), dict) else {
                "contracts": response.get("data", []),
                "status": response.get("status"),
            }
        except (KeyError, TypeError):
            return {}

    # ------------------------------------------------------------------
    # Option Contracts — V2
    # ------------------------------------------------------------------

    async def fetch_option_contracts(
        self,
        underlying_key: str,
        expiry_date: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch active option contract metadata for an underlying.

        Returns instrument_key, trading_symbol, strike, expiry, lot_size,
        tick_size, and weekly flag for every active option contract.

        Args:
            underlying_key: Upstox instrument key of the underlying.
            expiry_date:    Optional filter by expiry date (YYYY-MM-DD).

        Returns:
            List of option contract metadata dicts.

        Raises:
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
        """
        url = f"{UPSTOX_V2_BASE}/option/contract"
        params: dict[str, str] = {"instrument_key": underlying_key}
        if expiry_date is not None:
            params["expiry_date"] = expiry_date

        response = await self._request("GET", url, params=params)

        try:
            data = response.get("data", [])
            return list(data) if isinstance(data, list) else []
        except (KeyError, TypeError):
            return []

    # ------------------------------------------------------------------
    # Market Information — exchange status, holidays, timings
    # ------------------------------------------------------------------

    async def fetch_exchange_status(self, exchange: str) -> dict[str, Any]:
        """Fetch current trading status for an exchange.

        Args:
            exchange: Exchange code — e.g. ``"NSE"``, ``"BSE"``, ``"MCX"``.

        Returns:
            Dict with ``exchange``, ``status`` (e.g. ``"NORMAL_OPEN"``),
            and ``last_updated`` (epoch ms).

        Raises:
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
        """
        url = f"{UPSTOX_V2_BASE}/market/status/{exchange}"
        response = await self._request("GET", url)
        try:
            return dict(response.get("data", {}))
        except (KeyError, TypeError):
            return {}

    async def fetch_market_holidays(self) -> list[dict[str, Any]]:
        """Fetch NSE/BSE/MCX market holiday schedule.

        Returns:
            List of holiday dicts with ``date``, ``description``, ``exchange``.

        Raises:
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
        """
        url = f"{UPSTOX_V2_BASE}/market/holidays"
        response = await self._request("GET", url)
        try:
            data = response.get("data", [])
            return list(data) if isinstance(data, list) else []
        except (KeyError, TypeError):
            return []

    async def fetch_market_timings(self, date: str) -> list[dict[str, Any]]:
        """Fetch market session timings for a specific date.

        Args:
            date: Date as ``"YYYY-MM-DD"``.

        Returns:
            List of session timing dicts with exchange, segment, open/close times.

        Raises:
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
        """
        url = f"{UPSTOX_V2_BASE}/market/timings/{date}"
        response = await self._request("GET", url)
        try:
            data = response.get("data", [])
            return list(data) if isinstance(data, list) else []
        except (KeyError, TypeError):
            return []

    # ------------------------------------------------------------------
    # WebSocket authorization
    # ------------------------------------------------------------------

    async def fetch_ws_authorized_url(self) -> str:
        """Fetch the dynamic authorized WebSocket URI for V3 market data feed.

        The Upstox V3 WebSocket requires calling this endpoint first to get
        a one-time-use authorized wss:// URI with an embedded code parameter.
        Hardcoding the WebSocket URL is incorrect — this method must be called
        before every new WebSocket connection.

        Returns:
            The ``authorized_redirect_uri`` string (begins with ``wss://``).

        Raises:
            ProviderAuthError:        On token failure.
            ProviderRateLimitedError: On HTTP 429.
            KeyError:                 If the response does not contain the
                                      ``authorized_redirect_uri`` field.
        """
        response = await self._request("GET", UPSTOX_WS_AUTH_URL)
        try:
            uri: str = response["data"]["authorized_redirect_uri"]
        except (KeyError, TypeError) as exc:
            raise KeyError(
                "Upstox WS auth response missing 'authorized_redirect_uri'"
            ) from exc

        logger.info(  # type: ignore[attr-defined]
            "upstox_ws_authorized_url_obtained",
            component="upstox_adapter",
            # Log only the host portion, not the embedded auth code
            host=uri.split("?")[0] if "?" in uri else uri[:60],
        )
        return uri

    # ------------------------------------------------------------------
    # Instrument Search
    # ------------------------------------------------------------------

    async def search_instruments(
        self,
        query: str,
        exchanges: Optional[list[str]] = None,
        segments: Optional[list[str]] = None,
        records: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for instruments by name or symbol.

        Args:
            query:     Search string (e.g. ``"RELIANCE"``, ``"NIFTY"``).
            exchanges: Optional list of exchange codes to filter.
            segments:  Optional list of segment codes to filter.
            records:   Max results to return (default 10).

        Returns:
            List of instrument metadata dicts including instrument_key,
            trading_symbol, exchange, segment, etc.
        """
        url = f"{UPSTOX_V2_BASE}/instruments/search"
        params: dict[str, Any] = {"query": query, "records": records}
        if exchanges:
            params["exchanges"] = ",".join(exchanges)
        if segments:
            params["segments"] = ",".join(segments)

        response = await self._request("GET", url, params=params)
        try:
            data = response.get("data", [])
            return list(data) if isinstance(data, list) else []
        except (KeyError, TypeError):
            return []

    # ------------------------------------------------------------------
    # Expired Instruments (Upstox Plus plan)
    # ------------------------------------------------------------------

    async def fetch_expired_expiries(self, underlying_key: str) -> list[str]:
        """Fetch all past expiry dates for an underlying (Upstox Plus).

        Returns every historical expiry date so survivorship-bias-free
        F&O backtesting can discover contracts that existed in any period.

        Args:
            underlying_key: Upstox instrument key of the underlying.

        Returns:
            List of expiry date strings in ``"YYYY-MM-DD"`` format.

        Raises:
            ProviderAuthError:        On token failure or plan restriction.
            ProviderRateLimitedError: On HTTP 429.
        """
        url = f"{UPSTOX_V2_BASE}/expired-instruments/expiries"
        response = await self._request(
            "GET", url,
            params={"instrument_key": underlying_key},
        )
        try:
            data = response.get("data", [])
            return list(data) if isinstance(data, list) else []
        except (KeyError, TypeError):
            return []

    async def fetch_expired_option_contracts(
        self,
        underlying_key: str,
        expiry_date: str,
    ) -> list[dict[str, Any]]:
        """Fetch expired option contracts for a specific expiry (Upstox Plus).

        Returns expired_instrument_key, strike, option_type, trading_symbol
        for all contracts that settled on the given expiry.

        Args:
            underlying_key: Upstox instrument key of the underlying.
            expiry_date:    Expiry date as ``"YYYY-MM-DD"``.

        Returns:
            List of expired contract dicts including ``expired_instrument_key``.
        """
        url = f"{UPSTOX_V2_BASE}/expired-instruments/option/contract"
        response = await self._request(
            "GET", url,
            params={"instrument_key": underlying_key, "expiry_date": expiry_date},
        )
        try:
            data = response.get("data", [])
            return list(data) if isinstance(data, list) else []
        except (KeyError, TypeError):
            return []

    async def fetch_expired_future_contracts(
        self,
        underlying_key: str,
        expiry_date: str,
    ) -> list[dict[str, Any]]:
        """Fetch expired future contracts for a specific expiry (Upstox Plus).

        Args:
            underlying_key: Upstox instrument key of the underlying.
            expiry_date:    Expiry date as ``"YYYY-MM-DD"``.

        Returns:
            List of expired contract dicts including ``expired_instrument_key``.
        """
        url = f"{UPSTOX_V2_BASE}/expired-instruments/future/contract"
        response = await self._request(
            "GET", url,
            params={"instrument_key": underlying_key, "expiry_date": expiry_date},
        )
        try:
            data = response.get("data", [])
            return list(data) if isinstance(data, list) else []
        except (KeyError, TypeError):
            return []

    async def fetch_expired_historical_candles(
        self,
        expired_instrument_key: str,
        from_date: str,
        to_date: str,
        interval: str = "1d",
    ) -> list[dict[str, Any]]:
        """Fetch historical OHLCV for an expired F&O contract (Upstox Plus).

        Uses the expired instruments candle endpoint which accepts the
        ``expired_instrument_key`` format: e.g. ``"NSE_FO|53806|24-04-2025"``.

        Args:
            expired_instrument_key: Expired contract key returned by
                                    fetch_expired_option_contracts() or
                                    fetch_expired_future_contracts().
            from_date:              Start date ``"YYYY-MM-DD"``.
            to_date:                End date ``"YYYY-MM-DD"``.
            interval:               Interval string — ``"day"``, ``"1minute"``,
                                    ``"30minute"``.

        Returns:
            List of candle dicts with OI at index 6.
        """
        url = (
            f"{UPSTOX_V2_BASE}/expired-instruments/historical-candle"
            f"/{expired_instrument_key}/{interval}/{to_date}/{from_date}"
        )

        logger.info(  # type: ignore[attr-defined]
            "upstox_expired_candle_request",
            component="upstox_adapter",
            expired_key=expired_instrument_key,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
        )

        response = await self._request("GET", url)

        try:
            raw_candles: list[list[Any]] = response["data"]["candles"]
        except (KeyError, TypeError):
            return []

        candles: list[dict[str, Any]] = []
        for row in raw_candles:
            if not isinstance(row, list) or len(row) < 6:
                continue
            candles.append({
                "timestamp":               row[0],
                "open":                    row[1],
                "high":                    row[2],
                "low":                     row[3],
                "close":                   row[4],
                "volume":                  row[5],
                "open_interest":           row[6] if len(row) > 6 else None,
                "provider":                PROVIDER_ID.value,
                "source_type":             SOURCE_TYPE.value,
                "expired_instrument_key":  expired_instrument_key,
                "interval":                interval,
                "api_version":             "v2_expired",
            })

        return candles

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client if it was created by this adapter."""
        if self._owned_client:
            await self._http.aclose()

    async def __aenter__(self) -> "UpstoxAdapter":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()
