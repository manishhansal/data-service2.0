"""
tests/integration/test_circuit_breaker.py
==========================================

Integration tests for the CircuitBreaker state machine.

Covers all state transitions and the special non-failure semantics mandated
by Requirements 5.4–5.8:

    CLOSED → OPEN          : threshold consecutive failures open the circuit
    OPEN (fail-fast)       : all calls are rejected without touching the provider
    OPEN → HALF_OPEN       : after recovery_window_sec elapses, one probe is allowed
    HALF_OPEN → CLOSED     : probe success resets the circuit
    HALF_OPEN → OPEN       : probe failure immediately re-opens the circuit
    HTTP 429 (rate limit)  : does NOT increment failure counter (Req 5.5 / 5.6)
    MARKET_CLOSED          : does NOT increment failure counter (Req 5.7)

These tests run purely in-memory: ``MockRedis`` is wrapped in a ``RedisClient``
so the circuit breaker's read/write paths exercise real JSON serialisation and
deserialisation — no live Redis process is required.

Markers
-------
Tests are NOT marked with ``@pytest.mark.integration`` because they need no
live infrastructure.  The marker is reserved for tests that require a real
Redis or PostgreSQL container.

Design note
-----------
``time.monotonic()`` is monkeypatched (via ``freezegun``-style approach using
``unittest.mock.patch``) to control the OPEN → HALF_OPEN recovery window
transition without sleeping.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.cache.redis_client import RedisClient
from src.core.schemas.provider import CircuitState
from src.providers.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    _cb_key,
    _now_ms,
)
from tests.mocks.provider_mocks import MockRedis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockRedisClient(RedisClient):
    """``RedisClient`` backed by ``MockRedis`` for in-memory testing.

    ``RedisClient`` wraps a ``redis.asyncio.Redis`` instance.  ``MockRedis``
    implements the same async interface, so we can pass it to the ``RedisClient``
    constructor directly.  This lets the circuit breaker exercise its full
    read / write / JSON round-trip path without a live Redis process.
    """

    def __init__(self, mock_redis: MockRedis) -> None:
        # Bypass the type annotation — MockRedis is duck-typed compatible.
        super().__init__(mock_redis)  # type: ignore[arg-type]


def _make_cb(
    mock_redis: MockRedis,
    *,
    provider: str = "test_provider",
    capability: str = "HISTORICAL_OHLCV",
    failure_threshold: int = 3,
    recovery_window_sec: int = 60,
) -> CircuitBreaker:
    """Factory that wires a fresh ``CircuitBreaker`` to the given ``MockRedis``."""
    redis_client = _MockRedisClient(mock_redis)
    return CircuitBreaker(
        provider=provider,
        capability=capability,
        redis_client=redis_client,
        failure_threshold=failure_threshold,
        recovery_window_sec=recovery_window_sec,
    )


async def _succeeding_fn() -> str:
    """A provider function that always succeeds."""
    return "ok"


async def _failing_fn() -> str:
    """A provider function that always raises a generic runtime error."""
    raise RuntimeError("provider error")


# ---------------------------------------------------------------------------
# 1. Initial state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_state_is_closed(mock_redis: MockRedis) -> None:
    """A freshly created CircuitBreaker starts in the CLOSED state."""
    cb = _make_cb(mock_redis)
    assert await cb.get_state() == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 2. CLOSED → OPEN: failure_threshold consecutive failures open the circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closed_to_open_on_threshold_failures(mock_redis: MockRedis) -> None:
    """After ``failure_threshold`` failures the circuit transitions to OPEN.

    Requirements 5.4: circuit opens after configurable failure_threshold (1–100).
    """
    cb = _make_cb(mock_redis, failure_threshold=3)

    # Two failures — circuit should remain CLOSED.
    await cb.record_failure(reason="timeout")
    await cb.record_failure(reason="timeout")
    assert await cb.get_state() == CircuitState.CLOSED

    # Third failure meets the threshold — circuit must open.
    await cb.record_failure(reason="timeout")
    assert await cb.get_state() == CircuitState.OPEN


@pytest.mark.asyncio
async def test_single_failure_opens_circuit_when_threshold_is_one(
    mock_redis: MockRedis,
) -> None:
    """With ``failure_threshold=1``, the very first failure opens the circuit."""
    cb = _make_cb(mock_redis, failure_threshold=1)

    await cb.record_failure(reason="connection_refused")
    assert await cb.get_state() == CircuitState.OPEN


@pytest.mark.asyncio
async def test_state_persisted_to_redis_on_open(mock_redis: MockRedis) -> None:
    """Opening the circuit writes the state to Redis with the correct JSON payload."""
    cb = _make_cb(mock_redis, failure_threshold=1)
    await cb.record_failure(reason="dns_error")

    key = _cb_key("test_provider", "HISTORICAL_OHLCV")
    raw = await mock_redis.get(key)
    assert raw is not None, "State must be written to Redis when circuit opens"

    state_dict = json.loads(raw)
    assert state_dict["state"] == CircuitState.OPEN.value
    assert state_dict["failure_count"] == 1
    assert state_dict["opened_at_ms"] is not None
    assert state_dict["last_failure_reason"] == "dns_error"


# ---------------------------------------------------------------------------
# 3. OPEN state: fail-fast — provider is never called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_circuit_rejects_calls_immediately(mock_redis: MockRedis) -> None:
    """When the circuit is OPEN, ``call()`` raises ``CircuitOpenError`` without
    invoking the provider function (fail-fast behaviour).

    Requirements 5.4: circuit breaker blocks all calls when OPEN.
    """
    cb = _make_cb(mock_redis, failure_threshold=1)
    await cb.record_failure(reason="provider_down")

    provider_called = False

    async def _spy_fn() -> str:
        nonlocal provider_called
        provider_called = True
        return "should_not_reach"

    with pytest.raises(CircuitOpenError) as exc_info:
        await cb.call(_spy_fn)

    assert not provider_called, "Provider must NOT be called when circuit is OPEN"
    assert exc_info.value.state == CircuitState.OPEN
    assert exc_info.value.provider == "test_provider"
    assert exc_info.value.capability == "HISTORICAL_OHLCV"


@pytest.mark.asyncio
async def test_open_circuit_rejects_multiple_calls(mock_redis: MockRedis) -> None:
    """Every subsequent call while the circuit is OPEN raises ``CircuitOpenError``."""
    cb = _make_cb(mock_redis, failure_threshold=1)
    await cb.record_failure()

    for _ in range(5):
        with pytest.raises(CircuitOpenError):
            await cb.call(_succeeding_fn)


# ---------------------------------------------------------------------------
# 4. OPEN → HALF_OPEN: recovery window elapses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_transitions_to_half_open_after_recovery_window(
    mock_redis: MockRedis,
) -> None:
    """After ``recovery_window_sec`` elapses the circuit advances to HALF_OPEN.

    We monkeypatch ``src.providers.circuit_breaker._now_ms`` to advance the
    clock without sleeping.

    Requirements 5.4: circuit transitions to HALF_OPEN after recovery window.
    """
    cb = _make_cb(mock_redis, failure_threshold=1, recovery_window_sec=60)
    await cb.record_failure(reason="timeout")

    # Simulate 61 seconds passing by monkeypatching _now_ms used inside
    # circuit_breaker module.
    with patch("src.providers.circuit_breaker._now_ms") as mock_now:
        # Return opened_at_ms + 61 000 ms so elapsed_sec >= recovery_window_sec.
        key = _cb_key("test_provider", "HISTORICAL_OHLCV")
        raw = await mock_redis.get(key)
        state_dict = json.loads(raw)  # type: ignore[arg-type]
        opened_at = state_dict["opened_at_ms"]
        mock_now.return_value = opened_at + 61_000  # 61 seconds later

        state = await cb.get_state()

    assert state == CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_open_stays_open_before_recovery_window(mock_redis: MockRedis) -> None:
    """The circuit stays OPEN if the recovery window has NOT yet elapsed."""
    cb = _make_cb(mock_redis, failure_threshold=1, recovery_window_sec=60)
    await cb.record_failure(reason="timeout")

    with patch("src.providers.circuit_breaker._now_ms") as mock_now:
        key = _cb_key("test_provider", "HISTORICAL_OHLCV")
        raw = await mock_redis.get(key)
        state_dict = json.loads(raw)  # type: ignore[arg-type]
        opened_at = state_dict["opened_at_ms"]
        mock_now.return_value = opened_at + 30_000  # only 30 seconds — still OPEN

        state = await cb.get_state()

    assert state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# 5. HALF_OPEN: exactly one probe is allowed through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_half_open_allows_exactly_one_probe(mock_redis: MockRedis) -> None:
    """In HALF_OPEN state exactly one probe request is dispatched; all others
    are rejected with ``CircuitOpenError``.

    Requirements 5.4: "exactly one probe request is dispatched."
    """
    cb = _make_cb(mock_redis, failure_threshold=1, recovery_window_sec=60)
    await cb.record_failure()

    # Advance clock past recovery window.
    key = _cb_key("test_provider", "HISTORICAL_OHLCV")
    raw = await mock_redis.get(key)
    state_dict = json.loads(raw)  # type: ignore[arg-type]
    opened_at = state_dict["opened_at_ms"]

    with patch("src.providers.circuit_breaker._now_ms", return_value=opened_at + 61_000):
        # Manually advance state to HALF_OPEN via get_state().
        assert await cb.get_state() == CircuitState.HALF_OPEN

    # Now start the probe call but hold it in-flight with an event.
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()

    async def _slow_probe() -> str:
        probe_started.set()
        await probe_release.wait()
        return "probe_result"

    probe_task = asyncio.create_task(cb.call(_slow_probe))
    await asyncio.wait_for(probe_started.wait(), timeout=2.0)

    # While probe is in-flight, a second call must be rejected immediately.
    with pytest.raises(CircuitOpenError) as exc_info:
        await cb.call(_succeeding_fn)

    assert exc_info.value.state == CircuitState.HALF_OPEN

    # Release the probe so cleanup is clean.
    probe_release.set()
    result = await probe_task
    assert result == "probe_result"


# ---------------------------------------------------------------------------
# 6. HALF_OPEN → CLOSED: probe succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_half_open_to_closed_on_probe_success(mock_redis: MockRedis) -> None:
    """When the probe in HALF_OPEN state succeeds, the circuit closes.

    Requirements 5.4: probe success → CLOSED transition.
    """
    cb = _make_cb(mock_redis, failure_threshold=1, recovery_window_sec=60)
    await cb.record_failure()

    # Advance to HALF_OPEN.
    key = _cb_key("test_provider", "HISTORICAL_OHLCV")
    raw = await mock_redis.get(key)
    state_dict = json.loads(raw)  # type: ignore[arg-type]
    opened_at = state_dict["opened_at_ms"]

    with patch("src.providers.circuit_breaker._now_ms", return_value=opened_at + 61_000):
        await cb.get_state()  # advances to HALF_OPEN

    # Probe succeeds.
    result = await cb.call(_succeeding_fn)
    assert result == "ok"

    # record_success must be called externally by the caller after a successful
    # probe to close the circuit (matches the real gateway usage pattern).
    await cb.record_success()

    assert await cb.get_state() == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_closed_circuit_resets_failure_count_on_success(
    mock_redis: MockRedis,
) -> None:
    """``record_success()`` in CLOSED state resets the failure counter to zero."""
    cb = _make_cb(mock_redis, failure_threshold=5)

    # Record 3 failures (below threshold).
    for _ in range(3):
        await cb.record_failure()

    # A successful call resets the counter.
    await cb.record_success()

    # Now 3 more failures should NOT open the circuit (counter was reset).
    for _ in range(3):
        await cb.record_failure()

    assert await cb.get_state() == CircuitState.CLOSED

    # But a 4th failure still won't open (threshold=5, count=4).
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.CLOSED

    # 5th failure meets threshold — now it opens.
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.OPEN


# ---------------------------------------------------------------------------
# 7. HALF_OPEN → OPEN: probe fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_half_open_to_open_on_probe_failure(mock_redis: MockRedis) -> None:
    """When the probe in HALF_OPEN state fails, the circuit immediately re-opens.

    Requirements 5.4: probe failure → back to OPEN.
    """
    cb = _make_cb(mock_redis, failure_threshold=1, recovery_window_sec=60)
    await cb.record_failure(reason="initial_failure")

    # Advance to HALF_OPEN.
    key = _cb_key("test_provider", "HISTORICAL_OHLCV")
    raw = await mock_redis.get(key)
    state_dict = json.loads(raw)  # type: ignore[arg-type]
    opened_at = state_dict["opened_at_ms"]

    with patch("src.providers.circuit_breaker._now_ms", return_value=opened_at + 61_000):
        await cb.get_state()  # advances to HALF_OPEN

    # Record probe failure directly (simulating an exception handler calling this).
    await cb.record_failure(reason="probe_failed")

    assert await cb.get_state() == CircuitState.OPEN


@pytest.mark.asyncio
async def test_half_open_probe_failure_persists_open_state_to_redis(
    mock_redis: MockRedis,
) -> None:
    """After a probe failure, Redis stores OPEN with a fresh opened_at_ms."""
    cb = _make_cb(mock_redis, failure_threshold=1, recovery_window_sec=60)
    await cb.record_failure(reason="first")

    key = _cb_key("test_provider", "HISTORICAL_OHLCV")
    raw_open = await mock_redis.get(key)
    first_open_at = json.loads(raw_open)["opened_at_ms"]  # type: ignore[arg-type]

    # Advance to HALF_OPEN.
    with patch("src.providers.circuit_breaker._now_ms", return_value=first_open_at + 61_000):
        await cb.get_state()

    # Probe fails — re-opens with a new timestamp.
    second_open_time = first_open_at + 62_000
    with patch("src.providers.circuit_breaker._now_ms", return_value=second_open_time):
        await cb.record_failure(reason="probe_failed")

    raw_reopen = await mock_redis.get(key)
    state_dict = json.loads(raw_reopen)  # type: ignore[arg-type]
    assert state_dict["state"] == CircuitState.OPEN.value
    assert state_dict["opened_at_ms"] == second_open_time


# ---------------------------------------------------------------------------
# 8. HTTP 429 does NOT increment failure counter (Requirements 5.5, 5.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_rate_limit_does_not_increment_failure_count(
    mock_redis: MockRedis,
) -> None:
    """``record_rate_limit()`` must NOT increment the failure counter.

    Requirements 5.5: HTTP 429 with Retry-After does not count as a failure.
    Requirements 5.6: HTTP 429 without Retry-After does not count as a failure.
    """
    cb = _make_cb(mock_redis, failure_threshold=3)

    # Simulate 10 rate-limit responses — circuit must remain CLOSED.
    for i in range(10):
        retry_after = 60 if i % 2 == 0 else None  # alternates with / without header
        await cb.record_rate_limit(retry_after_sec=retry_after)

    assert await cb.get_state() == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_rate_limit_does_not_open_circuit_after_real_failures(
    mock_redis: MockRedis,
) -> None:
    """Rate-limit responses interspersed with real failures do not count toward
    the threshold."""
    cb = _make_cb(mock_redis, failure_threshold=3)

    # Two real failures — below threshold.
    await cb.record_failure(reason="timeout")
    await cb.record_failure(reason="timeout")

    # Many rate-limit responses — must not tip the count over.
    for _ in range(20):
        await cb.record_rate_limit(retry_after_sec=60)

    assert await cb.get_state() == CircuitState.CLOSED

    # Third real failure now meets the threshold.
    await cb.record_failure(reason="timeout")
    assert await cb.get_state() == CircuitState.OPEN


@pytest.mark.asyncio
async def test_record_rate_limit_with_retry_after_header(mock_redis: MockRedis) -> None:
    """``record_rate_limit(retry_after_sec=N)`` is accepted without side-effects."""
    cb = _make_cb(mock_redis, failure_threshold=2)
    await cb.record_rate_limit(retry_after_sec=120)
    assert await cb.get_state() == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_record_rate_limit_without_retry_after_header(mock_redis: MockRedis) -> None:
    """``record_rate_limit(retry_after_sec=None)`` applies default 60-second backoff
    semantics but still does NOT open the circuit."""
    cb = _make_cb(mock_redis, failure_threshold=2)
    await cb.record_rate_limit(retry_after_sec=None)
    assert await cb.get_state() == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 9. MARKET_CLOSED does NOT increment failure counter (Requirement 5.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_market_closed_does_not_increment_failure_count(
    mock_redis: MockRedis,
) -> None:
    """``record_market_closed()`` must not increment the failure counter or
    change the circuit state.

    Requirement 5.7: market-closed semantics are not provider failures.
    """
    cb = _make_cb(mock_redis, failure_threshold=3)

    # 10 market-closed events — circuit must remain CLOSED.
    for _ in range(10):
        await cb.record_market_closed()

    assert await cb.get_state() == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_market_closed_interspersed_with_failures(mock_redis: MockRedis) -> None:
    """Market-closed events interspersed with real failures do not inflate the
    failure counter beyond actual failures."""
    cb = _make_cb(mock_redis, failure_threshold=3)

    await cb.record_failure(reason="timeout")
    await cb.record_market_closed()
    await cb.record_market_closed()
    await cb.record_failure(reason="timeout")
    await cb.record_market_closed()

    # Only 2 real failures — still CLOSED.
    assert await cb.get_state() == CircuitState.CLOSED

    # Third real failure — opens.
    await cb.record_failure(reason="timeout")
    assert await cb.get_state() == CircuitState.OPEN


# ---------------------------------------------------------------------------
# 10. UNSUPPORTED_CAPABILITY does NOT increment failure counter (Requirement 5.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_unsupported_capability_does_not_change_state(
    mock_redis: MockRedis,
) -> None:
    """``record_unsupported_capability()`` must not increment the failure counter.

    Requirement 5.8: unsupported-capability responses are not provider failures.
    """
    cb = _make_cb(mock_redis, failure_threshold=3)

    for _ in range(10):
        await cb.record_unsupported_capability()

    assert await cb.get_state() == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 11. Redis state persists across simulated process restarts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_survives_new_circuit_breaker_instance(
    mock_redis: MockRedis,
) -> None:
    """State written to Redis is read back correctly by a new CircuitBreaker
    instance sharing the same Redis backend — simulates a service restart.
    """
    cb1 = _make_cb(mock_redis, failure_threshold=1)
    await cb1.record_failure(reason="startup_probe_fail")
    assert await cb1.get_state() == CircuitState.OPEN

    # Simulate process restart — new instance, same Redis.
    cb2 = _make_cb(mock_redis, failure_threshold=1)
    assert await cb2.get_state() == CircuitState.OPEN


@pytest.mark.asyncio
async def test_closed_state_survives_new_instance(mock_redis: MockRedis) -> None:
    """CLOSED state (default) is correctly interpreted by a new instance."""
    cb1 = _make_cb(mock_redis, failure_threshold=2)
    await cb1.record_failure()
    await cb1.record_success()  # resets to CLOSED

    cb2 = _make_cb(mock_redis, failure_threshold=2)
    assert await cb2.get_state() == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 12. Reset utility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_clears_open_circuit(mock_redis: MockRedis) -> None:
    """``reset()`` forces the circuit back to CLOSED with zeroed counters."""
    cb = _make_cb(mock_redis, failure_threshold=1)
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.OPEN

    await cb.reset()
    assert await cb.get_state() == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_reset_clears_failure_count_in_redis(mock_redis: MockRedis) -> None:
    """After ``reset()``, the Redis payload reflects zeroed counters."""
    cb = _make_cb(mock_redis, failure_threshold=2)
    await cb.record_failure()
    await cb.reset()

    key = _cb_key("test_provider", "HISTORICAL_OHLCV")
    raw = await mock_redis.get(key)
    assert raw is not None

    state_dict = json.loads(raw)
    assert state_dict["state"] == CircuitState.CLOSED.value
    assert state_dict["failure_count"] == 0
    assert state_dict["opened_at_ms"] is None


# ---------------------------------------------------------------------------
# 13. Constructor validation
# ---------------------------------------------------------------------------


def test_invalid_failure_threshold_raises() -> None:
    """``failure_threshold`` outside [1, 100] raises ``ValueError``."""
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreaker("p", "c", failure_threshold=0)

    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreaker("p", "c", failure_threshold=101)


def test_invalid_recovery_window_raises() -> None:
    """``recovery_window_sec`` outside [1, 3600] raises ``ValueError``."""
    with pytest.raises(ValueError, match="recovery_window_sec"):
        CircuitBreaker("p", "c", recovery_window_sec=0)

    with pytest.raises(ValueError, match="recovery_window_sec"):
        CircuitBreaker("p", "c", recovery_window_sec=3601)


def test_valid_boundary_values() -> None:
    """Boundary values 1 and 100 / 1 and 3600 are accepted without errors."""
    cb_min = CircuitBreaker("p", "c", failure_threshold=1, recovery_window_sec=1)
    cb_max = CircuitBreaker("p", "c", failure_threshold=100, recovery_window_sec=3600)
    assert cb_min is not None
    assert cb_max is not None


# ---------------------------------------------------------------------------
# 14. In-memory fallback when Redis is unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_works_without_redis_client() -> None:
    """A ``CircuitBreaker`` with ``redis_client=None`` uses in-memory state."""
    cb = CircuitBreaker(
        provider="no_redis_provider",
        capability="LIVE_QUOTE",
        redis_client=None,
        failure_threshold=2,
        recovery_window_sec=60,
    )

    assert await cb.get_state() == CircuitState.CLOSED

    await cb.record_failure()
    assert await cb.get_state() == CircuitState.CLOSED

    await cb.record_failure()
    assert await cb.get_state() == CircuitState.OPEN


# ---------------------------------------------------------------------------
# 15. cb_key helper
# ---------------------------------------------------------------------------


def test_cb_key_format() -> None:
    """``_cb_key`` produces the expected ``mds:cb:{provider}:{capability}`` pattern."""
    assert _cb_key("angel_one", "HISTORICAL_OHLCV") == "mds:cb:angel_one:HISTORICAL_OHLCV"
    assert _cb_key("binance", "CRYPTO_KLINES") == "mds:cb:binance:CRYPTO_KLINES"


# ---------------------------------------------------------------------------
# 16. Concurrent calls in CLOSED state pass through independently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_calls_in_closed_state(mock_redis: MockRedis) -> None:
    """Multiple concurrent calls in CLOSED state all succeed independently."""
    cb = _make_cb(mock_redis, failure_threshold=10)

    results = await asyncio.gather(
        cb.call(_succeeding_fn),
        cb.call(_succeeding_fn),
        cb.call(_succeeding_fn),
    )

    assert results == ["ok", "ok", "ok"]
    # No failures recorded — circuit stays CLOSED.
    assert await cb.get_state() == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 17. ``call()`` wrapper propagates provider exceptions without altering state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_propagates_provider_exception(mock_redis: MockRedis) -> None:
    """``call()`` propagates the exception raised by the provider function.
    The circuit breaker itself does not catch provider exceptions — it is the
    caller's responsibility to call ``record_failure()`` after catching them.
    """
    cb = _make_cb(mock_redis, failure_threshold=3)

    with pytest.raises(RuntimeError, match="provider error"):
        await cb.call(_failing_fn)

    # The CircuitBreaker does NOT auto-increment on exceptions — callers must
    # call record_failure() explicitly.  State remains CLOSED.
    assert await cb.get_state() == CircuitState.CLOSED
