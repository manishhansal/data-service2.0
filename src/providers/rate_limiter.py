"""
src/providers/rate_limiter.py

Token-bucket rate limiter for DATA-SERVICE 2.0.

Each provider × capability pair has an independent token bucket. Tokens are
stored in Redis so that all replicas share the same budget (cross-replica
consistency via atomic Lua scripts). When Redis is unavailable the limiter
falls back to a local in-process token bucket per (provider, capability) pair.

Design (from §Provider Gateway / Token-Bucket Rate Limiter):
  capacity     = requestsPerSecond × burst_multiplier   (default burst_multiplier=2.0)
  refill_rate  = requestsPerSecond tokens/second
  Redis key    = mds:rl:{provider}:{capability}
  Key value    = JSON with {tokens: float, last_refill_ts: float}

Queue depth:
  acquire() blocks waiting callers up to a configurable maximum (default 100
  from settings).  When the queue is full the call is rejected immediately with
  ProviderQueueFullError (error_code="PROVIDER_QUEUE_FULL").

Requirements: 5.2, 5.3
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from src.core.schemas.provider import ProviderId

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider-level rate limits (req/s)
# Sourced from the Capability Matrix (§Provider Gateway).
# ---------------------------------------------------------------------------

#: Default rate limit (req/s) used when a provider is not listed below.
_DEFAULT_RPS: float = 1.0

#: Per-provider rate limits in requests per second.
#: Used by TokenBucketRateLimiter when a capability-specific override is not
#: provided.  Callers may also pass requestsPerSecond directly via acquire().
#:
#: Upstox rate limits (per NSE circular, May 2025):
#:   Standard APIs: 50 req/s, 500/min, 2000/30min.
#:   Order APIs: 10 req/s (not used here for market data).
PROVIDER_RATE_LIMITS: dict[str, float] = {
    ProviderId.ANGEL_ONE.value:     3.0,
    ProviderId.UPSTOX.value:        50.0,   # FIXED: was 10.0, actual is 50 req/s
    ProviderId.SCRAPLING_NSE.value: 2.0,
    ProviderId.JUGAAD_DATA.value:   1.0,
    ProviderId.OPENCHART.value:     5.0,
    ProviderId.YAHOO_FINANCE.value: 1.0,
    ProviderId.BINANCE.value:       20.0,
    ProviderId.DERIBIT.value:       5.0,
    ProviderId.DELTA.value:         10.0,  # DS2-RCA-001: Delta Exchange India REST
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProviderQueueFullError(RuntimeError):
    """Raised when the per-provider request queue is at capacity.

    This maps to the ``PROVIDER_QUEUE_FULL`` error code in the canonical
    error envelope (Requirement 5.3).

    Attributes:
        error_code:  Always ``"PROVIDER_QUEUE_FULL"``.
        provider_id: The provider whose queue overflowed.
        capability:  The capability that was being acquired.
        queue_depth: The configured maximum queue depth.
    """

    error_code: str = "PROVIDER_QUEUE_FULL"

    def __init__(
        self,
        provider_id: str,
        capability: str,
        queue_depth: int,
    ) -> None:
        self.provider_id = provider_id
        self.capability = capability
        self.queue_depth = queue_depth
        super().__init__(
            f"Provider queue full for provider={provider_id!r} "
            f"capability={capability!r} (max_depth={queue_depth}). "
            f"error_code=PROVIDER_QUEUE_FULL"
        )


# ---------------------------------------------------------------------------
# In-process (local) token bucket — used as a Redis fallback
# ---------------------------------------------------------------------------


@dataclass
class _LocalBucket:
    """A single in-process token bucket for one (provider, capability) pair.

    Thread-safety: This is designed for use inside asyncio.  All mutations
    happen in a coroutine that holds the event loop lock (no actual locking
    needed for asyncio single-threaded execution).  For thread-safety in
    multi-threaded environments an asyncio.Lock would be added.
    """

    capacity: float
    tokens: float
    refill_rate: float          # tokens per second
    last_refill_ts: float = field(default_factory=time.monotonic)

    def _refill(self) -> None:
        """Add tokens based on elapsed time since the last refill."""
        now = time.monotonic()
        elapsed = now - self.last_refill_ts
        self.last_refill_ts = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)

    def try_consume(self) -> bool:
        """Attempt to consume one token atomically.

        Returns:
            ``True`` if a token was available and consumed, ``False`` otherwise.
        """
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


# ---------------------------------------------------------------------------
# Lua script for atomic Redis token-bucket operation
# ---------------------------------------------------------------------------

# The script atomically:
#   1. Loads current state (tokens, last_refill_ts) from Redis (or uses
#      defaults if the key does not exist yet).
#   2. Refills tokens based on elapsed wall-clock time.
#   3. If tokens >= 1: decrement by 1, store new state, return 1 (acquired).
#      Otherwise: store refreshed state (no decrement), return 0 (rejected).
#
# KEYS[1]  = mds:rl:{provider}:{capability}
# ARGV[1]  = capacity (string float)
# ARGV[2]  = refill_rate tokens/second (string float)
# ARGV[3]  = current wall-clock epoch (string float)
# ARGV[4]  = TTL for the key in seconds (int)
#
# Returns: "1" (acquired) or "0" (no tokens available).

_LUA_TOKEN_BUCKET = """
local key         = KEYS[1]
local capacity    = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now         = tonumber(ARGV[3])
local ttl_sec     = tonumber(ARGV[4])

-- Load existing state or initialise to full bucket
local raw = redis.call('GET', key)
local tokens, last_ts

if raw then
    local state = cjson.decode(raw)
    tokens  = tonumber(state['tokens'])
    last_ts = tonumber(state['last_refill_ts'])
else
    -- First call: start with a full bucket
    tokens  = capacity
    last_ts = now
end

-- Refill based on elapsed time
local elapsed = now - last_ts
tokens = math.min(capacity, tokens + elapsed * refill_rate)
last_ts = now

-- Try to consume one token
local acquired = 0
if tokens >= 1.0 then
    tokens    = tokens - 1.0
    acquired  = 1
end

-- Persist new state with TTL (avoids stale keys accumulating)
local new_state = cjson.encode({tokens=tokens, last_refill_ts=last_ts})
redis.call('SET', key, new_state, 'EX', ttl_sec)

return acquired
"""


# ---------------------------------------------------------------------------
# TokenBucketRateLimiter
# ---------------------------------------------------------------------------


class TokenBucketRateLimiter:
    """Per-provider token-bucket rate limiter backed by Redis.

    Each (provider_id, capability) pair has its own bucket.  Tokens are
    stored in Redis so that multiple service replicas share the same rate
    budget.  When Redis is unreachable the limiter falls back to a local
    in-process bucket for the duration of the outage.

    Args:
        redis_client:    An ``redis.asyncio.Redis`` instance (decoded responses
                         expected).  May be ``None``; in that case the limiter
                         starts immediately in local-fallback mode.
        queue_max_depth: Maximum number of coroutines that may wait for a
                         token for the same (provider, capability) pair before
                         new callers are rejected with ProviderQueueFullError.
                         Valid range 1–10 000 (default 100).
        burst_multiplier: Bucket capacity multiplier.  ``capacity`` =
                         ``requestsPerSecond × burst_multiplier``.
                         Default: 2.0.

    Redis key pattern::

        mds:rl:{provider_id}:{capability}

    Usage::

        limiter = TokenBucketRateLimiter(redis_client=redis)
        async with limiter.acquire("angel_one", "HISTORICAL_OHLCV"):
            await provider.fetch(...)   # one token consumed

    Or without the context manager::

        await limiter.acquire("angel_one", "HISTORICAL_OHLCV")
        try:
            await provider.fetch(...)
        finally:
            # release() is a no-op for token buckets but available for API compat
            await limiter.release("angel_one", "HISTORICAL_OHLCV")
    """

    #: Redis key TTL (seconds).  Keys auto-expire after no activity.
    _KEY_TTL_SEC: int = 3600

    #: How long (seconds) to wait between retry attempts inside acquire().
    _POLL_INTERVAL_SEC: float = 0.05

    def __init__(
        self,
        redis_client=None,  # redis.asyncio.Redis[str] | None
        *,
        queue_max_depth: int = 100,
        burst_multiplier: float = 2.0,
    ) -> None:
        if not (1 <= queue_max_depth <= 10_000):
            raise ValueError(
                f"queue_max_depth must be between 1 and 10 000; got {queue_max_depth!r}"
            )
        if burst_multiplier <= 0:
            raise ValueError(
                f"burst_multiplier must be positive; got {burst_multiplier!r}"
            )
        self._redis = redis_client
        self._queue_max_depth = queue_max_depth
        self._burst_multiplier = burst_multiplier

        # Tracks waiting coroutine count per (provider_id, capability) pair.
        self._waiting: dict[tuple[str, str], int] = {}

        # Local in-process buckets — used as Redis fallback.
        self._local_buckets: dict[tuple[str, str], _LocalBucket] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _redis_key(provider_id: str, capability: str) -> str:
        """Build the Redis key for a (provider_id, capability) pair."""
        return f"mds:rl:{provider_id}:{capability}"

    def _get_rps(self, provider_id: str) -> float:
        """Look up the configured requests-per-second for a provider."""
        return PROVIDER_RATE_LIMITS.get(provider_id, _DEFAULT_RPS)

    def _get_or_create_local_bucket(
        self, provider_id: str, capability: str, rps: float
    ) -> _LocalBucket:
        """Return (or lazily create) the local fallback bucket."""
        key = (provider_id, capability)
        if key not in self._local_buckets:
            capacity = rps * self._burst_multiplier
            self._local_buckets[key] = _LocalBucket(
                capacity=capacity,
                tokens=capacity,       # start with a full bucket
                refill_rate=rps,
            )
        return self._local_buckets[key]

    async def _redis_try_acquire(
        self, provider_id: str, capability: str, rps: float
    ) -> bool:
        """Execute the Lua script against Redis.

        Returns:
            ``True`` if a token was acquired, ``False`` if not available.

        Raises:
            Exception: On Redis errors (caller falls back to local bucket).
        """
        key = self._redis_key(provider_id, capability)
        capacity = rps * self._burst_multiplier
        now = time.time()
        result = await self._redis.eval(  # type: ignore[union-attr]
            _LUA_TOKEN_BUCKET,
            1,
            key,
            str(capacity),
            str(rps),
            str(now),
            str(self._KEY_TTL_SEC),
        )
        return bool(int(result))

    def _increment_waiting(self, key: tuple[str, str]) -> None:
        self._waiting[key] = self._waiting.get(key, 0) + 1

    def _decrement_waiting(self, key: tuple[str, str]) -> None:
        if key in self._waiting:
            self._waiting[key] = max(0, self._waiting[key] - 1)

    def _queue_depth(self, provider_id: str, capability: str) -> int:
        return self._waiting.get((provider_id, capability), 0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def try_acquire(self, provider_id: str, capability: str) -> bool:
        """Non-blocking attempt to consume one token.

        Tries Redis first; if Redis is unavailable, tries the local bucket.

        Args:
            provider_id: Provider identifier string (e.g. ``"angel_one"``).
            capability:  Data capability string (e.g. ``"HISTORICAL_OHLCV"``).

        Returns:
            ``True`` if a token was consumed, ``False`` if no tokens available.
        """
        rps = self._get_rps(provider_id)

        if self._redis is not None:
            try:
                return await self._redis_try_acquire(provider_id, capability, rps)
            except Exception as exc:
                logger.warning(
                    "rate_limiter_redis_unavailable_fallback",
                    extra={
                        "provider": provider_id,
                        "capability": capability,
                        "error": str(exc),
                    },
                )

        # Local fallback
        bucket = self._get_or_create_local_bucket(provider_id, capability, rps)
        return bucket.try_consume()

    async def acquire(
        self,
        provider_id: str,
        capability: str,
        timeout_sec: float = 30.0,
    ) -> None:
        """Block until a token is available, the timeout elapses, or the
        queue is full.

        This method polls for a token at ``_POLL_INTERVAL_SEC`` intervals.
        Callers waiting while tokens are exhausted occupy one slot in the
        per-(provider, capability) queue counter.  When the counter reaches
        ``queue_max_depth``, new callers are rejected immediately with
        ``ProviderQueueFullError``.

        Args:
            provider_id: Provider identifier string.
            capability:  Data capability string.
            timeout_sec: Maximum seconds to wait for a token.  After the
                         timeout the method raises ``asyncio.TimeoutError``.

        Raises:
            ProviderQueueFullError: If the queue is already at capacity.
            asyncio.TimeoutError:   If ``timeout_sec`` elapses before a token
                                    becomes available.
        """
        key = (provider_id, capability)

        # Check queue capacity *before* queuing
        if self._queue_depth(provider_id, capability) >= self._queue_max_depth:
            raise ProviderQueueFullError(provider_id, capability, self._queue_max_depth)

        self._increment_waiting(key)
        deadline = time.monotonic() + timeout_sec
        try:
            while True:
                acquired = await self.try_acquire(provider_id, capability)
                if acquired:
                    return

                if time.monotonic() >= deadline:
                    raise asyncio.TimeoutError(
                        f"Rate-limiter timeout after {timeout_sec}s for "
                        f"provider={provider_id!r} capability={capability!r}"
                    )

                await asyncio.sleep(self._POLL_INTERVAL_SEC)
        finally:
            self._decrement_waiting(key)

    async def release(self, provider_id: str, capability: str) -> None:
        """No-op for token-bucket semantics.

        Token buckets refill automatically over time; there is nothing to
        release.  This method exists only for API compatibility with
        semaphore-style limiters.

        Args:
            provider_id: Provider identifier string.
            capability:  Data capability string.
        """
        # Intentionally a no-op.

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def waiting_count(self, provider_id: str, capability: str) -> int:
        """Return the number of coroutines currently waiting for a token.

        Args:
            provider_id: Provider identifier string.
            capability:  Data capability string.

        Returns:
            Non-negative integer count.
        """
        return self._queue_depth(provider_id, capability)

    async def current_tokens(
        self, provider_id: str, capability: str
    ) -> Optional[float]:
        """Return the approximate current token count for the bucket.

        Reads from Redis if available, otherwise reads from the local bucket.
        Returns ``None`` when neither source has been initialised yet.

        Args:
            provider_id: Provider identifier string.
            capability:  Data capability string.

        Returns:
            Current token count (float) or ``None``.
        """
        if self._redis is not None:
            try:
                key = self._redis_key(provider_id, capability)
                raw = await self._redis.get(key)
                if raw:
                    state = json.loads(raw)
                    return float(state["tokens"])
            except Exception:  # noqa: BLE001
                pass  # fall through to local bucket

        local = self._local_buckets.get((provider_id, capability))
        if local is not None:
            local._refill()
            return local.tokens
        return None


# ---------------------------------------------------------------------------
# HierarchicalRateLimiter
# ---------------------------------------------------------------------------
# Upstox enforces three independent rolling-window quotas:
#   Bucket 1 — 50  requests / second
#   Bucket 2 — 500 requests / minute
#   Bucket 3 — 2000 requests / 30 minutes
#
# A request must pass ALL three windows before it is allowed.  The per-second
# TokenBucketRateLimiter alone does NOT prevent burst violations at the
# minute or 30-minute level.
#
# This class wraps the existing TokenBucketRateLimiter (per-second bucket)
# and adds two sliding-window counters for the minute and 30-minute quotas.
# All three are Redis-backed and cross-replica safe.
#
# Provider support:
#   - "upstox":    all three windows enforced
#   - all others:  only the per-second bucket is enforced (existing behaviour)
#
# Requirements: Phase J / §Provider Gateway Hierarchical Rate Limits

_UPSTOX_WINDOW_QUOTAS: dict[str, tuple[int, int]] = {
    # "window_key_suffix": (window_seconds, max_requests)
    "per_min":   (60,   500),
    "per_30min": (1800, 2000),
}


class HierarchicalRateLimiter:
    """Enforces per-second, per-minute, and per-30-minute rate limits.

    Wraps ``TokenBucketRateLimiter`` for the per-second bucket and adds
    Redis sorted-set sliding windows for the per-minute and per-30-minute
    quotas.  Only Upstox currently has multi-window quotas; all other
    providers delegate to the per-second bucket only.

    Args:
        redis_client:    Async Redis client.  When ``None`` falls back to
                         local-only mode (no cross-replica safety).
        queue_max_depth: Forwarded to the inner TokenBucketRateLimiter.
        burst_multiplier: Forwarded to the inner TokenBucketRateLimiter.

    Usage::

        limiter = HierarchicalRateLimiter(redis_client=redis)
        allowed = await limiter.try_acquire("upstox", "HISTORICAL_OHLCV")
        if not allowed:
            raise ProviderRateLimitedError(...)
    """

    _SLIDING_KEY_TEMPLATE = "mds:rl_sw:{provider}:{window}"

    def __init__(
        self,
        redis_client=None,
        *,
        queue_max_depth: int = 100,
        burst_multiplier: float = 2.0,
    ) -> None:
        self._redis = redis_client
        self._inner = TokenBucketRateLimiter(
            redis_client=redis_client,
            queue_max_depth=queue_max_depth,
            burst_multiplier=burst_multiplier,
        )

    async def try_acquire(self, provider_id: str, capability: str) -> bool:
        """Try to acquire capacity across all applicable rate-limit windows.

        Returns ``True`` immediately if all windows allow the request.
        Returns ``False`` if any window is exhausted (caller should back off).

        The per-second token-bucket is checked last (it blocks briefly on
        overflow) so the faster sliding-window rejections happen first.
        """
        # Sliding-window checks apply only to Upstox
        if provider_id == ProviderId.UPSTOX.value and self._redis is not None:
            now_ts = time.time()
            for window_suffix, (window_sec, max_req) in _UPSTOX_WINDOW_QUOTAS.items():
                key = self._SLIDING_KEY_TEMPLATE.format(
                    provider=provider_id, window=window_suffix
                )
                try:
                    # Remove entries older than the window
                    cutoff = now_ts - window_sec
                    pipe = self._redis.pipeline()
                    pipe.zremrangebyscore(key, "-inf", cutoff)
                    pipe.zcard(key)
                    _, current_count = await pipe.execute()
                    if current_count >= max_req:
                        logger.warning(
                            "upstox_sliding_window_exhausted",
                            window=window_suffix,
                            current=current_count,
                            limit=max_req,
                            component="hierarchical_rate_limiter",
                        )
                        return False
                    # Record this request in the window
                    entry_id = f"{now_ts:.6f}"
                    await self._redis.zadd(key, {entry_id: now_ts})
                    await self._redis.expire(key, window_sec + 60)  # safety TTL
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "sliding_window_redis_error",
                        window=window_suffix,
                        error=str(exc),
                        component="hierarchical_rate_limiter",
                    )
                    # Redis failure → degrade gracefully; per-second bucket still enforced

        # Per-second token-bucket (for all providers)
        return await self._inner.try_acquire(provider_id, capability)

    async def acquire(
        self,
        provider_id: str,
        capability: str,
        *,
        timeout_sec: float = 30.0,
    ) -> None:
        """Block until capacity is available across all windows.

        Raises ``ProviderQueueFullError`` if the per-second queue fills up.
        Uses polling with jitter for the sliding-window checks.
        """
        import random  # noqa: PLC0415
        deadline = time.monotonic() + timeout_sec
        while True:
            ok = await self.try_acquire(provider_id, capability)
            if ok:
                return
            if time.monotonic() >= deadline:
                raise ProviderQueueFullError(
                    provider_id=provider_id,
                    capability=capability,
                    queue_depth=self._inner._queue_max_depth,
                )
            jitter = random.uniform(0.0, 0.02)
            await asyncio.sleep(0.05 + jitter)

    def waiting_count(self, provider_id: str, capability: str) -> int:
        """Forward to inner bucket's waiting count."""
        return self._inner.waiting_count(provider_id, capability)

    async def current_tokens(
        self, provider_id: str, capability: str
    ) -> Optional[float]:
        """Forward to inner bucket's token count."""
        return await self._inner.current_tokens(provider_id, capability)

    async def get_window_usage(self, provider_id: str) -> dict[str, dict]:
        """Return current usage for all sliding windows (Upstox only).

        Returns a dict mapping window name → {current, limit, window_sec}.
        Returns empty dict for providers with no sliding-window quotas.
        """
        if provider_id != ProviderId.UPSTOX.value or self._redis is None:
            return {}
        now_ts = time.time()
        result: dict[str, dict] = {}
        for window_suffix, (window_sec, max_req) in _UPSTOX_WINDOW_QUOTAS.items():
            key = self._SLIDING_KEY_TEMPLATE.format(
                provider=provider_id, window=window_suffix
            )
            try:
                cutoff = now_ts - window_sec
                await self._redis.zremrangebyscore(key, "-inf", cutoff)
                current = await self._redis.zcard(key)
                result[window_suffix] = {
                    "current": int(current),
                    "limit": max_req,
                    "window_sec": window_sec,
                    "remaining": max(0, max_req - int(current)),
                }
            except Exception:  # noqa: BLE001
                result[window_suffix] = {"error": "redis_unavailable"}
        return result
