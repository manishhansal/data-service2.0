"""
src/auth/angel_one_jwt.py

Angel One JWT store and rotation scheduler for DATA-SERVICE 2.0.

Task 13.5 — Requirements 19.1, 19.7

The Angel One SmartAPI JWT expires every 24 hours.  This module provides:

1. ``AngelOneJwtStore`` — An in-memory credential store that holds the
   current JWT, its expiry, and exposes helpers to check validity.

2. ``AngelOneJwtRotator`` — Calls the Angel One SmartAPI login endpoint
   to obtain a fresh JWT (using TOTP), and starts a long-running asyncio
   task that rotates the token every ``interval_hours`` hours (default 23).

Rotation is scheduled at 23:55 IST daily by the scheduler layer (APScheduler
job in ``src/scheduler_jobs/``), which calls ``AngelOneJwtRotator.rotate()``.
This module itself only contains the pure rotation logic; scheduling wiring
belongs to the scheduler entry-point.

Security rules (Requirements 19.1, 19.4)
-----------------------------------------
- The JWT value, TOTP secret, and API key are **never** written to logs,
  traces, or error messages.
- Log entries use opaque placeholders (e.g. ``"<redacted>"``) for all
  credential references.
- The store's ``get_jwt()`` method is the **only** public accessor for the
  token value.  No other module should hold a copy.

Thread / concurrency safety
-----------------------------
``AngelOneJwtStore`` uses an ``asyncio.Lock`` to serialise writes and reads
from multiple concurrent coroutines in the same event loop.  It is **not**
safe to share instances across multiple OS processes; each process should
maintain its own store (the adapter in each process authenticates independently).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import pyotp

from src.core.settings import Settings
from src.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDER_NAME = "angel_one"

# Angel One SmartAPI login endpoint (Task description + angel_one.py adapter)
_LOGIN_URL = (
    "https://apiconnect.angelbroking.com"
    "/rest/auth/angelbroking/user/v1/loginByPassword"
)

# JWTs are considered "near expiry" when fewer than this many seconds remain.
# Using 5 minutes (300 s) as the pre-expiry safety window.
_NEAR_EXPIRY_WINDOW_SEC: int = 5 * 60  # 300 s

# Default rotation cycle (hours).  Rotates just before the 24-hour expiry.
_DEFAULT_ROTATION_INTERVAL_HOURS: int = 23

# Max retry attempts and delay between retries on rotation failure.
_MAX_ROTATION_RETRIES: int = 3
_RETRY_DELAY_SEC: float = 60.0


# ---------------------------------------------------------------------------
# AngelOneJwtStore
# ---------------------------------------------------------------------------


class AngelOneJwtStore:
    """In-memory store for the Angel One SmartAPI JWT access token.

    The store is intentionally simple — it keeps a single JWT in memory and
    exposes helpers used by the adapter and the rotation scheduler.

    Locking
    -------
    All public methods acquire ``_lock`` to avoid read-during-write races in
    multi-coroutine scenarios.

    Args:
        near_expiry_window_sec:
            Seconds before nominal expiry at which ``is_valid()`` returns
            ``False``.  Defaults to 300 (5 minutes).

    Example::

        store = AngelOneJwtStore()
        store.set_jwt("eyJ...", expires_at=datetime.now(UTC) + timedelta(hours=24))
        if store.is_valid():
            token = store.get_jwt()
    """

    def __init__(self, near_expiry_window_sec: int = _NEAR_EXPIRY_WINDOW_SEC) -> None:
        self._jwt: Optional[str] = None
        self._expires_at: Optional[datetime] = None
        self._near_expiry_window_sec = near_expiry_window_sec
        self._lock: asyncio.Lock = asyncio.Lock()

    # ---------------------------------------------------------------------- #
    # Public API                                                               #
    # ---------------------------------------------------------------------- #

    def get_jwt(self) -> Optional[str]:
        """Return the current JWT string if it is valid, else ``None``.

        This is a **synchronous** helper for use inside hot paths (e.g. the
        adapter's ``_auth_headers()``).  It does not acquire the async lock;
        reads of a single Python object reference are atomic at the CPython
        level.  Writers must use ``set_jwt()`` / ``clear()`` which acquire
        the async lock when called from async contexts.

        Returns:
            The JWT string when present and not within the near-expiry window,
            otherwise ``None``.
        """
        if not self._jwt or not self._expires_at:
            return None
        remaining = (self._expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= self._near_expiry_window_sec:
            return None
        return self._jwt

    async def set_jwt(self, jwt: str, expires_at: datetime) -> None:
        """Store a new JWT with its expiry timestamp.

        Args:
            jwt:        The raw JWT access token string.  Never logged.
            expires_at: The UTC ``datetime`` at which the token expires.
                        Must be timezone-aware.

        Raises:
            ValueError: ``jwt`` is empty, or ``expires_at`` is naive.
        """
        if not jwt:
            raise ValueError("jwt must be a non-empty string")
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be a timezone-aware datetime (UTC)")

        async with self._lock:
            self._jwt = jwt
            self._expires_at = expires_at
            logger.info(
                "angel_one_jwt_stored",
                component="angel_one_jwt_store",
                provider=_PROVIDER_NAME,
                # expires_at is safe to log (timestamp, not a secret)
                expires_at_utc=expires_at.isoformat(),
            )

    def is_valid(self) -> bool:
        """Return ``True`` when a JWT is present and not near expiry.

        "Near expiry" is defined as fewer than ``near_expiry_window_sec``
        seconds remaining before the nominal expiry.

        Returns:
            ``True`` if the stored JWT is valid and has at least
            ``near_expiry_window_sec`` seconds left, ``False`` otherwise.
        """
        return self.get_jwt() is not None

    async def clear(self) -> None:
        """Clear the stored JWT and expiry, forcing re-authentication on next use."""
        async with self._lock:
            self._jwt = None
            self._expires_at = None
            logger.debug(
                "angel_one_jwt_cleared",
                component="angel_one_jwt_store",
                provider=_PROVIDER_NAME,
            )

    @property
    def expires_at(self) -> Optional[datetime]:
        """Return the expiry datetime of the current JWT, or ``None``."""
        return self._expires_at


# ---------------------------------------------------------------------------
# AngelOneJwtRotator
# ---------------------------------------------------------------------------


class AngelOneJwtRotator:
    """Fetches and periodically rotates the Angel One SmartAPI JWT.

    ``rotate()`` performs a single rotation:
    1. Generates a fresh TOTP code from the configured secret.
    2. POSTs to the Angel One login endpoint.
    3. On success: writes the new JWT into the provided ``AngelOneJwtStore``.
    4. On failure: logs a WARNING (credentials are never logged).

    ``start_rotation_task()`` wraps ``rotate()`` in a long-running asyncio
    task that retries on failure and sleeps between successful rotations.

    Security (Requirements 19.1, 19.4)
    ------------------------------------
    - ``api_key``, ``totp_secret``, and the JWT value are never written to
      any log entry.
    - ``client_id`` is logged only as a presence indicator (not its value).
    """

    # ------------------------------------------------------------------ #
    # Single rotation                                                      #
    # ------------------------------------------------------------------ #

    async def rotate(
        self,
        settings: Settings,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> Optional[str]:
        """Obtain a fresh JWT from Angel One SmartAPI.

        Generates a TOTP code, calls the SmartAPI login endpoint, and returns
        the JWT access token string on success.

        Args:
            settings:
                Platform settings; must have ``angel_one_api_key``,
                ``angel_one_client_id``, and ``angel_one_totp_secret`` set.
            http_client:
                Optional pre-built ``httpx.AsyncClient`` (injected in tests).
                When ``None`` (default) a temporary client is created and
                closed after the request.

        Returns:
            The JWT access-token string on success, or ``None`` on failure.

        Security note: The JWT value is returned to the caller (the store
        writer) but is **never** written to logs.
        """
        api_key = settings.angel_one_api_key
        client_id = settings.angel_one_client_id
        totp_secret = settings.angel_one_totp_secret

        if not api_key or not client_id or not totp_secret:
            logger.error(
                "angel_one_jwt_rotation_credentials_missing",
                component="angel_one_jwt_rotator",
                provider=_PROVIDER_NAME,
                has_api_key=bool(api_key),
                has_client_id=bool(client_id),
                has_totp_secret=bool(totp_secret),
            )
            return None

        totp_code = pyotp.TOTP(totp_secret).now()

        payload = {
            "clientcode": client_id,
            "password": totp_code,  # Angel One uses TOTP as the login OTP
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
            "X-PrivateKey": api_key,  # API key in header — never in logs
        }

        owns_client = http_client is None
        if owns_client:
            http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

        try:
            resp = await http_client.post(_LOGIN_URL, json=payload, headers=headers)
        except httpx.TimeoutException:
            logger.warning(
                "angel_one_jwt_rotation_timeout",
                component="angel_one_jwt_rotator",
                provider=_PROVIDER_NAME,
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                "angel_one_jwt_rotation_request_error",
                component="angel_one_jwt_rotator",
                provider=_PROVIDER_NAME,
                error_type=type(exc).__name__,
            )
            return None
        finally:
            if owns_client:
                await http_client.aclose()

        if resp.status_code == 429:
            logger.warning(
                "angel_one_jwt_rotation_rate_limited",
                component="angel_one_jwt_rotator",
                provider=_PROVIDER_NAME,
                status_code=429,
            )
            return None

        if resp.status_code >= 500:
            logger.warning(
                "angel_one_jwt_rotation_server_error",
                component="angel_one_jwt_rotator",
                provider=_PROVIDER_NAME,
                status_code=resp.status_code,
            )
            return None

        if resp.status_code != 200:
            logger.warning(
                "angel_one_jwt_rotation_unexpected_status",
                component="angel_one_jwt_rotator",
                provider=_PROVIDER_NAME,
                status_code=resp.status_code,
            )
            return None

        # Parse response body
        try:
            body: dict = resp.json()
        except Exception:  # noqa: BLE001
            logger.warning(
                "angel_one_jwt_rotation_malformed_response",
                component="angel_one_jwt_rotator",
                provider=_PROVIDER_NAME,
            )
            return None

        if not body.get("status", False):
            error_msg = body.get("message", "unknown")
            logger.warning(
                "angel_one_jwt_rotation_api_rejected",
                component="angel_one_jwt_rotator",
                provider=_PROVIDER_NAME,
                api_message=error_msg,  # message is not a secret — safe to log
            )
            return None

        data = body.get("data") or {}
        jwt_token: Optional[str] = data.get("jwtToken")

        if not jwt_token:
            logger.warning(
                "angel_one_jwt_rotation_missing_token",
                component="angel_one_jwt_rotator",
                provider=_PROVIDER_NAME,
            )
            return None

        logger.info(
            "angel_one_jwt_rotation_success",
            component="angel_one_jwt_rotator",
            provider=_PROVIDER_NAME,
            # Never log jwt_token value
        )
        return jwt_token

    # ------------------------------------------------------------------ #
    # Background rotation task                                            #
    # ------------------------------------------------------------------ #

    async def start_rotation_task(
        self,
        settings: Settings,
        store: AngelOneJwtStore,
        interval_hours: int = _DEFAULT_ROTATION_INTERVAL_HOURS,
    ) -> asyncio.Task:  # type: ignore[type-arg]
        """Start a long-running asyncio task that rotates the JWT periodically.

        The task:
        1. Performs an immediate rotation on startup.
        2. Sleeps for ``interval_hours * 3600`` seconds.
        3. Repeats until cancelled.

        On rotation failure the task retries up to ``_MAX_ROTATION_RETRIES``
        times at ``_RETRY_DELAY_SEC``-second intervals before emitting an
        alert-level log and waiting until the next scheduled cycle.

        The task stops gracefully when cancelled (``asyncio.CancelledError``
        is not re-raised).

        Args:
            settings:       Platform settings (credentials sourced from here).
            store:          The ``AngelOneJwtStore`` to write new JWTs into.
            interval_hours: Hours between rotation cycles (default 23).

        Returns:
            The running ``asyncio.Task`` object.  The caller is responsible
            for storing a reference to prevent premature garbage collection.
        """
        if interval_hours < 1:
            raise ValueError(f"interval_hours must be >= 1, got {interval_hours}")

        task = asyncio.create_task(
            self._rotation_loop(settings, store, interval_hours),
            name="angel_one_jwt_rotation",
        )

        logger.info(
            "angel_one_jwt_rotation_task_started",
            component="angel_one_jwt_rotator",
            provider=_PROVIDER_NAME,
            interval_hours=interval_hours,
        )
        return task

    async def _rotation_loop(
        self,
        settings: Settings,
        store: AngelOneJwtStore,
        interval_hours: int,
    ) -> None:
        """Internal coroutine that drives the rotation loop.

        Runs until cancelled.  On ``asyncio.CancelledError``, cleans up and
        returns without re-raising so the task exits cleanly.
        """
        sleep_seconds = interval_hours * 3600

        while True:
            # Attempt rotation with retries
            jwt_token = await self._rotate_with_retries(settings)

            if jwt_token is not None:
                # Store with a 24-hour nominal expiry (Angel One JWT lifetime)
                expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
                await store.set_jwt(jwt_token, expires_at=expires_at)
            else:
                logger.error(
                    "angel_one_jwt_rotation_all_retries_exhausted",
                    component="angel_one_jwt_rotator",
                    provider=_PROVIDER_NAME,
                    max_retries=_MAX_ROTATION_RETRIES,
                    # Requirement 19.7: alert when all retries exhausted
                )

            try:
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                logger.info(
                    "angel_one_jwt_rotation_task_cancelled",
                    component="angel_one_jwt_rotator",
                    provider=_PROVIDER_NAME,
                )
                return

    async def _rotate_with_retries(
        self,
        settings: Settings,
        max_retries: int = _MAX_ROTATION_RETRIES,
        retry_delay_sec: float = _RETRY_DELAY_SEC,
    ) -> Optional[str]:
        """Attempt ``rotate()`` up to ``max_retries`` times.

        On each failure, waits ``retry_delay_sec`` seconds before the next
        attempt.

        Args:
            settings:        Platform settings.
            max_retries:     Maximum number of attempts (default 3).
            retry_delay_sec: Seconds between attempts (default 60).

        Returns:
            JWT string on first success, or ``None`` after all retries fail.
        """
        for attempt in range(1, max_retries + 1):
            jwt_token = await self.rotate(settings)
            if jwt_token is not None:
                return jwt_token

            if attempt < max_retries:
                logger.warning(
                    "angel_one_jwt_rotation_retry",
                    component="angel_one_jwt_rotator",
                    provider=_PROVIDER_NAME,
                    attempt=attempt,
                    max_retries=max_retries,
                    retry_in_sec=retry_delay_sec,
                )
                try:
                    await asyncio.sleep(retry_delay_sec)
                except asyncio.CancelledError:
                    logger.info(
                        "angel_one_jwt_rotation_cancelled_during_retry",
                        component="angel_one_jwt_rotator",
                        provider=_PROVIDER_NAME,
                    )
                    return None

        return None
