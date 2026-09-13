"""
tests/unit/providers/test_circuit_breaker.py

Unit tests for the CircuitBreaker state machine.

Covers every state transition, special-case responses (rate limit, market
closed), in-memory fallback when Redis is unavailable, and the circuit_open
structured log event.

Requirements: 5.4, 5.5, 5.7, 5.8, 18.5
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.cache.redis_client import RedisClient, RedisUnavailableError
from src.core.schemas.provider import CircuitState
from src.providers.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    _cb_key,
    _default_state_dict,
)

# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


class FakeRedis:
    """In-process dict-backed fake Redis compatible with RedisClient.

    Stores string → string mappings and exposes the four async primitives
    that CircuitBreaker uses: ``get``, ``set_with_ttl``, ``delete``,
    ``ttl_remaining``.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> int:
        removed = key in self._store
        self._store.pop(key, None)
        return int(removed)

    async def ttl_remaining(self, key: str) -> int:
        return 86_400 if key in self._store else -2

    def raw(self, key: str) -> Optional[dict[str, Any]]:
        raw = self._store.get(key)
        return json.loads(raw) if raw is not None else None


def make_fake_redis_client() -> RedisClient:
    """Return a ``RedisClient`` instance backed by ``FakeRedis``."""
    fake = FakeRedis()
    client = RedisClient.__new__(RedisClient)
    client._client = fake  # type: ignore[attr-defined]
    # Patch the four methods to delegate to FakeRedis directly
    client.get = fake.get  # type: ignore[method-assign]
    client.set_with_ttl = fake.set_with_ttl  # type: ignore[method-assign]
    client.delete = fake.delete  # type: ignore[method-assign]
    client.ttl_remaining = fake.ttl_remaining  # type: ignore[method-assign]
    return client, fake  # type: ignore[return-value]


def _make_cb(
    redis_client: Optional[RedisClient] = None,
    failure_threshold: int = 3,
    recovery_window_sec: int = 60,
    provider: str = "angel_one",
    capability: str = "HISTORICAL_OHLCV",
) -> CircuitBreaker:
    return CircuitBreaker(
        provider=provider,
        capability=capability,
        redis_client=redis_client,
        failure_threshold=failure_threshold,
        recovery_window_sec=recovery_window_sec,
    )


async def _ok_fn() -> str:
    """Dummy provider function that always succeeds."""
    return "ok"


async def _fail_fn() -> None:
    """Dummy provider function that always raises."""
    raise RuntimeError("provider error")


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestCircuitBreakerConstructor:
    def test_valid_parameters(self) -> None:
        cb = _make_cb(failure_threshold=5, recovery_window_sec=120)
        assert cb.provider == "angel_one"
        assert cb.capability == "HISTORICAL_OHLCV"

    def test_failure_threshold_lower_bound(self) -> None:
        cb = _make_cb(failure_threshold=1)
        assert cb._failure_threshold == 1  # noqa: SLF001

    def test_failure_threshold_upper_bound(self) -> None:
        cb = _make_cb(failure_threshold=100)
        assert cb._failure_threshold == 100  # noqa: SLF001

    def test_failure_threshold_below_range_raises(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold"):
            _make_cb(failure_threshold=0)

    def test_failure_threshold_above_range_raises(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold"):
            _make_cb(failure_threshold=101)

    def test_recovery_window_lower_bound(self) -> None:
        cb = _make_cb(recovery_window_sec=1)
        assert cb._recovery_window_sec == 1  # noqa: SLF001

    def test_recovery_window_upper_bound(self) -> None:
        cb = _make_cb(recovery_window_sec=3600)
        assert cb._recovery_window_sec == 3600  # noqa: SLF001

    def test_recovery_window_below_range_raises(self) -> None:
        with pytest.raises(ValueError, match="recovery_window_sec"):
            _make_cb(recovery_window_sec=0)

    def test_recovery_window_above_range_raises(self) -> None:
        with pytest.raises(ValueError, match="recovery_window_sec"):
            _make_cb(recovery_window_sec=3601)

    def test_redis_key_pattern(self) -> None:
        assert _cb_key("angel_one", "HISTORICAL_OHLCV") == "mds:cb:angel_one:HISTORICAL_OHLCV"

    def test_repr(self) -> None:
        cb = _make_cb(failure_threshold=5, recovery_window_sec=30)
        r = repr(cb)
        assert "angel_one" in r
        assert "HISTORICAL_OHLCV" in r
        assert "threshold=5" in r


# ---------------------------------------------------------------------------
# CLOSED state — stays CLOSED on success
# ---------------------------------------------------------------------------


class TestClosedStateSuccess:
    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self) -> None:
        cb = _make_cb()
        state = await cb.get_state()
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_call_succeeds_in_closed_state(self) -> None:
        cb = _make_cb()
        result = await cb.call(_ok_fn)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_record_success_keeps_circuit_closed(self) -> None:
        cb = _make_cb()
        await cb.record_success()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_single_failure_does_not_open_below_threshold(self) -> None:
        cb = _make_cb(failure_threshold=3)
        await cb.record_failure("network timeout")
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failures_below_threshold_stay_closed(self) -> None:
        cb = _make_cb(failure_threshold=3)
        await cb.record_failure()
        await cb.record_failure()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_count_resets_after_success(self) -> None:
        cb = _make_cb(failure_threshold=3)
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        # After success, failure_count is reset — two more failures should
        # NOT open the circuit (threshold is 3).
        await cb.record_failure()
        await cb.record_failure()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_record_success_resets_failure_count_in_state(self) -> None:
        cb = _make_cb(failure_threshold=5)
        # Drive failure count to 3.
        for _ in range(3):
            await cb.record_failure()
        await cb.record_success()
        # State dict should have failure_count = 0.
        assert cb._mem["failure_count"] == 0  # noqa: SLF001


# ---------------------------------------------------------------------------
# CLOSED → OPEN: threshold reached
# ---------------------------------------------------------------------------


class TestClosedToOpen:
    @pytest.mark.asyncio
    async def test_circuit_opens_when_threshold_reached(self) -> None:
        cb = _make_cb(failure_threshold=3)
        for _ in range(3):
            await cb.record_failure("timeout")
        assert await cb.get_state() == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_circuit_opens_exactly_at_threshold(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_failure()
        assert await cb.get_state() == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_opened_at_ms_is_set_on_open(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_failure()
        assert cb._mem["opened_at_ms"] is not None  # noqa: SLF001
        assert isinstance(cb._mem["opened_at_ms"], int)  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_failure_reason_is_stored(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_failure("upstream 500")
        assert cb._mem["last_failure_reason"] == "upstream 500"  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_circuit_open_log_emitted_on_open(self) -> None:
        cb = _make_cb(failure_threshold=2)
        with patch.object(cb, "_emit_circuit_open_log") as mock_log:
            await cb.record_failure()
            mock_log.assert_not_called()
            await cb.record_failure()
            mock_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_circuit_open_log_not_emitted_below_threshold(self) -> None:
        cb = _make_cb(failure_threshold=3)
        with patch.object(cb, "_emit_circuit_open_log") as mock_log:
            await cb.record_failure()
            await cb.record_failure()
            mock_log.assert_not_called()

    @pytest.mark.asyncio
    async def test_circuit_open_structlog_fields(self) -> None:
        """Verify the structured log event contains required fields (Req 18.5)."""
        cb = _make_cb(failure_threshold=1)
        log_calls: list[dict] = []

        original_emit = cb._emit_circuit_open_log

        def capture_emit(failure_count, reason):
            original_emit(failure_count, reason)

        with patch("src.providers.circuit_breaker.logger") as mock_logger:
            await cb.record_failure("test reason")
            # Ensure warning was called with the required keyword args
            mock_logger.warning.assert_called_once()
            call_kwargs = mock_logger.warning.call_args
            # First positional arg is the event name
            assert call_kwargs.args[0] == "circuit_open"
            kw = call_kwargs.kwargs
            assert "provider" in kw
            assert "capability" in kw
            assert "failureRate" in kw
            assert "transitionTimestamp" in kw


# ---------------------------------------------------------------------------
# OPEN state — rejects all requests
# ---------------------------------------------------------------------------


class TestOpenStateRejectsRequests:
    @pytest.mark.asyncio
    async def test_call_raises_circuit_open_error_when_open(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_failure()
        with pytest.raises(CircuitOpenError) as exc_info:
            await cb.call(_ok_fn)
        assert exc_info.value.provider == "angel_one"
        assert exc_info.value.capability == "HISTORICAL_OHLCV"
        assert exc_info.value.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_provider_fn_not_called_when_open(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_failure()
        mock_fn = AsyncMock(return_value="should_not_be_called")
        with pytest.raises(CircuitOpenError):
            await cb.call(mock_fn)
        mock_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_additional_failures_in_open_do_not_change_timestamp(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_failure()
        opened_at = cb._mem["opened_at_ms"]  # noqa: SLF001
        await cb.record_failure("another failure")
        # opened_at_ms must NOT change after circuit is already OPEN.
        assert cb._mem["opened_at_ms"] == opened_at  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_circuit_open_log_not_re_emitted_on_subsequent_failure(self) -> None:
        cb = _make_cb(failure_threshold=1)
        with patch.object(cb, "_emit_circuit_open_log") as mock_log:
            await cb.record_failure()   # opens circuit — log emitted once
            mock_log.reset_mock()
            await cb.record_failure()   # already open — log NOT emitted again
            mock_log.assert_not_called()

    @pytest.mark.asyncio
    async def test_circuit_open_error_message_contains_provider(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_failure()
        with pytest.raises(CircuitOpenError) as exc_info:
            await cb.call(_ok_fn)
        assert "angel_one" in str(exc_info.value)


# ---------------------------------------------------------------------------
# OPEN → HALF_OPEN: recovery window elapsed
# ---------------------------------------------------------------------------


class TestOpenToHalfOpen:
    @pytest.mark.asyncio
    async def test_state_advances_to_half_open_after_recovery_window(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        assert await cb.get_state() == CircuitState.OPEN
        # Simulate recovery window by backdating opened_at_ms.
        cb._mem["opened_at_ms"] -= 2_000  # 2 seconds in the past  # noqa: SLF001
        assert await cb.get_state() == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_call_is_allowed_when_no_probe_in_flight(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        # Advance to HALF_OPEN first.
        await cb.get_state()
        result = await cb.call(_ok_fn)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_only_one_probe_allowed_in_half_open(self) -> None:
        """Second call in HALF_OPEN is rejected while probe is in-flight."""
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()  # advance to HALF_OPEN

        probe_started = asyncio.Event()
        probe_proceed = asyncio.Event()

        async def slow_probe() -> str:
            probe_started.set()
            await probe_proceed.wait()
            return "probe_result"

        # Start the probe in a background task.
        probe_task = asyncio.create_task(cb.call(slow_probe))
        await probe_started.wait()

        # While probe is in-flight, a second call must be rejected.
        with pytest.raises(CircuitOpenError) as exc_info:
            await cb.call(_ok_fn)
        assert exc_info.value.state == CircuitState.HALF_OPEN

        # Allow the probe to finish cleanly.
        probe_proceed.set()
        await probe_task

    @pytest.mark.asyncio
    async def test_get_state_advances_open_to_half_open_after_window(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=60)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 61_000  # 61 seconds elapsed  # noqa: SLF001
        state = await cb.get_state()
        assert state == CircuitState.HALF_OPEN


# ---------------------------------------------------------------------------
# HALF_OPEN → CLOSED: probe succeeds
# ---------------------------------------------------------------------------


class TestHalfOpenToClosedOnProbeSuccess:
    @pytest.mark.asyncio
    async def test_record_success_in_half_open_transitions_to_closed(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()  # → HALF_OPEN
        await cb.record_success()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_count_reset_after_half_open_to_closed(self) -> None:
        cb = _make_cb(failure_threshold=3, recovery_window_sec=1)
        for _ in range(3):
            await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()  # → HALF_OPEN
        await cb.record_success()
        assert cb._mem["failure_count"] == 0  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_probe_in_flight_cleared_after_success(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()
        await cb.call(_ok_fn)          # probe call
        await cb.record_success()
        assert cb._mem["probe_in_flight"] is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_circuit_closed_log_emitted(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()

        with patch("src.providers.circuit_breaker.logger") as mock_logger:
            await cb.record_success()
            mock_logger.info.assert_called_once()
            assert mock_logger.info.call_args.args[0] == "circuit_closed"


# ---------------------------------------------------------------------------
# HALF_OPEN → OPEN: probe fails
# ---------------------------------------------------------------------------


class TestHalfOpenToOpenOnProbeFailure:
    @pytest.mark.asyncio
    async def test_record_failure_in_half_open_transitions_to_open(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()  # → HALF_OPEN
        # Record a failure from the probe.
        await cb.record_failure("probe failed")
        assert await cb.get_state() == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_opened_at_ms_reset_on_re_open(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        first_opened_at = cb._mem["opened_at_ms"]  # noqa: SLF001
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()  # → HALF_OPEN
        await cb.record_failure("re-open")
        # opened_at_ms must be refreshed to now (≥ first_opened_at).
        assert cb._mem["opened_at_ms"] >= first_opened_at  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_probe_in_flight_cleared_on_re_open(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()
        cb._mem["probe_in_flight"] = True  # noqa: SLF001
        await cb.record_failure("re-open")
        assert cb._mem["probe_in_flight"] is False  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_circuit_open_log_re_emitted_on_re_open(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()
        with patch.object(cb, "_emit_circuit_open_log") as mock_log:
            await cb.record_failure("probe failed")
            mock_log.assert_called_once()


# ---------------------------------------------------------------------------
# HTTP 429 / rate limit — does NOT increment failure counter (Req 5.5, 5.6)
# ---------------------------------------------------------------------------


class TestRateLimitDoesNotIncrementCounter:
    @pytest.mark.asyncio
    async def test_record_rate_limit_does_not_change_failure_count(self) -> None:
        cb = _make_cb(failure_threshold=2)
        await cb.record_rate_limit(retry_after_sec=30)
        assert cb._mem["failure_count"] == 0  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_record_rate_limit_does_not_open_circuit(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_rate_limit()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_many_rate_limits_never_open_circuit(self) -> None:
        cb = _make_cb(failure_threshold=1)
        for _ in range(100):
            await cb.record_rate_limit()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_record_rate_limit_with_retry_after_header(self) -> None:
        cb = _make_cb(failure_threshold=1)
        # Should not raise; retry_after_sec is optional.
        await cb.record_rate_limit(retry_after_sec=60)
        await cb.record_rate_limit()   # no retry_after_sec
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_rate_limits_mixed_with_failures(self) -> None:
        """Rate limits must not contribute to failure count at all."""
        cb = _make_cb(failure_threshold=2)
        await cb.record_failure()       # count = 1
        await cb.record_rate_limit()    # count still 1
        await cb.record_rate_limit()    # count still 1
        # One more genuine failure → opens at threshold 2.
        await cb.record_failure()       # count = 2 → OPEN
        assert await cb.get_state() == CircuitState.OPEN


# ---------------------------------------------------------------------------
# MARKET_CLOSED — does NOT increment failure counter (Req 5.7)
# ---------------------------------------------------------------------------


class TestMarketClosedDoesNotIncrementCounter:
    @pytest.mark.asyncio
    async def test_record_market_closed_does_not_change_failure_count(self) -> None:
        cb = _make_cb(failure_threshold=2)
        await cb.record_market_closed()
        assert cb._mem["failure_count"] == 0  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_record_market_closed_does_not_open_circuit(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_market_closed()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_many_market_closed_events_never_open_circuit(self) -> None:
        cb = _make_cb(failure_threshold=1)
        for _ in range(100):
            await cb.record_market_closed()
        assert await cb.get_state() == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# UNSUPPORTED_CAPABILITY — does NOT increment failure counter (Req 5.8)
# ---------------------------------------------------------------------------


class TestUnsupportedCapabilityDoesNotIncrementCounter:
    @pytest.mark.asyncio
    async def test_record_unsupported_capability_does_not_change_failure_count(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_unsupported_capability()
        assert cb._mem["failure_count"] == 0  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_record_unsupported_capability_does_not_open_circuit(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_unsupported_capability()
        assert await cb.get_state() == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Redis-backed state — state persists and shared across instances (Req 5.4)
# ---------------------------------------------------------------------------


class TestRedisPersistence:
    @pytest.mark.asyncio
    async def test_state_written_to_redis_on_failure(self) -> None:
        redis_client, fake = make_fake_redis_client()
        cb = _make_cb(redis_client=redis_client, failure_threshold=1)
        await cb.record_failure("test")
        key = _cb_key("angel_one", "HISTORICAL_OHLCV")
        stored = fake.raw(key)
        assert stored is not None
        assert stored["state"] == CircuitState.OPEN.value

    @pytest.mark.asyncio
    async def test_second_instance_reads_state_from_redis(self) -> None:
        """Two CircuitBreaker instances sharing a Redis key see the same state."""
        redis_client, fake = make_fake_redis_client()
        cb1 = _make_cb(redis_client=redis_client, failure_threshold=1)
        cb2 = _make_cb(redis_client=redis_client, failure_threshold=1)

        await cb1.record_failure("from cb1")
        # cb2 must see OPEN state even though it recorded no failures.
        assert await cb2.get_state() == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_reset_writes_closed_state_to_redis(self) -> None:
        redis_client, fake = make_fake_redis_client()
        cb = _make_cb(redis_client=redis_client, failure_threshold=1)
        await cb.record_failure()
        await cb.reset()
        key = _cb_key("angel_one", "HISTORICAL_OHLCV")
        stored = fake.raw(key)
        assert stored["state"] == CircuitState.CLOSED.value
        assert stored["failure_count"] == 0

    @pytest.mark.asyncio
    async def test_success_recorded_persisted_to_redis(self) -> None:
        redis_client, fake = make_fake_redis_client()
        cb = _make_cb(redis_client=redis_client, failure_threshold=3)
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        key = _cb_key("angel_one", "HISTORICAL_OHLCV")
        stored = fake.raw(key)
        assert stored["failure_count"] == 0
        assert stored["state"] == CircuitState.CLOSED.value

    @pytest.mark.asyncio
    async def test_different_capabilities_have_different_keys(self) -> None:
        redis_client, fake = make_fake_redis_client()
        cb_hist = CircuitBreaker(
            provider="angel_one",
            capability="HISTORICAL_OHLCV",
            redis_client=redis_client,
            failure_threshold=1,
        )
        cb_live = CircuitBreaker(
            provider="angel_one",
            capability="LIVE_QUOTE",
            redis_client=redis_client,
            failure_threshold=5,
        )
        await cb_hist.record_failure()
        assert await cb_hist.get_state() == CircuitState.OPEN
        # Live quote circuit unaffected.
        assert await cb_live.get_state() == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# Redis unavailable — graceful in-memory fallback (Req 5.4 / design)
# ---------------------------------------------------------------------------


class TestRedisFallback:
    @pytest.mark.asyncio
    async def test_circuit_works_without_redis(self) -> None:
        """No redis_client at all — in-memory path should work correctly."""
        cb = _make_cb(redis_client=None, failure_threshold=2)
        await cb.record_failure()
        assert await cb.get_state() == CircuitState.CLOSED
        await cb.record_failure()
        assert await cb.get_state() == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_redis_get_failure_falls_back_to_memory(self) -> None:
        redis_client, fake = make_fake_redis_client()

        async def raising_get(key: str):
            raise RedisUnavailableError("connection refused")

        redis_client.get = raising_get  # type: ignore[method-assign]

        cb = _make_cb(redis_client=redis_client, failure_threshold=1)
        # Should not raise; falls back to in-memory default state (CLOSED).
        state = await cb.get_state()
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_redis_set_failure_continues_with_memory(self) -> None:
        redis_client, fake = make_fake_redis_client()

        async def raising_set(key, value, ttl_seconds):
            raise RedisUnavailableError("write failed")

        redis_client.set_with_ttl = raising_set  # type: ignore[method-assign]

        cb = _make_cb(redis_client=redis_client, failure_threshold=1)
        # record_failure should not raise even when Redis write fails.
        await cb.record_failure("redis down")
        # In-memory state should still track the failure.
        assert cb._mem["state"] == CircuitState.OPEN.value  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_call_succeeds_when_redis_down(self) -> None:
        """Provider calls pass through even when Redis is completely unavailable."""
        redis_client, fake = make_fake_redis_client()

        async def always_fail(*args, **kwargs):
            raise RedisUnavailableError("redis down")

        redis_client.get = always_fail  # type: ignore[method-assign]
        redis_client.set_with_ttl = always_fail  # type: ignore[method-assign]

        cb = _make_cb(redis_client=redis_client, failure_threshold=3)
        result = await cb.call(_ok_fn)
        assert result == "ok"


# ---------------------------------------------------------------------------
# reset()
# ---------------------------------------------------------------------------


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_clears_open_circuit(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_failure()
        assert await cb.get_state() == CircuitState.OPEN
        await cb.reset()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_reset_clears_failure_count(self) -> None:
        cb = _make_cb(failure_threshold=5)
        for _ in range(4):
            await cb.record_failure()
        await cb.reset()
        assert cb._mem["failure_count"] == 0  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_reset_from_half_open(self) -> None:
        cb = _make_cb(failure_threshold=1, recovery_window_sec=1)
        await cb.record_failure()
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()  # → HALF_OPEN
        await cb.reset()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_call_works_after_reset(self) -> None:
        cb = _make_cb(failure_threshold=1)
        await cb.record_failure()
        await cb.reset()
        result = await cb.call(_ok_fn)
        assert result == "ok"


# ---------------------------------------------------------------------------
# Full lifecycle: multi-cycle trip
# ---------------------------------------------------------------------------


class TestFullLifecycleMultiCycle:
    @pytest.mark.asyncio
    async def test_circuit_can_trip_and_recover_multiple_times(self) -> None:
        cb = _make_cb(failure_threshold=2, recovery_window_sec=1)

        # Cycle 1: trip → recover
        await cb.record_failure()
        await cb.record_failure()
        assert await cb.get_state() == CircuitState.OPEN
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()  # → HALF_OPEN
        await cb.record_success()
        assert await cb.get_state() == CircuitState.CLOSED

        # Cycle 2: trip again
        await cb.record_failure()
        await cb.record_failure()
        assert await cb.get_state() == CircuitState.OPEN
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()  # → HALF_OPEN

        # Probe fails → back to OPEN
        await cb.record_failure("second cycle probe fail")
        assert await cb.get_state() == CircuitState.OPEN

        # Eventually recover
        cb._mem["opened_at_ms"] -= 2_000  # noqa: SLF001
        await cb.get_state()  # → HALF_OPEN
        await cb.record_success()
        assert await cb.get_state() == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_default_state_dict_is_correct(self) -> None:
        sd = _default_state_dict()
        assert sd["state"] == CircuitState.CLOSED.value
        assert sd["failure_count"] == 0
        assert sd["opened_at_ms"] is None
        assert sd["last_failure_reason"] is None
        assert sd["probe_in_flight"] is False
