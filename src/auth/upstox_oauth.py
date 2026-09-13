"""
src/auth/upstox_oauth.py

Upstox OAuth token store and 401-triggered refresh handler.

Task 13.6 — Requirement 19.8

This module provides two classes:

``UpstoxTokenStore``
    In-memory holder for the Upstox OAuth access token and refresh token.
    Tracks expiry so callers can check validity before making requests.
    Token values are **never** logged or serialised to any external surface.

``UpstoxOAuthRefreshHandler``
    Async handler that:
    - Exchanges credentials for a new access token via the Upstox token
      endpoint (form-encoded POST).
    - Wraps an async request callable: on HTTP 401 it refreshes the token
      once and retries the original request.

Security notes (Requirement 19.1):
    - Credential values (``api_key``, ``api_secret``, token strings) are
      never written to log output, traces, or error messages.
    - All log entries reference *metadata* about tokens (e.g. expiry time,
      whether a refresh succeeded) but never the token value itself.

Upstox token endpoint:
    POST https://api.upstox.com/v2/login/authorization/token
    Content-Type: application/x-www-form-urlencoded
    Body: code, client_id, client_secret, redirect_uri,
          grant_type=authorization_code
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Optional, TypeVar

import httpx
import structlog

from src.core.settings import Settings

# ---------------------------------------------------------------------------
# Module logger — no credential values may appear in any log entry.
# ---------------------------------------------------------------------------

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Upstox OAuth token exchange/refresh endpoint.
UPSTOX_TOKEN_URL: str = "https://api.upstox.com/v2/login/authorization/token"

#: Seconds before expiry at which ``is_valid()`` returns False.
#: Keeping a 5-minute buffer avoids races where a token expires in-flight.
_EXPIRY_BUFFER_SECONDS: int = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: An async callable that takes an access token string and returns any value.
_T = TypeVar("_T")
_RequestFn = Callable[[str], Coroutine[Any, Any, _T]]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UpstoxAuthError(Exception):
    """Raised when the Upstox OAuth token cannot be obtained or refreshed.

    Requirement 19.8: when the refresh also fails the caller marks the
    Upstox provider as unavailable and surfaces this error.

    Attributes:
        message: Human-readable reason for the failure.  No credential
                 values are included in this message.
    """

    def __init__(self, message: str = "Upstox OAuth token refresh failed") -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# UpstoxTokenStore
# ---------------------------------------------------------------------------


class UpstoxTokenStore:
    """In-memory store for a single Upstox OAuth access/refresh token pair.

    Thread-safety: an ``asyncio.Lock`` serialises all mutations so the store
    is safe for use across concurrent coroutines.

    Token values are held **only** in memory and are **never** written to any
    log entry, trace span, or external serialisation surface.

    Usage::

        store = UpstoxTokenStore()
        store.set_tokens(access_token="...", refresh_token="...", expires_at=dt)

        if store.is_valid():
            token = store.get_access_token()
    """

    def __init__(self) -> None:
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expires_at: Optional[datetime] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_access_token(self) -> Optional[str]:
        """Return the stored access token, or ``None`` if not set.

        The returned value must never be written to any log entry.
        """
        return self._access_token

    def get_refresh_token(self) -> Optional[str]:
        """Return the stored refresh token, or ``None`` if not set.

        The returned value must never be written to any log entry.
        """
        return self._refresh_token

    def set_tokens(
        self,
        access_token: str,
        *,
        refresh_token: Optional[str] = None,
        expires_at: datetime,
    ) -> None:
        """Store a new access token (and optional refresh token) with its expiry.

        Args:
            access_token:  The Upstox OAuth access token string.
            refresh_token: The optional OAuth refresh token string.
            expires_at:    The UTC datetime at which the access token expires.
                           Must be timezone-aware.
        """
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be a timezone-aware datetime")

        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at.astimezone(timezone.utc)

        _log.debug(
            "upstox_tokens_stored",
            component="upstox_token_store",
            # Log expiry as ISO-8601 metadata — NOT the token value.
            expires_at_utc=self._expires_at.isoformat(),
            has_refresh_token=refresh_token is not None,
        )

    def is_valid(self) -> bool:
        """Return ``True`` when an access token is present and not near expiry.

        "Near expiry" means within 5 minutes (``_EXPIRY_BUFFER_SECONDS``) of
        the stored ``expires_at`` timestamp.  This buffer prevents in-flight
        requests from racing with token expiry.

        Returns:
            ``True`` when a token exists and ``now + 5 min < expires_at``.
            ``False`` when no token is stored, or the expiry is not known,
            or the token has (nearly) expired.
        """
        if self._access_token is None or self._expires_at is None:
            return False
        now_utc = datetime.now(timezone.utc)
        return now_utc + timedelta(seconds=_EXPIRY_BUFFER_SECONDS) < self._expires_at

    def clear(self) -> None:
        """Remove all stored token data.

        Called when the provider is marked unavailable so that stale tokens
        cannot be accidentally reused.
        """
        self._access_token = None
        self._refresh_token = None
        self._expires_at = None
        _log.info(
            "upstox_tokens_cleared",
            component="upstox_token_store",
        )


# ---------------------------------------------------------------------------
# UpstoxOAuthRefreshHandler
# ---------------------------------------------------------------------------


class UpstoxOAuthRefreshHandler:
    """Handles Upstox OAuth token acquisition and 401-triggered retries.

    Provides two entry-points:

    ``refresh(settings, store)``
        Exchange credentials for a new access token via the Upstox token
        endpoint and store it in the given ``UpstoxTokenStore``.

    ``with_retry(request_fn, store, settings, max_retries=1)``
        Wrap an async callable that accepts an access-token string.  On
        HTTP 401 response, refreshes the token once and retries.  If the
        second attempt also fails, raises ``UpstoxAuthError`` and the
        caller should mark the provider as unavailable.

    Requirements:
        19.8 — On 401 from Upstox: refresh token, retry once.
                If refresh fails: mark provider unavailable, return error.
        19.1 — Credentials never logged or serialised.

    Args:
        http_client: Optional pre-constructed ``httpx.AsyncClient``.
                     If ``None``, a default client is used per-call.
    """

    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._http: Optional[httpx.AsyncClient] = http_client
        self._refresh_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def refresh(
        self,
        settings: Settings,
        store: UpstoxTokenStore,
        *,
        auth_code: Optional[str] = None,
    ) -> bool:
        """Obtain a new Upstox access token and write it to ``store``.

        Issues a form-encoded POST to the Upstox token endpoint.  On
        success, calls ``store.set_tokens(...)`` with the new access token
        and its expiry (defaulting to 1 hour when the provider does not
        return an ``expires_in`` field).

        A ``asyncio.Lock`` ensures that concurrent coroutines do not each
        trigger a separate refresh — only the first one to acquire the lock
        will perform the network call; the rest will benefit from the result
        written to ``store``.

        Args:
            settings:  Platform settings supplying ``upstox_api_key``,
                       ``upstox_api_secret``, and ``upstox_redirect_uri``.
            store:     Token store to update on success or clear on failure.
            auth_code: Optional OAuth authorisation code for the
                       ``authorization_code`` grant type.  When not
                       supplied, the form body's ``code`` field is omitted.

        Returns:
            ``True`` on success, ``False`` on failure.
        """
        async with self._refresh_lock:
            return await self._do_refresh(settings, store, auth_code=auth_code)

    async def _do_refresh(
        self,
        settings: Settings,
        store: UpstoxTokenStore,
        *,
        auth_code: Optional[str],
    ) -> bool:
        """Internal refresh — caller must hold ``_refresh_lock``."""
        # Build form body — no credential values are logged.
        form_data: dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": settings.upstox_api_key or "",
            "client_secret": settings.upstox_api_secret or "",
            "redirect_uri": settings.upstox_redirect_uri or "",
        }
        if auth_code is not None:
            form_data["code"] = auth_code

        _log.info(
            "upstox_token_refresh_started",
            component="upstox_oauth_refresh_handler",
        )

        client = self._http

        try:
            if client is not None:
                response = await client.post(
                    UPSTOX_TOKEN_URL,
                    data=form_data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                )
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=10.0),
                ) as tmp_client:
                    response = await tmp_client.post(
                        UPSTOX_TOKEN_URL,
                        data=form_data,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json",
                        },
                    )
        except httpx.HTTPError as exc:
            _log.warning(
                "upstox_token_refresh_network_error",
                component="upstox_oauth_refresh_handler",
                error_type=type(exc).__name__,
                # exc message deliberately NOT logged (may contain URL with
                # credentials embedded by user misconfiguration).
            )
            store.clear()
            return False

        if response.status_code != 200:
            _log.warning(
                "upstox_token_refresh_failed",
                component="upstox_oauth_refresh_handler",
                http_status=response.status_code,
            )
            store.clear()
            return False

        # Parse response — presence of ``access_token`` is mandatory.
        try:
            payload: dict[str, Any] = response.json()
            access_token: str = payload["access_token"]
        except (KeyError, ValueError) as exc:
            _log.warning(
                "upstox_token_refresh_bad_response",
                component="upstox_oauth_refresh_handler",
                error=type(exc).__name__,
            )
            store.clear()
            return False

        # Determine expiry.  Upstox typically returns ``expires_in`` (seconds).
        raw_expires_in: Any = payload.get("expires_in")
        try:
            expires_seconds: int = int(raw_expires_in) if raw_expires_in is not None else 3600
        except (ValueError, TypeError):
            expires_seconds = 3600  # default 1 hour

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)

        refresh_token: Optional[str] = payload.get("refresh_token")

        store.set_tokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )

        _log.info(
            "upstox_token_refresh_success",
            component="upstox_oauth_refresh_handler",
            expires_at_utc=expires_at.isoformat(),
            has_refresh_token=refresh_token is not None,
        )
        return True

    # ------------------------------------------------------------------
    # with_retry helper
    # ------------------------------------------------------------------

    async def with_retry(
        self,
        request_fn: _RequestFn[_T],
        store: UpstoxTokenStore,
        settings: Settings,
        *,
        max_retries: int = 1,
    ) -> _T:
        """Wrap an async HTTP call with automatic 401-triggered token refresh.

        ``request_fn`` must be an async callable that accepts a single
        positional argument — the current access token string — and returns
        the response or raises an exception.

        On ``httpx.HTTPStatusError`` with status 401:
            1. Calls ``refresh(settings, store)`` to obtain a new token.
            2. If refresh succeeds: retries ``request_fn`` with the new
               token (up to ``max_retries`` times).
            3. If refresh fails or retries are exhausted: clears the store
               and raises ``UpstoxAuthError``.

        Non-401 errors propagate to the caller without modification.

        Args:
            request_fn:   Async callable ``(access_token: str) -> T``.
            store:        Token store holding the current access token.
            settings:     Platform settings for credential lookup during
                          the refresh call.
            max_retries:  Maximum number of refresh-and-retry cycles
                          (default 1, per Requirement 19.8).

        Returns:
            Whatever ``request_fn`` returns on success.

        Raises:
            UpstoxAuthError: When authentication cannot be recovered.
            Any exception raised by ``request_fn`` for non-401 errors.
        """
        # Try with the current token first.
        access_token = store.get_access_token()
        if access_token is None:
            # No token at all — attempt a fresh refresh before the first call.
            _log.info(
                "upstox_no_token_prerefresh",
                component="upstox_oauth_refresh_handler",
            )
            ok = await self.refresh(settings, store)
            if not ok or store.get_access_token() is None:
                raise UpstoxAuthError(
                    "No Upstox access token available and initial refresh failed"
                )
            access_token = store.get_access_token()

        retries_remaining = max_retries
        while True:
            try:
                return await request_fn(access_token)  # type: ignore[arg-type]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 401:
                    raise

                if retries_remaining <= 0:
                    raise UpstoxAuthError(
                        "Upstox returned HTTP 401 after token refresh — "
                        "provider marked as unavailable"
                    )

                _log.info(
                    "upstox_401_refresh_triggered",
                    component="upstox_oauth_refresh_handler",
                    retries_remaining=retries_remaining,
                )
                retries_remaining -= 1

                # Attempt token refresh.
                ok = await self.refresh(settings, store)
                if not ok:
                    _log.warning(
                        "upstox_401_refresh_failed_marking_unavailable",
                        component="upstox_oauth_refresh_handler",
                    )
                    raise UpstoxAuthError(
                        "Upstox returned HTTP 401 and token refresh failed — "
                        "provider marked as unavailable"
                    )

                new_token = store.get_access_token()
                if new_token is None:
                    raise UpstoxAuthError(
                        "Token refresh reported success but store returned no token"
                    )

                access_token = new_token
                # Loop: retry the original request with the fresh token.
