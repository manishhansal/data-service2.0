"""
Consumer rate-limiting middleware for DATA-SERVICE 2.0.

Task 13.3 — Requirements 19.3, 19.6

Implements a sliding-window (deque of timestamps) per-consumer rate limiter
and a Starlette ``BaseHTTPMiddleware`` that enforces the limit on every
incoming request.

Design decisions
----------------
* **Sliding window** — more accurate than a fixed-window counter; a consumer
  who sends exactly `limit` requests at 00:59 cannot reset the window and
  send another `limit` at 01:00.
* **Per-consumer identification** — uses the ``X-Forwarded-For`` header when
  present (reverse-proxy deployments), falling back to the raw client host
  from the ASGI connection scope.
* **In-process only** — state is stored in process memory, suitable for a
  single-replica deployment.  For multi-replica deployments, Redis-backed
  rate limiting (Token Bucket Lua scripts, see Task 4.3) should be preferred;
  this implementation is the lightweight fallback.
* **asyncio.Lock** — one lock per consumer ID prevents concurrent coroutines
  from corrupting the deque.  A single global lock is intentionally avoided
  to minimise lock contention across consumers.

Response headers added to every reply::

    X-RateLimit-Limit:     <max requests per window>
    X-RateLimit-Remaining: <remaining requests in current window>
    X-RateLimit-Reset:     <UTC epoch seconds when the oldest request expires>
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default request limit per window.
DEFAULT_LIMIT: Final[int] = 100

#: Default sliding window duration in seconds.
DEFAULT_WINDOW_SEC: Final[int] = 60

# Paths that are never rate-limited (liveness probe, Prometheus scrape).
_EXEMPT_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/v1/health/live",
        "/metrics",
    }
)


# ---------------------------------------------------------------------------
# Core sliding-window rate limiter
# ---------------------------------------------------------------------------


class SlidingWindowRateLimiter:
    """
    In-process sliding-window rate limiter.

    Each consumer is tracked by an arbitrary string key (typically their IP
    address or an API-key identifier).  For each key a ``deque[float]`` of
    request timestamps is maintained.  On every call to :meth:`is_allowed`
    stale entries (older than ``window_sec``) are pruned before the check,
    so the deque never grows beyond ``limit`` entries in practice.

    Thread-safety is provided by a per-consumer ``asyncio.Lock``.  This
    makes the class safe for use inside async request handlers without
    requiring an external mutex.

    Parameters
    ----------
    limit:
        Default maximum requests allowed per window.  Can be overridden
        per-call.
    window_sec:
        Default sliding-window duration in seconds.  Can be overridden
        per-call.
    """

    def __init__(
        self,
        limit: int = DEFAULT_LIMIT,
        window_sec: int = DEFAULT_WINDOW_SEC,
    ) -> None:
        self._default_limit = limit
        self._default_window_sec = window_sec

        # consumer_id → deque of request timestamps (monotonic, seconds)
        self._windows: dict[str, deque[float]] = {}
        # consumer_id → asyncio.Lock
        self._locks: dict[str, asyncio.Lock] = {}
        # A single lock to protect the creation of per-consumer entries.
        self._registry_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_lock(self, consumer_id: str) -> asyncio.Lock:
        """Return (or lazily create) the per-consumer lock."""
        if consumer_id not in self._locks:
            async with self._registry_lock:
                # Double-check after acquiring the registry lock.
                if consumer_id not in self._locks:
                    self._locks[consumer_id] = asyncio.Lock()
                    self._windows[consumer_id] = deque()
        return self._locks[consumer_id]

    def _prune(self, window: deque[float], cutoff: float) -> None:
        """Remove timestamps older than *cutoff* from the left of *window*."""
        while window and window[0] <= cutoff:
            window.popleft()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_allowed(
        self,
        consumer_id: str,
        limit: int | None = None,
        window_sec: int | None = None,
    ) -> bool:
        """
        Record a request attempt and return whether it is within the limit.

        Parameters
        ----------
        consumer_id:
            Arbitrary string identifying the consumer (e.g. IP address).
        limit:
            Maximum requests allowed in the window.  Defaults to the
            instance-level ``limit``.
        window_sec:
            Window duration in seconds.  Defaults to the instance-level
            ``window_sec``.

        Returns
        -------
        bool
            ``True`` if the request is allowed; ``False`` if the consumer
            has exhausted their quota for the current window.
        """
        effective_limit = limit if limit is not None else self._default_limit
        effective_window = window_sec if window_sec is not None else self._default_window_sec

        lock = await self._get_lock(consumer_id)
        async with lock:
            window = self._windows[consumer_id]
            now = time.monotonic()
            cutoff = now - effective_window

            self._prune(window, cutoff)

            if len(window) >= effective_limit:
                return False

            window.append(now)
            return True

    async def get_usage(
        self,
        consumer_id: str,
        window_sec: int | None = None,
    ) -> int:
        """
        Return the number of requests recorded in the current window.

        This method does **not** add a new timestamp; it is purely
        observational.

        Parameters
        ----------
        consumer_id:
            Consumer to query.
        window_sec:
            Window duration.  Defaults to the instance-level ``window_sec``.

        Returns
        -------
        int
            Count of requests in the current sliding window.
        """
        effective_window = window_sec if window_sec is not None else self._default_window_sec
        lock = await self._get_lock(consumer_id)
        async with lock:
            window = self._windows[consumer_id]
            now = time.monotonic()
            cutoff = now - effective_window
            self._prune(window, cutoff)
            return len(window)

    async def get_reset_epoch(
        self,
        consumer_id: str,
        window_sec: int | None = None,
    ) -> int:
        """
        Return the UTC epoch second at which the oldest in-window request
        will expire and free up capacity.

        If the consumer has no recorded requests the current time is returned
        (no wait needed).

        Parameters
        ----------
        consumer_id:
            Consumer to query.
        window_sec:
            Window duration.  Defaults to the instance-level ``window_sec``.

        Returns
        -------
        int
            UTC epoch seconds of the reset instant.
        """
        effective_window = window_sec if window_sec is not None else self._default_window_sec
        lock = await self._get_lock(consumer_id)
        async with lock:
            window = self._windows[consumer_id]
            now_monotonic = time.monotonic()
            cutoff = now_monotonic - effective_window
            self._prune(window, cutoff)

            if not window:
                return int(time.time())

            # Oldest in-window entry expires at: (oldest_monotonic + window_sec)
            # Convert to UTC wall clock: wall_now + (oldest_monotonic + window_sec - now_monotonic)
            seconds_until_oldest_expires = (window[0] + effective_window) - now_monotonic
            return int(time.time() + max(0.0, seconds_until_oldest_expires))

    async def reset(self, consumer_id: str) -> None:
        """
        Clear all recorded requests for *consumer_id*.

        Primarily intended for testing.

        Parameters
        ----------
        consumer_id:
            Consumer whose window should be reset.
        """
        lock = await self._get_lock(consumer_id)
        async with lock:
            self._windows[consumer_id].clear()


# ---------------------------------------------------------------------------
# Starlette middleware
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that enforces per-consumer sliding-window rate limits.

    Rate limit identity is derived from the ``X-Forwarded-For`` header
    (first IP in the chain) or, if that is absent, from the ASGI client
    host.  This matches typical reverse-proxy deployments where the
    real client IP is forwarded by the load balancer.

    When the limit is exceeded the middleware short-circuits the request
    pipeline and returns a ``429 Too Many Requests`` JSON response without
    invoking the downstream application.

    Response headers added on every non-exempt request::

        X-RateLimit-Limit:     <configured limit>
        X-RateLimit-Remaining: <remaining capacity after this request>
        X-RateLimit-Reset:     <UTC epoch second of window reset>

    Parameters
    ----------
    app:
        The downstream ASGI application.
    limit:
        Maximum requests per window (default: 100).
    window_sec:
        Window duration in seconds (default: 60).
    limiter:
        An existing :class:`SlidingWindowRateLimiter` instance to use.
        If ``None`` a new one is created with *limit* and *window_sec*.
        Supplying an external instance lets you share state across
        multiple middleware layers or inspect it in tests.
    exempt_paths:
        Paths that bypass rate limiting.  Defaults to
        ``{"/v1/health/live", "/metrics"}``.
    """

    def __init__(
        self,
        app: ASGIApp,
        limit: int = DEFAULT_LIMIT,
        window_sec: int = DEFAULT_WINDOW_SEC,
        limiter: SlidingWindowRateLimiter | None = None,
        exempt_paths: frozenset[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._limit = limit
        self._window_sec = window_sec
        self._limiter: SlidingWindowRateLimiter = limiter or SlidingWindowRateLimiter(
            limit=limit, window_sec=window_sec
        )
        self._exempt_paths: frozenset[str] = (
            exempt_paths if exempt_paths is not None else _EXEMPT_PATHS
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _consumer_id(request: Request) -> str:
        """
        Derive the consumer identity from request metadata.

        Priority:
        1. ``X-Forwarded-For`` header — first IP in the comma-separated list
           (the original client in proxy-chain notation).
        2. ``X-Real-IP`` header (Nginx convention).
        3. ASGI ``client`` tuple (``host``, ``port``).
        4. Fallback sentinel ``"unknown"``.
        """
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # "client_ip, proxy1_ip, proxy2_ip" — take the leftmost
            return forwarded_for.split(",")[0].strip()

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()

        if request.client and request.client.host:
            return request.client.host

        return "unknown"

    def _error_body(self, retry_after_ms: int) -> bytes:
        """Serialize the canonical 429 error envelope to JSON bytes."""
        payload = {
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": (
                    f"Rate limit of {self._limit} requests per "
                    f"{self._window_sec}s exceeded. "
                    f"Retry after {retry_after_ms} ms."
                ),
                "retryAfterMs": retry_after_ms,
                "provider": None,
                "requestId": None,
            }
        }
        return json.dumps(payload).encode()

    # ------------------------------------------------------------------
    # BaseHTTPMiddleware dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        """
        Intercept every HTTP request, check the rate limit, and either
        forward the request downstream or return a 429 response.
        """
        # ── Exempt paths bypass the limiter ──────────────────────────────
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        consumer_id = self._consumer_id(request)

        allowed = await self._limiter.is_allowed(
            consumer_id, limit=self._limit, window_sec=self._window_sec
        )

        # Always read usage *after* is_allowed so the count reflects the
        # just-recorded request (or the unchanged count for a blocked one).
        usage = await self._limiter.get_usage(
            consumer_id, window_sec=self._window_sec
        )
        reset_epoch = await self._limiter.get_reset_epoch(
            consumer_id, window_sec=self._window_sec
        )
        remaining = max(0, self._limit - usage)

        # ── Rate limit exceeded ───────────────────────────────────────────
        if not allowed:
            # Compute how many milliseconds until the window resets.
            now_epoch = int(time.time())
            retry_after_sec = max(0, reset_epoch - now_epoch)
            retry_after_ms = retry_after_sec * 1000

            return Response(
                content=self._error_body(retry_after_ms),
                status_code=429,
                media_type="application/json",
                headers={
                    "X-RateLimit-Limit": str(self._limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_epoch),
                    "Retry-After": str(retry_after_sec),
                },
            )

        # ── Request is allowed — forward downstream ───────────────────────
        response = await call_next(request)

        # Inject rate-limit headers into the real response.
        response.headers["X-RateLimit-Limit"] = str(self._limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_epoch)

        return response
