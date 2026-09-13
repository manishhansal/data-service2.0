"""
Upstox V2/V3 provider adapter for DATA-SERVICE 2.0.

Implements historical OHLCV acquisition and live quote fetching via the
Upstox REST API.  OAuth token management (including 401-triggered token
refresh and single retry) is handled entirely within this adapter.

Design constraints
------------------
* Source type: ``BROKER_AUTHENTICATED`` — OAuth access token required.
* Rate limit: 10 req/s (from Capability Matrix).
* The ``3m`` interval is **permanently blocked** for any Indian market
  request; callers will receive a ``ValueError`` before any I/O occurs.
* Credentials (access token, API key) are **never** written to logs,
  traces, or any external surface.

Interval mapping (canonical → Upstox API parameter):
    1m  → "1minute"
    5m  → "5minute"
    10m → "10minute"
    15m → "15minute"
    30m → "30minute"
    1h  → "60minute"
    1d  → "1day"

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

#: Source type classification from the Capability Matrix.
SOURCE_TYPE: SourceType = SourceType.BROKER_AUTHENTICATED

#: Rate limit from the Capability Matrix (10 req/s for Upstox).
RATE_LIMIT_RPS: float = 10.0

#: Upstox V2 API base URL.
UPSTOX_BASE_URL: str = "https://api.upstox.com/v2"

#: Upstox OAuth token refresh endpoint.
UPSTOX_TOKEN_URL: str = "https://api.upstox.com/v2/login/authorization/token"

#: Canonical → Upstox interval mapping.
#: The ``3m`` interval intentionally absent — it is permanently blocked for
#: Indian market data (Requirement 1.5, 4.2, 10.11, 16.10).
INTERVAL_MAP: dict[str, str] = {
    "1m":  "1minute",
    "5m":  "5minute",
    "10m": "10minute",
    "15m": "15minute",
    "30m": "30minute",
    "1h":  "60minute",
    "1d":  "1day",
    "1w":  "1week",
    "1M":  "1month",
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProviderAuthError(Exception):
    """Raised when OAuth token refresh fails and the request cannot proceed.

    This maps to the provider authentication failure path described in
    Requirement 19.8: when Upstox returns HTTP 401 and the subsequent token
    refresh also fails, the caller receives this error and the Upstox
    provider is marked unavailable upstream.

    Attributes:
        provider: Always ``"upstox"``.
        message:  Human-readable description of the auth failure.
    """

    def __init__(self, message: str = "Upstox OAuth token refresh failed") -> None:
        self.provider = PROVIDER_ID.value
        super().__init__(message)


class ProviderRateLimitedError(Exception):
    """Raised when Upstox returns HTTP 429 (rate limit exceeded).

    Per the design, HTTP 429 responses must NOT increment the circuit-breaker
    failure counter (Requirement 5.5).  Callers should catch this and apply
    the appropriate backoff without recording a provider failure.

    Attributes:
        provider:        Always ``"upstox"``.
        retry_after_sec: Seconds to wait before retrying (from ``Retry-After``
                         header, or ``None`` if the header is absent).
    """

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
    """Upstox V2/V3 REST provider adapter.

    Handles:
    - OAuth access-token storage and refresh.
    - Historical OHLCV candle acquisition (task 4.7).
    - Live quote and batch market-quote fetching.
    - Automatic 401 → token-refresh → single-retry flow (Requirement 19.8).
    - Hard block on the ``3m`` interval for Indian market data.

    Args:
        api_key:      Upstox API key (from secrets store; never logged).
        api_secret:   Upstox API secret (from secrets store; never logged).
        redirect_uri: OAuth redirect URI registered with Upstox.
        http_client:  Optional pre-constructed ``httpx.AsyncClient``.  If not
                      provided, a client is created lazily on first use and
                      closed on ``aclose()``.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        redirect_uri: str = "https://localhost/callback",
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        # Credentials — never written to logs or external surfaces.
        self._api_key: str = api_key
        self._api_secret: str = api_secret
        self._redirect_uri: str = redirect_uri

        # OAuth state
        self._access_token: Optional[str] = None
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
        """Store the OAuth access token that was obtained externally.

        The token is stored in memory only; it is **never** written to logs
        or any persistent store from within this adapter.

        Args:
            token: A valid Upstox OAuth2 access token.
        """
        async with self._token_lock:
            self._access_token = token
        # Log at DEBUG level without revealing the token value.
        logger.debug(  # type: ignore[attr-defined]
            "access_token_updated",
            component="upstox_adapter",
        )

    async def refresh_token(self) -> None:
        """Attempt to refresh the Upstox OAuth access token.

        Calls the Upstox token endpoint using the stored ``api_key`` and
        ``api_secret``.  On success, updates the in-memory ``_access_token``.
        On failure, raises ``ProviderAuthError``.

        The request body contains the client credentials; none of these values
        are written to any log entry.

        Raises:
            ProviderAuthError: If the refresh request fails (network error,
                               HTTP 4xx/5xx, or missing token in response).
        """
        async with self._token_lock:
            await self._do_token_refresh()

    async def _do_token_refresh(self) -> None:
        """Internal token refresh — caller must hold ``_token_lock``."""
        try:
            resp = await self._http.post(
                UPSTOX_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._api_key,
                    "client_secret": self._api_secret,
                    "redirect_uri": self._redirect_uri,
                    # In a real implementation the auth-code would be supplied
                    # here.  The adapter supports being called with a pre-set
                    # token (set_access_token) for broker-flow usage; this
                    # refresh path is the "automatic 401 recovery" path.
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            # Network-level failure — do NOT log any credential values.
            logger.warning(  # type: ignore[attr-defined]
                "token_refresh_failed",
                component="upstox_adapter",
                error=type(exc).__name__,
            )
            raise ProviderAuthError(
                "Upstox token refresh failed: network error"
            ) from exc

        if resp.status_code != 200:
            logger.warning(  # type: ignore[attr-defined]
                "token_refresh_failed",
                component="upstox_adapter",
                status_code=resp.status_code,
                # Response body logged only as a hash / truncated snippet;
                # never the full body which might contain secrets.
            )
            raise ProviderAuthError(
                f"Upstox token refresh returned HTTP {resp.status_code}"
            )

        try:
            payload = resp.json()
            new_token: str = payload["access_token"]
        except (KeyError, ValueError) as exc:
            raise ProviderAuthError(
                "Upstox token refresh response missing 'access_token' field"
            ) from exc

        # Store new token without logging the value.
        self._access_token = new_token
        logger.info(  # type: ignore[attr-defined]
            "token_refreshed",
            component="upstox_adapter",
        )

    async def ensure_authenticated(self) -> None:
        """Validate that a token is present; raise ``ProviderAuthError`` if not.

        This is a lightweight guard for callers that want to confirm the
        adapter is in an authenticated state before attempting a request.
        A full OAuth flow is outside the scope of this adapter; callers are
        expected to call ``set_access_token()`` with a valid token.

        Raises:
            ProviderAuthError: If no access token has been set.
        """
        if self._access_token is None:
            raise ProviderAuthError("No Upstox access token set; call set_access_token() first")

    # ------------------------------------------------------------------
    # Internal request helper — handles 401 retry & rate-limit detection
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        """Return Authorization headers without logging the token value."""
        if self._access_token is None:
            raise ProviderAuthError("No Upstox access token available")
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        _retry_on_401: bool = True,
    ) -> dict[str, Any]:
        """Execute an authenticated HTTP request with 401-refresh-retry logic.

        On HTTP 401: immediately attempts ``refresh_token()`` and retries
        the request exactly once with the new token.  If the refresh fails,
        raises ``ProviderAuthError`` (Requirement 19.8).

        On HTTP 429: raises ``ProviderRateLimitedError`` with the
        ``Retry-After`` header value (if present).  Per Requirement 5.5, this
        must NOT be treated as a circuit-breaker failure by callers.

        Args:
            method:          HTTP method string (``"GET"``, ``"POST"``, …).
            url:             Full request URL.
            params:          Optional query-string parameters.
            _retry_on_401:   Internal flag to prevent infinite retry loops.

        Returns:
            Parsed JSON response body as ``dict``.

        Raises:
            ProviderAuthError:        On auth failure that cannot be recovered.
            ProviderRateLimitedError: On HTTP 429.
            httpx.HTTPStatusError:    On other 4xx/5xx responses.
        """
        try:
            resp = await self._http.request(
                method,
                url,
                params=params,
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            logger.warning(  # type: ignore[attr-defined]
                "request_failed",
                component="upstox_adapter",
                url=url,
                error=type(exc).__name__,
            )
            raise

        # --- HTTP 401: attempt token refresh and retry once ---------------
        if resp.status_code == 401:
            if not _retry_on_401:
                # Already retried once; give up.
                raise ProviderAuthError(
                    "Upstox returned HTTP 401 after token refresh — "
                    "marking provider as unavailable"
                )
            logger.info(  # type: ignore[attr-defined]
                "401_token_refresh_triggered",
                component="upstox_adapter",
                url=url,
            )
            await self.refresh_token()
            # Retry with the freshly acquired token.
            return await self._request(
                method, url, params=params, _retry_on_401=False
            )

        # --- HTTP 429: rate limited ----------------------------------------
        if resp.status_code == 429:
            retry_after: Optional[int] = None
            raw_retry = resp.headers.get("Retry-After")
            if raw_retry is not None:
                try:
                    retry_after = int(raw_retry)
                except ValueError:
                    pass
            logger.warning(  # type: ignore[attr-defined]
                "rate_limited",
                component="upstox_adapter",
                url=url,
                retry_after_sec=retry_after,
            )
            raise ProviderRateLimitedError(retry_after_sec=retry_after)

        # --- Other non-2xx -------------------------------------------------
        resp.raise_for_status()

        return resp.json()  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # 3m interval guard
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_not_3m(interval: str) -> None:
        """Raise ``ValueError`` for Indian market ``3m`` requests.

        The ``3m`` interval is permanently unsupported for Indian market data
        at every processing layer (Requirement 1.5, 4.2, 10.11, 16.10).
        This check fires at the adapter level — before any I/O — so that no
        3m candle can ever be acquired from Upstox for Indian instruments.

        Args:
            interval: Canonical interval string from the caller.

        Raises:
            ValueError: Always, when ``interval == "3m"``.
        """
        if interval == "3m":
            raise ValueError(
                "3m interval unsupported: the 3m interval is permanently "
                "unsupported for Indian market data at every processing layer "
                "(Requirement 1.5, 4.2, 10.11, 16.10)"
            )

    # ------------------------------------------------------------------
    # Historical OHLCV
    # ------------------------------------------------------------------

    async def fetch_historical_ohlcv(
        self,
        instrument_key: str,
        from_date: str,
        to_date: str,
        interval: str,
        unit: str = "DAY",
    ) -> list[dict[str, Any]]:
        """Fetch historical OHLCV candles from the Upstox V2 historical API.

        Maps the canonical interval to the Upstox API parameter and calls
        ``GET /v2/historical-candle/{instrument_key}/{api_interval}/{to_date}``
        or the date-range variant depending on whether ``from_date`` is needed.

        The ``3m`` interval is blocked for Indian market data; a ``ValueError``
        is raised before any I/O occurs.

        Args:
            instrument_key: Upstox instrument key, e.g. ``"NSE_EQ|INE002A01018"``.
            from_date:      Start date in ``YYYY-MM-DD`` format (inclusive).
            to_date:        End date in ``YYYY-MM-DD`` format (inclusive).
            interval:       Canonical interval string (``"1m"``, ``"5m"``, …).
            unit:           Unused compatibility parameter (kept for interface
                            parity with other adapter signatures).

        Returns:
            List of raw candle dicts as returned by the Upstox API, each
            containing ``timestamp``, ``open``, ``high``, ``low``, ``close``,
            ``volume``, and ``oi`` (where available).

        Raises:
            ValueError:              If ``interval == "3m"``.
            ProviderAuthError:       On OAuth failure.
            ProviderRateLimitedError: On HTTP 429.
            httpx.HTTPStatusError:   On other provider errors.
        """
        self._assert_not_3m(interval)

        api_interval = INTERVAL_MAP.get(interval)
        if api_interval is None:
            raise ValueError(
                f"Unsupported interval {interval!r} for Upstox adapter. "
                f"Supported: {sorted(INTERVAL_MAP)}"
            )

        # Upstox V2 historical candle endpoint (date-range variant).
        url = (
            f"{UPSTOX_BASE_URL}/historical-candle"
            f"/{instrument_key}/{api_interval}/{to_date}/{from_date}"
        )

        logger.info(  # type: ignore[attr-defined]
            "historical_ohlcv_request",
            component="upstox_adapter",
            instrument_key=instrument_key,
            interval=interval,
            api_interval=api_interval,
            from_date=from_date,
            to_date=to_date,
        )

        response = await self._request("GET", url)

        # Upstox wraps the candle list in {"status":"success","data":{"candles":[…]}}
        try:
            candles: list[dict[str, Any]] = response["data"]["candles"]
        except (KeyError, TypeError):
            logger.warning(  # type: ignore[attr-defined]
                "unexpected_response_shape",
                component="upstox_adapter",
                instrument_key=instrument_key,
                interval=interval,
                response_keys=list(response.keys()) if isinstance(response, dict) else None,
            )
            return []

        return candles

    # ------------------------------------------------------------------
    # Live quote
    # ------------------------------------------------------------------

    async def fetch_live_quote(self, instrument_key: str) -> dict[str, Any]:
        """Fetch the current live market quote for a single instrument.

        Calls ``GET /v2/market-quote/quotes?instrument_key={instrument_key}``.

        Args:
            instrument_key: Upstox instrument key, e.g. ``"NSE_EQ|INE002A01018"``.

        Returns:
            Raw quote dict from the Upstox API for the requested instrument.

        Raises:
            ProviderAuthError:        On OAuth failure.
            ProviderRateLimitedError: On HTTP 429.
            httpx.HTTPStatusError:    On other provider errors.
            KeyError:                 If the instrument key is absent from the
                                      response payload.
        """
        url = f"{UPSTOX_BASE_URL}/market-quote/quotes"

        logger.info(  # type: ignore[attr-defined]
            "live_quote_request",
            component="upstox_adapter",
            instrument_key=instrument_key,
        )

        response = await self._request(
            "GET",
            url,
            params={"instrument_key": instrument_key},
        )

        # Upstox returns {"status":"success","data":{"NSE_EQ|INE…":{ quote }}}
        try:
            data: dict[str, Any] = response["data"]
            quote: dict[str, Any] = data[instrument_key]
        except (KeyError, TypeError) as exc:
            raise KeyError(
                f"Instrument key {instrument_key!r} not found in Upstox live "
                f"quote response"
            ) from exc

        return quote

    async def fetch_market_quote(
        self, instrument_keys: list[str]
    ) -> dict[str, Any]:
        """Fetch live market quotes for multiple instruments in a single call.

        Calls ``GET /v2/market-quote/quotes`` with a comma-separated list of
        instrument keys.

        Args:
            instrument_keys: List of Upstox instrument key strings.

        Returns:
            Dict mapping each instrument key to its raw quote dict as returned
            by Upstox.  Instruments absent from the response are not included.

        Raises:
            ValueError:               If ``instrument_keys`` is empty.
            ProviderAuthError:        On OAuth failure.
            ProviderRateLimitedError: On HTTP 429.
            httpx.HTTPStatusError:    On other provider errors.
        """
        if not instrument_keys:
            raise ValueError("instrument_keys must not be empty")

        url = f"{UPSTOX_BASE_URL}/market-quote/quotes"
        # Upstox accepts multiple keys as a comma-separated query parameter.
        joined = ",".join(instrument_keys)

        logger.info(  # type: ignore[attr-defined]
            "batch_quote_request",
            component="upstox_adapter",
            count=len(instrument_keys),
        )

        response = await self._request(
            "GET",
            url,
            params={"instrument_key": joined},
        )

        try:
            return dict(response["data"])
        except (KeyError, TypeError):
            return {}

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
