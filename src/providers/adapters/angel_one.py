"""
Angel One SmartAPI provider adapter for DATA-SERVICE 2.0.

This adapter is the **only** component that may call Angel One SmartAPI
endpoints directly.  It is authenticated via TOTP + JWT and all responses
are classified with ``SourceType.BROKER_AUTHENTICATED``.

Responsibilities
----------------
- TOTP + JWT authentication (login, token refresh, scheduled rotation)
- Historical OHLCV candle fetch (primary for multi-day equity/F&O intraday)
- Live quote fetch (LTP + market data)
- Broker analytics: PCR, OI buildup, gainers/losers (from SmartAPI)

Multi-worker authentication
-----------------------------
Angel One TOTP codes are single-use within a 30-second window.  When
uvicorn runs with --workers N, all N worker processes call authenticate()
at startup, but only the first one succeeds — the others get HTTP 403
because the TOTP has already been consumed.

Fix: Redis-backed JWT sharing.
  Key: ``mds:angel_one:jwt:<client_id>``
  TTL: 6 hours (Angel One JWTs are valid for approximately 1 day, but
       we refresh conservatively at 6h to stay well within the window).

  Startup flow per worker:
    1. Check Redis for an existing JWT.
    2. If found, load it — skip TOTP login entirely.
    3. If not found, acquire a Redis distributed lock, re-check, then
       perform TOTP login and store the JWT in Redis.
  This ensures exactly one TOTP login per 6-hour window regardless of
  the number of workers.

Rate limits
-----------
Angel One enforces a 3 req/s ceiling at the NSE proxy level.  The adapter
implements a simple in-process token bucket (asyncio.Semaphore + sleep) for
this cap.  For cross-replica enforcement, the ``TokenBucketRateLimiter`` in
the Provider Gateway adds an additional Redis-backed layer.

Interval mapping
----------------
The SmartAPI ``getCandleData`` endpoint uses named intervals:

| Canonical | SmartAPI name   |
|-----------|-----------------|
| ``1m``    | ONE_MINUTE      |
| ``5m``    | FIVE_MINUTE     |
| ``10m``   | TEN_MINUTE      |
| ``15m``   | FIFTEEN_MINUTE  |
| ``30m``   | THIRTY_MINUTE   |
| ``1h``    | ONE_HOUR        |
| ``1d``    | ONE_DAY         |
| ``1w``    | ONE_WEEK        |

The ``3m`` interval is permanently unsupported for Indian market data and is
blocked here with a ``ProviderUnsupportedError``.

Error mapping
-------------
| HTTP / API status | Exception raised            | Notes                       |
|-------------------|-----------------------------|-----------------------------|
| 401 (auth)        | ProviderAuthError           | Token expired; re-auth once |
| 429               | ProviderRateLimitedError    | Circuit breaker NOT counted |
| 5xx / timeout     | ProviderUnavailableError    | Circuit breaker counted     |
| bad JSON / schema | ProviderDataError           | Circuit breaker counted     |
| market closed     | ProviderMarketClosedError   | Circuit breaker NOT counted |
| unsupported cap.  | ProviderUnsupportedError    | Circuit breaker NOT counted |

Security
--------
- API key, client ID, TOTP secret, and JWT access token are **NEVER** written
  to logs, traces, or error messages (Requirements 19.1, 19.4).
- All credential references in log entries use opaque placeholders
  (e.g. ``"<redacted>"``).

Requirements: 5.9, 19.7
"""

from __future__ import annotations

import asyncio
import json as _json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import pyotp

from src.core.schemas.provider import SourceType
from src.observability.logging import get_logger
from src.providers.adapters.base import (
    ProviderAuthError,
    ProviderDataError,
    ProviderMarketClosedError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
    ProviderUnsupportedError,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDER_NAME = "angel_one"
_SOURCE_TYPE = SourceType.BROKER_AUTHENTICATED

# Angel One SmartAPI base URL
_BASE_URL = "https://apiconnect.angelbroking.com"

# REST endpoints
_LOGIN_URL = f"{_BASE_URL}/rest/auth/angelbroking/user/v1/loginByPassword"
_CANDLE_URL = f"{_BASE_URL}/rest/secure/angelbroking/historical/v1/getCandleData"
_OI_URL = f"{_BASE_URL}/rest/secure/angelbroking/historical/v1/getOIData"
_QUOTE_URL = f"{_BASE_URL}/rest/secure/angelbroking/market/v1/quote/"
_LTP_URL = f"{_BASE_URL}/rest/secure/angelbroking/order/v1/getLtpData"
_PCR_URL = f"{_BASE_URL}/rest/secure/angelbroking/marketData/v1/putCallRatio"
_OI_BUILDUP_URL = f"{_BASE_URL}/rest/secure/angelbroking/marketData/v1/OIBuildup"
_GAINERS_LOSERS_URL = f"{_BASE_URL}/rest/secure/angelbroking/marketData/v1/gainersAndLosers"
_OPTION_GREEK_URL = f"{_BASE_URL}/rest/secure/angelbroking/marketData/v1/optionGreek"
_NSE_INTRADAY_URL = f"{_BASE_URL}/rest/secure/angelbroking/marketData/v1/nseIntraday"

# Rate limit: 3 req/s (Capability Matrix)
_REQUESTS_PER_SECOND = 3.0
_MIN_INTERVAL_S = 1.0 / _REQUESTS_PER_SECOND  # ~0.333s

# Interval mapping — canonical → SmartAPI named interval
# Note: Angel One SmartAPI does NOT support 1M (monthly) candles.
# Monthly bars must be sourced from Upstox or Yahoo Finance.
# Requesting 1M raises ProviderUnsupportedError — callers must fall back.
_INTERVAL_MAP: dict[str, str] = {
    "1m": "ONE_MINUTE",
    "5m": "FIVE_MINUTE",
    "10m": "TEN_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "30m": "THIRTY_MINUTE",
    "1h": "ONE_HOUR",
    "1d": "ONE_DAY",
    "1w": "ONE_WEEK",
    # "1M": NOT SUPPORTED — Angel One SmartAPI has no monthly interval.
    # Use Upstox (1month) or Yahoo Finance (1mo) for monthly candles.
}

# Intervals permanently banned for Indian market data
_BANNED_INTERVALS = frozenset({"3m"})

# ---------------------------------------------------------------------------
# Redis JWT sharing — prevents TOTP conflicts in multi-worker deployments
# ---------------------------------------------------------------------------

#: Redis key for the shared JWT token (keyed by client_id at runtime).
#: Format: ``mds:angel_one:jwt:{client_id}``
_REDIS_JWT_KEY_TEMPLATE = "mds:angel_one:jwt:{client_id}"

#: Redis key for the distributed auth lock (prevents simultaneous logins).
_REDIS_AUTH_LOCK_TEMPLATE = "mds:angel_one:auth_lock:{client_id}"

#: How long to store the JWT in Redis (seconds). Angel One JWTs are valid
#: for ~24 hours; we refresh after 6 hours to stay safely within window.
_JWT_REDIS_TTL_SEC = 6 * 3600  # 6 hours

#: How long to hold the distributed auth lock while performing login (seconds).
_AUTH_LOCK_TTL_SEC = 15

#: How long to wait to acquire the auth lock before giving up (seconds).
_AUTH_LOCK_WAIT_SEC = 20


# ---------------------------------------------------------------------------
# AngelOneAdapter
# ---------------------------------------------------------------------------


class AngelOneAdapter:
    """Provider adapter for Angel One SmartAPI.

    This is an **authenticated** adapter.  It requires three credentials
    supplied at construction time:

    - ``api_key``     — SmartAPI REST API key
    - ``client_id``   — Broker client ID (registered login)
    - ``totp_secret`` — Base32 TOTP seed (RFC 6238)

    The adapter is **not** thread-safe.  It is designed for single-threaded
    asyncio use.  If multiple concurrent requests are made, they are
    serialised through a per-instance asyncio lock.

    Args:
        api_key:      Angel One SmartAPI key.  Never logged.
        client_id:    Angel One client ID.  Never logged.
        totp_secret:  Base32 TOTP seed.  Never logged.
        http_client:  Optional pre-constructed ``httpx.AsyncClient``.  When
                      ``None`` (default), a new client is created on first use.
                      Inject a mock client in tests.
    """

    def __init__(
        self,
        api_key: str,
        client_id: str,
        totp_secret: str,
        *,
        mpin: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        redis_client: Optional[Any] = None,
    ) -> None:
        # Credentials are stored in private attributes and never echoed
        self._api_key = api_key
        self._client_id = client_id
        self._totp_secret = totp_secret
        # Angel One SmartAPI login requires:
        #   password = 4-digit MPIN (broker login PIN)
        #   totp     = 6-digit TOTP code (from TOTP_SECRET)
        # If mpin is not provided, fall back to using TOTP as password for
        # backward compat (some test environments do not need MPIN).
        self._mpin: Optional[str] = mpin

        self._http_client = http_client
        self._owns_client = http_client is None  # True → we created it, we close it

        # Optional Redis client for cross-worker JWT sharing.
        # When set, authenticate() reads/writes the JWT via Redis so that
        # only one worker per 6-hour window performs the TOTP login.
        self._redis: Optional[Any] = redis_client

        # JWT token state
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._token_acquired_at: Optional[float] = None  # monotonic clock seconds

        # Serialise auth operations to prevent concurrent re-auth races
        self._auth_lock: asyncio.Lock = asyncio.Lock()

        # In-process rate-limiting (last request timestamp for min-interval enforcement)
        self._last_request_at: float = 0.0
        self._rate_lock: asyncio.Lock = asyncio.Lock()

        logger.debug(
            "angel_one_adapter_created",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            source_type=_SOURCE_TYPE.value,
        )

    # ---------------------------------------------------------------------- #
    # Lifecycle                                                                #
    # ---------------------------------------------------------------------- #

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared async HTTP client, creating it on first call."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )
        return self._http_client

    async def close(self) -> None:
        """Release resources (close HTTP client if we created it)."""
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
            logger.debug(
                "angel_one_adapter_closed",
                component="angel_one_adapter",
                provider=_PROVIDER_NAME,
            )

    # ---------------------------------------------------------------------- #
    # Authentication                                                           #
    # ---------------------------------------------------------------------- #

    async def _load_jwt_from_redis(self) -> bool:
        """Try to load a valid JWT from Redis.

        Returns True if a token was found and loaded, False otherwise.
        """
        if self._redis is None:
            return False
        try:
            redis_key = _REDIS_JWT_KEY_TEMPLATE.format(client_id=self._client_id)
            raw = await self._redis.get(redis_key)
            if raw:
                data = _json.loads(raw)
                token = data.get("access_token")
                if token:
                    self._access_token = token
                    self._refresh_token = data.get("refresh_token")
                    self._token_acquired_at = time.monotonic()
                    logger.info(
                        "angel_one_jwt_loaded_from_redis",
                        component="angel_one_adapter",
                        provider=_PROVIDER_NAME,
                    )
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "angel_one_redis_jwt_read_failed",
                component="angel_one_adapter",
                error=str(exc),
            )
        return False

    async def _store_jwt_in_redis(self) -> None:
        """Persist the current JWT to Redis for cross-worker sharing."""
        if self._redis is None or not self._access_token:
            return
        try:
            redis_key = _REDIS_JWT_KEY_TEMPLATE.format(client_id=self._client_id)
            payload = _json.dumps({
                "access_token": self._access_token,
                "refresh_token": self._refresh_token or "",
                "stored_at": _utc_iso_now(),
            })
            await self._redis.set(redis_key, payload, ex=_JWT_REDIS_TTL_SEC)
            logger.info(
                "angel_one_jwt_stored_in_redis",
                component="angel_one_adapter",
                provider=_PROVIDER_NAME,
                ttl_sec=_JWT_REDIS_TTL_SEC,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "angel_one_redis_jwt_write_failed",
                component="angel_one_adapter",
                error=str(exc),
            )

    async def authenticate(self) -> None:
        """Authenticate with Angel One SmartAPI using TOTP + password flow.

        Multi-worker safe: uses a Redis distributed lock so that only one
        worker performs the TOTP login at a time.  Other workers wait for the
        lock to release, then read the JWT from Redis (stored by the winner).

        Generates a fresh TOTP code from the seed, calls the SmartAPI login
        endpoint, and stores the returned JWT access token + refresh token
        both in-process and in Redis.

        This method should not be called concurrently; callers should use
        ``ensure_authenticated()`` which acquires the auth lock.

        Raises:
            ProviderAuthError:        Login API returned a non-success status
                                      or an error code in the response body.
            ProviderUnavailableError: Network or HTTP 5xx failure.
        """
        # ── Distributed lock (Redis) — prevents simultaneous TOTP logins ──
        lock_key = _REDIS_AUTH_LOCK_TEMPLATE.format(client_id=self._client_id)
        acquired_lock = False

        if self._redis is not None:
            try:
                # SET NX EX — atomic acquire; returns True if we got the lock
                acquired_lock = await self._redis.set(
                    lock_key, "1", nx=True, ex=_AUTH_LOCK_TTL_SEC
                )
                if not acquired_lock:
                    # Another worker is authenticating; wait then read their token
                    waited = 0.0
                    while waited < _AUTH_LOCK_WAIT_SEC:
                        await asyncio.sleep(0.5)
                        waited += 0.5
                        if await self._load_jwt_from_redis():
                            logger.info(
                                "angel_one_jwt_acquired_after_lock_wait",
                                component="angel_one_adapter",
                                provider=_PROVIDER_NAME,
                            )
                            return
                    # Lock holder may have crashed — proceed with our own login
                    logger.warning(
                        "angel_one_auth_lock_wait_timeout",
                        component="angel_one_adapter",
                        provider=_PROVIDER_NAME,
                        waited_sec=waited,
                        note="Proceeding with TOTP login anyway",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "angel_one_redis_lock_failed",
                    component="angel_one_adapter",
                    error=str(exc),
                    note="Falling through to direct TOTP login",
                )

        # ── TOTP login ────────────────────────────────────────────────────
        totp_code = pyotp.TOTP(self._totp_secret).now()

        payload = {
            "clientcode": self._client_id,
            # SmartAPI: password = 4-digit MPIN, totp = 6-digit TOTP code.
            # Fall back to TOTP as password when MPIN not provided (legacy/test).
            "password": self._mpin if self._mpin else totp_code,
            "totp": totp_code,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self._api_key,  # API key in header, never in body or logs
        }

        client = await self._get_client()
        try:
            resp = await client.post(_LOGIN_URL, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                "Angel One login timed out",
                provider=_PROVIDER_NAME,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(
                f"Angel One login request failed: {type(exc).__name__}",
                provider=_PROVIDER_NAME,
            ) from exc

        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp)
            raise ProviderRateLimitedError(
                "Angel One rate limit during login",
                provider=_PROVIDER_NAME,
                status_code=429,
                retry_after_s=retry_after,
            )

        if resp.status_code >= 500:
            raise ProviderUnavailableError(
                f"Angel One login server error: HTTP {resp.status_code}",
                provider=_PROVIDER_NAME,
                status_code=resp.status_code,
            )

        if resp.status_code != 200:
            # 401 or other auth rejection — do not log credentials
            raise ProviderAuthError(
                f"Angel One login failed: HTTP {resp.status_code}",
                provider=_PROVIDER_NAME,
                status_code=resp.status_code,
            )

        body = _parse_json(resp, provider=_PROVIDER_NAME)

        # SmartAPI success indicator: body["status"] == True
        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            raise ProviderAuthError(
                f"Angel One login rejected: {error_msg}",
                provider=_PROVIDER_NAME,
                status_code=resp.status_code,
            )

        data = body.get("data") or {}
        self._access_token = data.get("jwtToken")
        self._refresh_token = data.get("refreshToken")
        self._token_acquired_at = time.monotonic()

        if not self._access_token:
            raise ProviderAuthError(
                "Angel One login succeeded but jwtToken missing in response",
                provider=_PROVIDER_NAME,
            )

        logger.info(
            "angel_one_authenticated",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            # NEVER log the token value
        )

        # ── Store JWT in Redis so other workers can reuse it ──────────────
        await self._store_jwt_in_redis()

        # ── Release the distributed auth lock ────────────────────────────
        if acquired_lock and self._redis is not None:
            try:
                await self._redis.delete(lock_key)
            except Exception:  # noqa: BLE001
                pass  # Lock will expire on its own via TTL

    async def ensure_authenticated(self) -> None:
        """Ensure the adapter holds a valid JWT token.

        Priority order (multi-worker safe):
          1. In-process token already present → use it.
          2. Redis has a cached token from another worker → load it.
          3. Neither found → acquire Redis distributed lock → TOTP login.

        This method is safe to call from multiple concurrent tasks — it
        acquires an in-process lock so only one authentication attempt runs
        at a time within this process.

        Raises:
            ProviderAuthError:        Authentication failed.
            ProviderUnavailableError: Network failure during auth.
        """
        async with self._auth_lock:
            # Fast path: already authenticated in this process
            if self._access_token is not None:
                return
            # Try Redis first — avoids consuming a new TOTP code
            if await self._load_jwt_from_redis():
                return
            # Full TOTP login (handles Redis lock internally)
            await self.authenticate()

    async def rotate_token(self) -> None:
        """Force a full token rotation (used by the 23:55 IST scheduler).

        Discards the current token, clears the Redis cache, and performs a
        fresh TOTP login.

        Raises:
            ProviderAuthError:        Re-authentication failed.
            ProviderUnavailableError: Network failure during re-auth.
        """
        async with self._auth_lock:
            self._access_token = None
            self._refresh_token = None
            self._token_acquired_at = None
            # Clear Redis so other workers pick up the new token
            if self._redis is not None:
                try:
                    redis_key = _REDIS_JWT_KEY_TEMPLATE.format(
                        client_id=self._client_id
                    )
                    await self._redis.delete(redis_key)
                except Exception:  # noqa: BLE001
                    pass
            await self.authenticate()
            logger.info(
                "angel_one_token_rotated",
                component="angel_one_adapter",
                provider=_PROVIDER_NAME,
            )

    # ---------------------------------------------------------------------- #
    # Internal request helper                                                  #
    # ---------------------------------------------------------------------- #

    def _auth_headers(self) -> dict[str, str]:
        """Return the standard authenticated request headers.

        The JWT access token is placed in the Authorization header.
        It is never written to the log.
        """
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self._api_key,
        }

    async def _throttle(self) -> None:
        """Enforce the 3 req/s rate limit using a simple min-interval guard.

        Sleeps until the minimum inter-request interval has elapsed since the
        last outbound call.  Uses an asyncio lock to serialise the check.
        """
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_at
            if elapsed < _MIN_INTERVAL_S:
                await asyncio.sleep(_MIN_INTERVAL_S - elapsed)
            self._last_request_at = time.monotonic()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
        _retry_on_401: bool = True,
    ) -> Any:
        """Execute an authenticated HTTP request with 401 retry logic.

        On HTTP 401, re-authenticates once and retries the original request.
        Never retries a 429 — raises ``ProviderRateLimitedError`` immediately.

        Args:
            method:         HTTP method (GET, POST, etc.).
            url:            Full URL to request.
            json_body:      JSON body for POST requests.
            params:         Query parameters.
            _retry_on_401:  Internal flag — set False on the retry to prevent
                            infinite recursion.

        Returns:
            Parsed JSON body as a Python dict or list.

        Raises:
            ProviderAuthError:        Authentication failure that cannot be recovered.
            ProviderRateLimitedError: HTTP 429 from upstream.
            ProviderUnavailableError: HTTP 5xx or network error.
            ProviderDataError:        Malformed response body.
        """
        await self.ensure_authenticated()
        await self._throttle()

        client = await self._get_client()
        headers = self._auth_headers()

        try:
            resp = await client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                f"Angel One request timed out: {url}",
                provider=_PROVIDER_NAME,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(
                f"Angel One request error: {type(exc).__name__}",
                provider=_PROVIDER_NAME,
            ) from exc

        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp)
            logger.warning(
                "angel_one_rate_limited",
                component="angel_one_adapter",
                provider=_PROVIDER_NAME,
                retry_after_s=retry_after,
                # Do NOT log URL if it could contain credentials
            )
            raise ProviderRateLimitedError(
                "Angel One rate limit exceeded (HTTP 429)",
                provider=_PROVIDER_NAME,
                status_code=429,
                retry_after_s=retry_after,
            )

        if resp.status_code == 401:
            if _retry_on_401:
                # Token expired — re-authenticate once and retry
                logger.info(
                    "angel_one_token_expired_reauth",
                    component="angel_one_adapter",
                    provider=_PROVIDER_NAME,
                )
                async with self._auth_lock:
                    self._access_token = None
                    await self.authenticate()
                return await self._request(
                    method,
                    url,
                    json_body=json_body,
                    params=params,
                    _retry_on_401=False,  # no infinite loop
                )
            raise ProviderAuthError(
                "Angel One authentication failed after token rotation",
                provider=_PROVIDER_NAME,
                status_code=401,
            )

        if resp.status_code >= 500:
            raise ProviderUnavailableError(
                f"Angel One server error: HTTP {resp.status_code}",
                provider=_PROVIDER_NAME,
                status_code=resp.status_code,
            )

        if resp.status_code not in (200, 201):
            raise ProviderDataError(
                f"Angel One unexpected status: HTTP {resp.status_code}",
                provider=_PROVIDER_NAME,
                status_code=resp.status_code,
            )

        return _parse_json(resp, provider=_PROVIDER_NAME)

    # ---------------------------------------------------------------------- #
    # Historical OHLCV                                                         #
    # ---------------------------------------------------------------------- #

    async def fetch_historical_ohlcv(
        self,
        symbol: str,
        token: str,
        from_date: str,
        to_date: str,
        interval: str,
        exchange: str = "NSE",
    ) -> list[dict[str, Any]]:
        """Fetch historical OHLCV candle data from Angel One SmartAPI.

        Primary source for multi-day intraday equity/F&O history at
        ``1m``–``1h`` intervals.

        Args:
            symbol:    Trading symbol (e.g. ``"RELIANCE"``).
            token:     Angel One instrument token (numeric string).
            from_date: Start datetime as ``"YYYY-MM-DD HH:MM"`` (IST).
            to_date:   End datetime as ``"YYYY-MM-DD HH:MM"`` (IST).
            interval:  Canonical interval string (e.g. ``"1m"``, ``"5m"``).
                       ``"3m"`` raises ``ProviderUnsupportedError``.
            exchange:  Exchange code (default ``"NSE"``).

        Returns:
            List of raw OHLCV dicts with keys ``time``, ``open``, ``high``,
            ``low``, ``close``, ``volume`` as returned by SmartAPI.

        Raises:
            ProviderUnsupportedError: Interval is ``"3m"`` or unmapped.
            ProviderAuthError:        Authentication failed.
            ProviderRateLimitedError: HTTP 429 from upstream.
            ProviderUnavailableError: HTTP 5xx or network error.
            ProviderDataError:        Malformed response.
        """
        if interval in _BANNED_INTERVALS:
            raise ProviderUnsupportedError(
                "interval 3m is permanently unsupported for Indian market data",
                provider=_PROVIDER_NAME,
            )

        smartapi_interval = _INTERVAL_MAP.get(interval)
        if smartapi_interval is None:
            raise ProviderUnsupportedError(
                f"Interval {interval!r} is not supported by Angel One SmartAPI",
                provider=_PROVIDER_NAME,
            )

        payload: dict[str, Any] = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": smartapi_interval,
            "fromdate": from_date,
            "todate": to_date,
        }

        logger.debug(
            "angel_one_fetch_historical_ohlcv",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
            # token is a non-secret numeric identifier — safe to log
            token=token,
        )

        body = await self._request("POST", _CANDLE_URL, json_body=payload)

        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            # Angel One returns status=True with empty data when market is closed
            if "no data" in error_msg.lower() or "no record" in error_msg.lower():
                raise ProviderMarketClosedError(
                    f"Angel One returned no data (market closed): {error_msg}",
                    provider=_PROVIDER_NAME,
                )
            raise ProviderDataError(
                f"Angel One historical fetch failed: {error_msg}",
                provider=_PROVIDER_NAME,
            )

        raw_candles = body.get("data") or []
        if not isinstance(raw_candles, list):
            raise ProviderDataError(
                "Angel One historical response 'data' field is not a list",
                provider=_PROVIDER_NAME,
            )

        # SmartAPI returns candles as [timestamp, open, high, low, close, volume]
        candles: list[dict[str, Any]] = []
        for row in raw_candles:
            if not isinstance(row, list) or len(row) < 6:
                logger.warning(
                    "angel_one_malformed_candle_row",
                    component="angel_one_adapter",
                    provider=_PROVIDER_NAME,
                    symbol=symbol,
                    row_length=len(row) if isinstance(row, list) else "non-list",
                )
                continue
            candles.append(
                {
                    "time": row[0],    # ISO-8601 timestamp string from API
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "volume": row[5],
                    # Provenance metadata
                    "provider": _PROVIDER_NAME,
                    "sourceType": _SOURCE_TYPE.value,
                    "symbol": symbol,
                    "exchange": exchange,
                    "interval": interval,
                }
            )

        logger.debug(
            "angel_one_historical_ohlcv_fetched",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            symbol=symbol,
            interval=interval,
            candle_count=len(candles),
        )
        return candles

    # ---------------------------------------------------------------------- #
    # Live quote                                                               #
    # ---------------------------------------------------------------------- #

    async def fetch_live_quote(
        self,
        token: str,
        exchange: str = "NSE",
    ) -> dict[str, Any]:
        """Fetch a live LTP and market snapshot for a single instrument.

        Args:
            token:    Angel One instrument token (numeric string).
            exchange: Exchange code (default ``"NSE"``).

        Returns:
            Raw quote dict containing fields such as ``ltp``, ``open``,
            ``high``, ``low``, ``close``, ``volume``, ``tradeTime``,
            ``upperCircuit``, ``lowerCircuit``, ``52WeekHigh``,
            ``52WeekLow`` as returned by SmartAPI.

        Raises:
            ProviderMarketClosedError: Market is closed (empty data).
            ProviderAuthError:         Authentication failed.
            ProviderRateLimitedError:  HTTP 429 from upstream.
            ProviderUnavailableError:  HTTP 5xx or network error.
            ProviderDataError:         Malformed response.
        """
        payload: dict[str, Any] = {
            "mode": "FULL",
            "exchangeTokens": {
                exchange: [token],
            },
        }

        logger.debug(
            "angel_one_fetch_live_quote",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            exchange=exchange,
            # token is a non-secret numeric instrument identifier
            token=token,
        )

        body = await self._request("POST", _QUOTE_URL, json_body=payload)

        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            raise ProviderDataError(
                f"Angel One live quote failed: {error_msg}",
                provider=_PROVIDER_NAME,
            )

        data = body.get("data") or {}
        fetched_data = data.get("fetched") or []

        if not fetched_data:
            raise ProviderMarketClosedError(
                "Angel One live quote returned empty fetched data (market closed)",
                provider=_PROVIDER_NAME,
            )

        quote_raw = fetched_data[0] if isinstance(fetched_data, list) else fetched_data

        # Attach provenance metadata
        quote_raw["provider"] = _PROVIDER_NAME
        quote_raw["sourceType"] = _SOURCE_TYPE.value
        quote_raw["exchange"] = exchange
        quote_raw["token"] = token

        logger.debug(
            "angel_one_live_quote_fetched",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            exchange=exchange,
            token=token,
        )
        return quote_raw

    # ---------------------------------------------------------------------- #
    # Broker analytics                                                         #
    # ---------------------------------------------------------------------- #

    async def fetch_pcr(self) -> dict[str, Any]:
        """Fetch Put-Call Ratio (PCR) data from Angel One SmartAPI.

        Returns the PCR for tracked index derivatives.

        Returns:
            Raw PCR dict as returned by SmartAPI, with ``provider`` and
            ``sourceType`` metadata attached.

        Raises:
            ProviderAuthError:        Authentication failed.
            ProviderRateLimitedError: HTTP 429 from upstream.
            ProviderUnavailableError: HTTP 5xx or network error.
            ProviderDataError:        Malformed or error response.
        """
        logger.debug(
            "angel_one_fetch_pcr",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
        )

        body = await self._request("GET", _PCR_URL)

        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            raise ProviderDataError(
                f"Angel One PCR fetch failed: {error_msg}",
                provider=_PROVIDER_NAME,
            )

        data = body.get("data") or {}
        data["provider"] = _PROVIDER_NAME
        data["sourceType"] = _SOURCE_TYPE.value
        data["fetchedAt"] = _utc_iso_now()
        return data

    async def fetch_oi_buildup(self) -> list[dict[str, Any]]:
        """Fetch OI buildup data (long/short buildup, covering, unwinding).

        Returns:
            List of OI buildup records as returned by SmartAPI, each with
            ``provider`` and ``sourceType`` metadata attached.

        Raises:
            ProviderAuthError:        Authentication failed.
            ProviderRateLimitedError: HTTP 429 from upstream.
            ProviderUnavailableError: HTTP 5xx or network error.
            ProviderDataError:        Malformed or error response.
        """
        logger.debug(
            "angel_one_fetch_oi_buildup",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
        )

        body = await self._request("GET", _OI_BUILDUP_URL)

        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            raise ProviderDataError(
                f"Angel One OI buildup fetch failed: {error_msg}",
                provider=_PROVIDER_NAME,
            )

        records = body.get("data") or []
        if not isinstance(records, list):
            raise ProviderDataError(
                "Angel One OI buildup 'data' is not a list",
                provider=_PROVIDER_NAME,
            )

        fetched_at = _utc_iso_now()
        for record in records:
            if isinstance(record, dict):
                record["provider"] = _PROVIDER_NAME
                record["sourceType"] = _SOURCE_TYPE.value
                record["fetchedAt"] = fetched_at

        logger.debug(
            "angel_one_oi_buildup_fetched",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            record_count=len(records),
        )
        return records

    async def fetch_historical_oi(
        self,
        symbol: str,
        token: str,
        from_date: str,
        to_date: str,
        interval: str,
        exchange: str = "NFO",
    ) -> list[dict[str, Any]]:
        """Fetch historical Open Interest data from Angel One SmartAPI.

        Uses the dedicated OI endpoint which returns a time-series of OI
        values for derivative instruments.  This is separate from the OHLCV
        endpoint and is the authoritative source for historical OI from
        Angel One.

        Args:
            symbol:    Trading symbol (e.g. ``"NIFTY23DECFUT"``).
            token:     Angel One instrument token (numeric string).
            from_date: Start datetime as ``"YYYY-MM-DD HH:MM"`` (IST).
            to_date:   End datetime as ``"YYYY-MM-DD HH:MM"`` (IST).
            interval:  Canonical interval string.  Valid: 1m,5m,10m,15m,30m,1h,1d.
                       3m raises ProviderUnsupportedError.
            exchange:  Exchange code — typically ``"NFO"`` or ``"MCX"``.

        Returns:
            List of OI records with keys: timestamp, openInterest, provider.

        Raises:
            ProviderUnsupportedError: If interval is unsupported.
            ProviderAuthError:        Authentication failed.
            ProviderRateLimitedError: HTTP 429 from upstream.
            ProviderUnavailableError: HTTP 5xx or network error.
            ProviderDataError:        Malformed response.
        """
        if interval in _BANNED_INTERVALS:
            raise ProviderUnsupportedError(
                "interval 3m is permanently unsupported for Indian market data",
                provider=_PROVIDER_NAME,
            )

        smartapi_interval = _INTERVAL_MAP.get(interval)
        if smartapi_interval is None:
            raise ProviderUnsupportedError(
                f"Interval {interval!r} is not supported by Angel One getOIData",
                provider=_PROVIDER_NAME,
            )

        payload: dict[str, Any] = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": smartapi_interval,
            "fromdate": from_date,
            "todate": to_date,
        }

        logger.debug(
            "angel_one_fetch_historical_oi",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
            token=token,
        )

        body = await self._request("POST", _OI_URL, json_body=payload)

        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            if "no data" in error_msg.lower() or "no record" in error_msg.lower():
                raise ProviderMarketClosedError(
                    f"Angel One getOIData returned no data: {error_msg}",
                    provider=_PROVIDER_NAME,
                )
            raise ProviderDataError(
                f"Angel One getOIData failed: {error_msg}",
                provider=_PROVIDER_NAME,
            )

        raw_oi = body.get("data") or []
        if not isinstance(raw_oi, list):
            raise ProviderDataError(
                "Angel One getOIData 'data' field is not a list",
                provider=_PROVIDER_NAME,
            )

        # OI data format: [[timestamp, openInterest], ...]
        oi_records: list[dict[str, Any]] = []
        for row in raw_oi:
            if isinstance(row, list) and len(row) >= 2:
                oi_records.append({
                    "timestamp":     row[0],
                    "openInterest":  row[1],
                    "provider":      _PROVIDER_NAME,
                    "sourceType":    _SOURCE_TYPE.value,
                    "symbol":        symbol,
                    "exchange":      exchange,
                    "interval":      interval,
                })
            elif isinstance(row, dict):
                # Some response versions return dicts directly
                row["provider"] = _PROVIDER_NAME
                row["sourceType"] = _SOURCE_TYPE.value
                oi_records.append(row)

        logger.debug(
            "angel_one_historical_oi_fetched",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            symbol=symbol,
            interval=interval,
            record_count=len(oi_records),
        )
        return oi_records

    async def fetch_option_greeks(
        self,
        name: str,
        expiry_date: str,
    ) -> list[dict[str, Any]]:
        """Fetch option Greeks for an underlying + expiry from Angel One SmartAPI.

        Returns delta, gamma, theta, vega, implied volatility, trade volume,
        and open interest for all strikes of the given underlying on the
        specified expiry.

        Args:
            name:        Underlying symbol name, e.g. ``"NIFTY"``, ``"BANKNIFTY"``,
                         ``"RELIANCE"``.
            expiry_date: Expiry date as ``"DDMMMYYYY"`` (e.g. ``"29FEB2024"``).
                         Angel One uses this non-ISO format.

        Returns:
            List of Greeks records with keys: strikePrice, optionType, delta,
            gamma, theta, vega, impliedVolatility, tradeVolume, openInterest,
            provider.

        Raises:
            ProviderAuthError:        Authentication failed.
            ProviderRateLimitedError: HTTP 429 from upstream.
            ProviderUnavailableError: HTTP 5xx or network error.
            ProviderDataError:        Malformed or error response.
        """
        payload: dict[str, Any] = {
            "name": name,
            "expirydate": expiry_date,
        }

        logger.debug(
            "angel_one_fetch_option_greeks",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            name=name,
            expiry_date=expiry_date,
        )

        body = await self._request("POST", _OPTION_GREEK_URL, json_body=payload)

        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            raise ProviderDataError(
                f"Angel One optionGreek fetch failed: {error_msg}",
                provider=_PROVIDER_NAME,
            )

        records = body.get("data") or []
        if not isinstance(records, list):
            raise ProviderDataError(
                "Angel One optionGreek 'data' is not a list",
                provider=_PROVIDER_NAME,
            )

        fetched_at = _utc_iso_now()
        for record in records:
            if isinstance(record, dict):
                record["provider"] = _PROVIDER_NAME
                record["sourceType"] = _SOURCE_TYPE.value
                record["fetchedAt"] = fetched_at
                record["underlyingName"] = name
                record["expiryDate"] = expiry_date

        logger.debug(
            "angel_one_option_greeks_fetched",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            name=name,
            expiry_date=expiry_date,
            record_count=len(records),
        )
        return records

    async def fetch_ltp(
        self,
        token: str,
        exchange: str,
        trading_symbol: str,
    ) -> dict[str, Any]:
        """Fetch LTP for a single instrument using the lightweight getLtpData endpoint.

        This is more efficient than fetch_live_quote (FULL mode) when only the
        last traded price is needed — it avoids the full market data response.

        Args:
            token:           Angel One instrument token.
            exchange:        Exchange code (e.g. ``"NSE"``, ``"NFO"``).
            trading_symbol:  Trading symbol (e.g. ``"RELIANCE-EQ"``).

        Returns:
            Dict with ltp, tradingsymbol, symboltoken, exchange.

        Raises:
            ProviderMarketClosedError: Market is closed (empty data).
            ProviderAuthError:         Authentication failed.
            ProviderRateLimitedError:  HTTP 429 from upstream.
            ProviderUnavailableError:  HTTP 5xx or network error.
            ProviderDataError:         Malformed response.
        """
        payload: dict[str, Any] = {
            "exchange": exchange,
            "tradingsymbol": trading_symbol,
            "symboltoken": token,
        }

        logger.debug(
            "angel_one_fetch_ltp",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
            exchange=exchange,
            token=token,
        )

        body = await self._request("POST", _LTP_URL, json_body=payload)

        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            raise ProviderDataError(
                f"Angel One getLtpData failed: {error_msg}",
                provider=_PROVIDER_NAME,
            )

        data = body.get("data") or {}
        data["provider"] = _PROVIDER_NAME
        data["sourceType"] = _SOURCE_TYPE.value
        return data

    async def fetch_nse_intraday(self) -> dict[str, Any]:
        """Fetch NSE intraday market breadth / OHLC summary data.

        Returns:
            Raw NSE intraday data dict with provider metadata attached.

        Raises:
            ProviderAuthError:        Authentication failed.
            ProviderRateLimitedError: HTTP 429 from upstream.
            ProviderUnavailableError: HTTP 5xx or network error.
            ProviderDataError:        Malformed or error response.
        """
        logger.debug(
            "angel_one_fetch_nse_intraday",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
        )

        body = await self._request("GET", _NSE_INTRADAY_URL)

        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            raise ProviderDataError(
                f"Angel One nseIntraday fetch failed: {error_msg}",
                provider=_PROVIDER_NAME,
            )

        data = body.get("data") or {}
        if isinstance(data, dict):
            data["provider"] = _PROVIDER_NAME
            data["sourceType"] = _SOURCE_TYPE.value
            data["fetchedAt"] = _utc_iso_now()
        return data

    async def fetch_gainers_losers(self) -> dict[str, Any]:
        """Fetch top OI/price gainers and losers from Angel One SmartAPI.

        Returns:
            Raw dict containing gainers and losers lists as returned by
            SmartAPI, with ``provider`` and ``sourceType`` metadata attached.

        Raises:
            ProviderAuthError:        Authentication failed.
            ProviderRateLimitedError: HTTP 429 from upstream.
            ProviderUnavailableError: HTTP 5xx or network error.
            ProviderDataError:        Malformed or error response.
        """
        logger.debug(
            "angel_one_fetch_gainers_losers",
            component="angel_one_adapter",
            provider=_PROVIDER_NAME,
        )

        body = await self._request("GET", _GAINERS_LOSERS_URL)

        if not body.get("status", False):
            error_msg = body.get("message", "Unknown error")
            raise ProviderDataError(
                f"Angel One gainers/losers fetch failed: {error_msg}",
                provider=_PROVIDER_NAME,
            )

        data = body.get("data") or {}
        if not isinstance(data, dict):
            raise ProviderDataError(
                "Angel One gainers/losers 'data' is not a dict",
                provider=_PROVIDER_NAME,
            )

        data["provider"] = _PROVIDER_NAME
        data["sourceType"] = _SOURCE_TYPE.value
        data["fetchedAt"] = _utc_iso_now()
        return data


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_retry_after(response: httpx.Response) -> Optional[int]:
    """Extract the ``Retry-After`` header value as integer seconds.

    Returns ``None`` if the header is absent or non-integer (callers apply
    the 60-second default as per Requirement 5.6).
    """
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _parse_json(response: httpx.Response, *, provider: str) -> Any:
    """Parse the JSON body of an httpx Response, raising ProviderDataError on failure."""
    try:
        return response.json()
    except Exception as exc:
        raise ProviderDataError(
            f"Angel One response is not valid JSON (status={response.status_code})",
            provider=provider,
            status_code=response.status_code,
        ) from exc


def _utc_iso_now() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )
