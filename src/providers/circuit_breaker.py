"""
src/providers/circuit_breaker.py

Circuit breaker state machine with Redis-backed state for DATA-SERVICE 2.0.

Implements per-provider × per-capability circuit breaking with the classic
three-state machine:

    CLOSED  ──(failure_count >= threshold)──►  OPEN
    OPEN    ──(recovery_window elapsed)────►  HALF_OPEN
    HALF_OPEN ──(probe succeeds)───────────►  CLOSED
    HALF_OPEN ──(probe fails)──────────────►  OPEN

State is stored in Redis so all service replicas share a consistent view
(Requirement 5.4).  If Redis is unavailable, the circuit falls back to
in-memory state — providers remain accessible rather than silently broken.

Key behaviours mandated by the design:
  - HTTP 429 (rate limit) responses do NOT increment the failure counter
    (Requirement 5.5).
  - ``MARKET_CLOSED`` semantics do NOT increment the failure counter
    (Requirement 5.7).
  - ``UNSUPPORTED_CAPABILITY`` semantics do NOT increment the failure counter
    (Requirement 5.8).
  - Exactly ONE probe request is allowed in HALF_OPEN; all others are
    rejected with ``CircuitOpenError`` until the probe resolves (Requirement 5.4).
  - When the circuit transitions to OPEN, a ``circuit_open`` structured-log
    event is emitted at WARN level within 1 second of the transition
    (Requirement 18.5).

Redis key pattern:
    ``mds:cb:{provider}:{capability}``

Stored JSON payload:
    {
        "state": "CLOSED" | "OPEN" | "HALF_OPEN",
        "opened_at_ms": <int ms timestamp | null>,
        "failure_count": <int>,
        "last_failure_reason": <str | null>,
        "probe_in_flight": <bool>   -- HALF_OPEN only
    }

Requirements: 5.4, 5.5, 5.7, 5.8, 18.5
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional, TypeVar

import structlog

from src.cache.redis_client import RedisClient, RedisUnavailableError
from src.core.schemas.provider import CircuitState

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------

T = TypeVar("T")
ProviderFn = Callable[..., Awaitable[T]]

# ---------------------------------------------------------------------------
# Redis key helper
# ---------------------------------------------------------------------------

_CB_KEY_PREFIX = "mds:cb"


def _cb_key(provider: str, capability: str) -> str:
    """Build the Redis key for a given provider × capability pair.

    Pattern: ``mds:cb:{provider}:{capability}``
    """
    return f"{_CB_KEY_PREFIX}:{provider}:{capability}"


# ---------------------------------------------------------------------------
# Default state (used when Redis is unavailable or key is absent)
# ---------------------------------------------------------------------------

def _default_state_dict() -> dict[str, Any]:
    return {
        "state": CircuitState.CLOSED.value,
        "opened_at_ms": None,
        "failure_count": 0,
        "last_failure_reason": None,
        "probe_in_flight": False,
    }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CircuitOpenError(Exception):
    """Raised when a provider call is rejected because the circuit is OPEN.

    Attributes:
        provider:   Provider identifier string.
        capability: Capability string (e.g. ``"HISTORICAL_OHLCV"``).
        state:      Current circuit state at the time of rejection.
    """

    def __init__(
        self,
        provider: str,
        capability: str,
        state: CircuitState = CircuitState.OPEN,
        message: str = "",
    ) -> None:
        self.provider = provider
        self.capability = capability
        self.state = state
        msg = message or (
            f"Circuit breaker is {state.value} for "
            f"{provider}:{capability} — request rejected"
        )
        super().__init__(msg)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Redis-backed per-provider × per-capability circuit breaker.

    Args:
        provider:              Provider identifier (e.g. ``"angel_one"``).
        capability:            Capability string (e.g. ``"HISTORICAL_OHLCV"``).
        redis_client:          Async ``RedisClient`` wrapper; may be ``None``
                               in which case the breaker runs fully in-memory.
        failure_threshold:     Number of failures required to open the circuit
                               (valid range 1–100; default 5).
        recovery_window_sec:   Seconds to wait in OPEN state before allowing a
                               probe attempt (valid range 1–3600; default 60).
    """

    def __init__(
        self,
        provider: str,
        capability: str,
        redis_client: Optional[RedisClient] = None,
        failure_threshold: int = 5,
        recovery_window_sec: int = 60,
    ) -> None:
        if not 1 <= failure_threshold <= 100:
            raise ValueError(
                f"failure_threshold must be between 1 and 100; got {failure_threshold}"
            )
        if not 1 <= recovery_window_sec <= 3600:
            raise ValueError(
                f"recovery_window_sec must be between 1 and 3600; got {recovery_window_sec}"
            )

        self.provider = provider
        self.capability = capability
        self._redis: Optional[RedisClient] = redis_client
        self._failure_threshold = failure_threshold
        self._recovery_window_sec = recovery_window_sec
        self._key = _cb_key(provider, capability)

        # In-memory fallback state — used when Redis is unavailable.
        # Each field mirrors the Redis JSON payload.
        self._mem: dict[str, Any] = _default_state_dict()

        # Asyncio lock: prevents concurrent state-mutation races on this instance
        # when Redis is down (in-memory path).
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers — Redis I/O
    # ------------------------------------------------------------------

    async def _read_state(self) -> dict[str, Any]:
        """Read circuit state from Redis; fall back to in-memory on error."""
        if self._redis is None:
            return dict(self._mem)
        try:
            raw = await self._redis.get(self._key)
            if raw is None:
                return _default_state_dict()
            return json.loads(raw)
        except (RedisUnavailableError, json.JSONDecodeError):
            return dict(self._mem)

    async def _write_state(self, state_dict: dict[str, Any]) -> None:
        """Persist circuit state to Redis; update in-memory fallback regardless."""
        self._mem.update(state_dict)
        if self._redis is None:
            return
        try:
            await self._redis.set_with_ttl(
                self._key,
                json.dumps(state_dict),
                # Keep circuit state alive for at least 24 h so it survives
                # Redis restarts during low-traffic periods.  The state machine
                # manages transitions; TTL is purely anti-leak insurance.
                ttl_seconds=86_400,
            )
        except RedisUnavailableError:
            # Already updated in-memory above; log and continue gracefully.
            logger.warning(
                "circuit_breaker_redis_write_failed",
                provider=self.provider,
                capability=self.capability,
            )

    # ------------------------------------------------------------------
    # Internal helpers — OPEN transition logging (Requirement 18.5)
    # ------------------------------------------------------------------

    def _emit_circuit_open_log(
        self,
        failure_count: int,
        reason: Optional[str],
    ) -> None:
        """Emit the mandatory ``circuit_open`` WARN-level structured log event.

        Per Requirement 18.5 this must fire within 1 second of the OPEN
        transition — calling it synchronously from ``record_failure`` satisfies
        that constraint without spawning a background task.

        ``failureRate`` is expressed as ``failure_count / failure_threshold``
        capped at 1.0 so the field is always in [0.0, 1.0].
        """
        failure_rate = min(
            1.0,
            failure_count / max(1, self._failure_threshold),
        )
        logger.warning(
            "circuit_open",
            provider=self.provider,
            capability=self.capability,
            failureRate=round(failure_rate, 4),
            failureCount=failure_count,
            failureThreshold=self._failure_threshold,
            lastFailureReason=reason,
            transitionTimestamp=_utc_iso_now(),
        )

    # ------------------------------------------------------------------
    # Public API — state query
    # ------------------------------------------------------------------

    async def get_state(self) -> CircuitState:
        """Return the current circuit state, advancing OPEN→HALF_OPEN if the
        recovery window has elapsed.

        Returns:
            Current ``CircuitState`` value (may differ from what is stored in
            Redis if the recovery window just elapsed — the state is updated
            before returning).
        """
        state_dict = await self._read_state()
        state = CircuitState(state_dict.get("state", CircuitState.CLOSED.value))

        if state == CircuitState.OPEN:
            opened_at_ms = state_dict.get("opened_at_ms")
            if opened_at_ms is not None:
                elapsed_sec = (_now_ms() - opened_at_ms) / 1_000
                if elapsed_sec >= self._recovery_window_sec:
                    # Advance to HALF_OPEN.
                    state_dict["state"] = CircuitState.HALF_OPEN.value
                    state_dict["probe_in_flight"] = False
                    await self._write_state(state_dict)
                    return CircuitState.HALF_OPEN

        return state

    # ------------------------------------------------------------------
    # Public API — call wrapper
    # ------------------------------------------------------------------

    async def call(self, provider_fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Execute ``provider_fn`` if the circuit permits, else raise ``CircuitOpenError``.

        Behaviour per state:
        - **CLOSED** — call passes through unconditionally.
        - **OPEN** — raises ``CircuitOpenError`` immediately; no I/O.
        - **HALF_OPEN** — if no probe is currently in-flight, marks
          ``probe_in_flight=True`` and dispatches this call as the probe.
          All other concurrent calls raise ``CircuitOpenError``.

        Args:
            provider_fn: Async callable representing the provider API call.
            *args:       Positional arguments forwarded to ``provider_fn``.
            **kwargs:    Keyword arguments forwarded to ``provider_fn``.

        Returns:
            The return value of ``provider_fn``.

        Raises:
            CircuitOpenError: When the circuit is OPEN or when HALF_OPEN and
                              a probe is already in-flight.
        """
        async with self._lock:
            state_dict = await self._read_state()
            state = CircuitState(state_dict.get("state", CircuitState.CLOSED.value))

            # Advance OPEN → HALF_OPEN if recovery window elapsed.
            if state == CircuitState.OPEN:
                opened_at_ms = state_dict.get("opened_at_ms")
                if opened_at_ms is not None:
                    elapsed_sec = (_now_ms() - opened_at_ms) / 1_000
                    if elapsed_sec >= self._recovery_window_sec:
                        state = CircuitState.HALF_OPEN
                        state_dict["state"] = CircuitState.HALF_OPEN.value
                        state_dict["probe_in_flight"] = False
                        await self._write_state(state_dict)

            if state == CircuitState.OPEN:
                raise CircuitOpenError(
                    self.provider, self.capability, state=CircuitState.OPEN
                )

            if state == CircuitState.HALF_OPEN:
                if state_dict.get("probe_in_flight", False):
                    # Another probe is already in-flight; reject this call.
                    raise CircuitOpenError(
                        self.provider,
                        self.capability,
                        state=CircuitState.HALF_OPEN,
                        message=(
                            f"Circuit is HALF_OPEN for {self.provider}:{self.capability}"
                            " — probe already in-flight; request rejected"
                        ),
                    )
                # Claim the probe slot.
                state_dict["probe_in_flight"] = True
                await self._write_state(state_dict)

        # --- Execute the provider call (outside the lock) ---
        return await provider_fn(*args, **kwargs)

    # ------------------------------------------------------------------
    # Public API — outcome recording
    # ------------------------------------------------------------------

    async def record_success(self) -> None:
        """Record a successful provider call.

        Behaviour:
        - **CLOSED** — reset ``failure_count`` to 0.
        - **HALF_OPEN** — transition to CLOSED; reset all counters.
        - **OPEN** — no-op (success recorded while technically OPEN is unusual;
          state will naturally advance on the next ``get_state()`` call).
        """
        async with self._lock:
            state_dict = await self._read_state()
            state = CircuitState(state_dict.get("state", CircuitState.CLOSED.value))

            if state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                state_dict["state"] = CircuitState.CLOSED.value
                state_dict["failure_count"] = 0
                state_dict["opened_at_ms"] = None
                state_dict["last_failure_reason"] = None
                state_dict["probe_in_flight"] = False
                await self._write_state(state_dict)
                if state == CircuitState.HALF_OPEN:
                    logger.info(
                        "circuit_closed",
                        provider=self.provider,
                        capability=self.capability,
                        reason="probe_succeeded",
                    )

    async def record_failure(self, reason: Optional[str] = None) -> None:
        """Record a provider failure and open the circuit if the threshold is met.

        Behaviour:
        - Increments ``failure_count`` by 1 in CLOSED state.
        - If ``failure_count >= failure_threshold``, transitions to OPEN and
          emits the ``circuit_open`` WARN log (Requirement 18.5).
        - In HALF_OPEN, transitions immediately back to OPEN and emits the log.

        This method must NOT be called for:
        - HTTP 429 rate-limit responses (use ``record_rate_limit()`` instead)
        - ``MARKET_CLOSED`` semantics (use ``record_market_closed()`` instead)
        - ``UNSUPPORTED_CAPABILITY`` semantics (callers simply do not call this)

        Args:
            reason: Optional human-readable failure reason stored in state.
        """
        async with self._lock:
            state_dict = await self._read_state()
            state = CircuitState(state_dict.get("state", CircuitState.CLOSED.value))

            if state == CircuitState.OPEN:
                # Already open; update reason but don't touch counters or timestamp.
                if reason:
                    state_dict["last_failure_reason"] = reason
                    await self._write_state(state_dict)
                return

            # CLOSED or HALF_OPEN: increment counter.
            state_dict["failure_count"] = state_dict.get("failure_count", 0) + 1
            state_dict["last_failure_reason"] = reason

            if state == CircuitState.HALF_OPEN or state_dict["failure_count"] >= self._failure_threshold:
                # Transition to OPEN.
                state_dict["state"] = CircuitState.OPEN.value
                state_dict["opened_at_ms"] = _now_ms()
                state_dict["probe_in_flight"] = False
                await self._write_state(state_dict)
                self._emit_circuit_open_log(state_dict["failure_count"], reason)
            else:
                await self._write_state(state_dict)

    async def record_rate_limit(self, retry_after_sec: Optional[int] = None) -> None:
        """Record an HTTP 429 (rate limit) response from the provider.

        Per Requirements 5.5 and 5.6, rate-limit responses do NOT increment
        the failure counter and do NOT open the circuit.  The ``retry_after_sec``
        hint is logged for operational visibility but not acted on here (the
        rate limiter component handles backoff).

        Args:
            retry_after_sec: Value of the ``Retry-After`` header if present.
        """
        logger.info(
            "provider_rate_limited",
            provider=self.provider,
            capability=self.capability,
            retryAfterSec=retry_after_sec or 60,
        )
        # Intentionally does NOT modify circuit state.

    async def record_market_closed(self) -> None:
        """Record a ``MARKET_CLOSED`` response from the provider.

        Per Requirement 5.7, market-closed responses do NOT increment the
        failure counter and do NOT open the circuit.
        """
        logger.debug(
            "provider_market_closed",
            provider=self.provider,
            capability=self.capability,
        )
        # Intentionally does NOT modify circuit state.

    async def record_unsupported_capability(self) -> None:
        """Record an ``UNSUPPORTED_CAPABILITY`` response from the provider.

        Per Requirement 5.8, unsupported-capability responses do NOT increment
        the failure counter and do NOT open the circuit.
        """
        logger.debug(
            "provider_unsupported_capability",
            provider=self.provider,
            capability=self.capability,
        )
        # Intentionally does NOT modify circuit state.

    # ------------------------------------------------------------------
    # Public API — reset (admin / test utility)
    # ------------------------------------------------------------------

    async def reset(self) -> None:
        """Force the circuit back to CLOSED with all counters zeroed.

        Intended for administrative use and test teardown.
        """
        async with self._lock:
            state_dict = _default_state_dict()
            await self._write_state(state_dict)

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(provider={self.provider!r}, "
            f"capability={self.capability!r}, "
            f"threshold={self._failure_threshold}, "
            f"recovery_window={self._recovery_window_sec}s)"
        )


# ---------------------------------------------------------------------------
# Module-level utilities
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Return the current UTC time as an integer epoch millisecond timestamp."""
    return int(time.monotonic() * 1_000)  # monotonic for duration math


def _utc_iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string with Z suffix."""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
