"""
tests/unit/providers/test_rate_limiter.py

Unit tests for src/providers/rate_limiter.py.

Requirements: 5.2, 5.3

Coverage:
  PROVIDER_RATE_LIMITS
    - All eight providers have the correct rate limits.

  ProviderQueueFullError
    - error_code is always "PROVIDER_QUEUE_FULL".
    - Attributes (provider_id, capability, queue_depth) are set correctly.

  _LocalBucket
    - try_consume returns True when tokens available.
    - try_consume returns False when bucket is empty.
    - Tokens refill over time based on elapsed duration.
    - Tokens are capped at capacity after refill.

  TokenBucketRateLimiter construction
    - Raises ValueError for queue_max_depth out of range (0, 10 001).
    - Raises ValueError for burst_multiplier <= 0.
    - Defaults are applied correctly.

  try_acquire (Redis path)
    - Calls Redis Lua eval with correct arguments.
    - Returns True when Lua script returns "1".
    - Returns False when Lua script returns "0".

  try_acquire (local fallback)
    - Falls back to local bucket when Redis raises.
    - Returns True on first call (full bucket).
    - Returns False when local bucket exhausted.

  acquire (happy path)
    - Blocks until a token becomes available.
    - Returns immediately when tokens are available.

  acquire (queue overflow)
    - Raises ProviderQueueFullError when queue is full before any wait.

  acquire (timeout)
    - Raises asyncio.TimeoutError when no token arrives within timeout.

  acquire (waiting counter)
    - waiting_count increments while coroutine is waiting.
    - waiting_count decrements after acquire returns (success or error).

  release
    - release() is a no-op (no exception raised).

  Redis unavailability fallback
    - When Redis raises, try_acquire falls back to local bucket.
    - Local bucket is shared across multiple fallback calls.

  Cross-replica Lua script (atomic)
    - The Lua script string contains the expected atomic operations.

  _redis_key helper
    - Returns correct mds:rl:{provider}:{capability} pattern.

  current_tokens
    - Returns token count from Redis when available.
    - Returns local bucket count when Redis unavailable.
    - Returns None when neither is initialised.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.schemas.provider import ProviderId
from src.providers.rate_limiter import (
    PROVIDER_RATE_LIMITS,
    ProviderQueueFullError,
    TokenBucketRateLimiter,
    _LocalBucket,
    _LUA_TOKEN_BUCKET,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_redis_mock(*, lua_return: Any = 1) -> AsyncMock:
    """Return a minimal Redis async mock.

    By default the Lua eval call returns 1 (token acquired).
    """
    mock = AsyncMock()
    mock.eval = AsyncMock(return_value=lua_return)
    mock.get = AsyncMock(return_value=None)
    return mock


# ===========================================================================
# PROVIDER_RATE_LIMITS
# ===========================================================================


class TestProviderRateLimits:
    """Verify provider-level rate limits match the Capability Matrix."""

    def test_angel_one_3_rps(self) -> None:
        assert PROVIDER_RATE_LIMITS[ProviderId.ANGEL_ONE.value] == 3.0

    def test_upstox_10_rps(self) -> None:
        assert PROVIDER_RATE_LIMITS[ProviderId.UPSTOX.value] == 10.0

    def test_scrapling_nse_2_rps(self) -> None:
        assert PROVIDER_RATE_LIMITS[ProviderId.SCRAPLING_NSE.value] == 2.0

    def test_jugaad_data_1_rps(self) -> None:
        assert PROVIDER_RATE_LIMITS[ProviderId.JUGAAD_DATA.value] == 1.0

    def test_openchart_5_rps(self) -> None:
        assert PROVIDER_RATE_LIMITS[ProviderId.OPENCHART.value] == 5.0

    def test_yahoo_finance_1_rps(self) -> None:
        assert PROVIDER_RATE_LIMITS[ProviderId.YAHOO_FINANCE.value] == 1.0

    def test_binance_20_rps(self) -> None:
        assert PROVIDER_RATE_LIMITS[ProviderId.BINANCE.value] == 20.0

    def test_deribit_5_rps(self) -> None:
        assert PROVIDER_RATE_LIMITS[ProviderId.DERIBIT.value] == 5.0

    def test_delta_10_rps(self) -> None:
        """DS2-RCA-001: Delta Exchange India added with 10 req/s limit."""
        assert PROVIDER_RATE_LIMITS[ProviderId.DELTA.value] == 10.0

    def test_all_nine_providers_present(self) -> None:
        """All ProviderId enum members must have a rate limit entry (was 8, now 9 with Delta)."""
        expected = {p.value for p in ProviderId}
        assert expected == set(PROVIDER_RATE_LIMITS.keys()), (
            "PROVIDER_RATE_LIMITS must contain exactly one entry per ProviderId"
        )

    def test_all_values_positive(self) -> None:
        for provider, rps in PROVIDER_RATE_LIMITS.items():
            assert rps > 0, f"Rate limit for {provider!r} must be positive; got {rps}"


# ===========================================================================
# ProviderQueueFullError
# ===========================================================================


class TestProviderQueueFullError:
    def test_error_code_is_provider_queue_full(self) -> None:
        err = ProviderQueueFullError("angel_one", "HISTORICAL_OHLCV", 100)
        assert err.error_code == "PROVIDER_QUEUE_FULL"

    def test_attributes_set_correctly(self) -> None:
        err = ProviderQueueFullError("binance", "CRYPTO_KLINES", 50)
        assert err.provider_id == "binance"
        assert err.capability == "CRYPTO_KLINES"
        assert err.queue_depth == 50

    def test_message_contains_useful_info(self) -> None:
        err = ProviderQueueFullError("upstox", "LIVE_QUOTE", 200)
        msg = str(err)
        assert "upstox" in msg
        assert "LIVE_QUOTE" in msg
        assert "PROVIDER_QUEUE_FULL" in msg

    def test_is_runtime_error_subclass(self) -> None:
        err = ProviderQueueFullError("angel_one", "cap", 100)
        assert isinstance(err, RuntimeError)


# ===========================================================================
# _LocalBucket
# ===========================================================================


class TestLocalBucket:
    def test_try_consume_returns_true_when_tokens_available(self) -> None:
        bucket = _LocalBucket(capacity=10.0, tokens=5.0, refill_rate=1.0)
        assert bucket.try_consume() is True
        assert bucket.tokens == pytest.approx(4.0, abs=0.01)

    def test_try_consume_returns_false_when_empty(self) -> None:
        bucket = _LocalBucket(capacity=10.0, tokens=0.0, refill_rate=1.0)
        result = bucket.try_consume()
        assert result is False
        assert bucket.tokens == pytest.approx(0.0, abs=0.01)

    def test_tokens_refill_over_time(self) -> None:
        """Bucket refills when _refill() is triggered by elapsed time."""
        # Start with 0 tokens, then simulate 2 seconds of elapsed time
        bucket = _LocalBucket(capacity=10.0, tokens=0.0, refill_rate=3.0)
        bucket.last_refill_ts = time.monotonic() - 2.0  # simulate 2s elapsed
        bucket._refill()
        # 3 tokens/s × 2s = 6 tokens, capped at capacity (10)
        assert bucket.tokens == pytest.approx(6.0, abs=0.1)

    def test_tokens_capped_at_capacity(self) -> None:
        bucket = _LocalBucket(capacity=5.0, tokens=4.5, refill_rate=10.0)
        bucket.last_refill_ts = time.monotonic() - 10.0  # would add 100 tokens
        bucket._refill()
        assert bucket.tokens == pytest.approx(5.0, abs=0.01)

    def test_consecutive_consume_drains_bucket(self) -> None:
        bucket = _LocalBucket(capacity=3.0, tokens=3.0, refill_rate=0.0)
        assert bucket.try_consume() is True   # 3 → 2
        assert bucket.try_consume() is True   # 2 → 1
        assert bucket.try_consume() is True   # 1 → 0
        assert bucket.try_consume() is False  # 0 → reject

    def test_fractional_tokens_reject_before_reaching_one(self) -> None:
        bucket = _LocalBucket(capacity=5.0, tokens=0.9, refill_rate=0.0)
        assert bucket.try_consume() is False  # 0.9 < 1.0

    def test_burst_capacity_determines_initial_limit(self) -> None:
        # If capacity=6 and all tokens available, we can consume 6 times
        bucket = _LocalBucket(capacity=6.0, tokens=6.0, refill_rate=0.0)
        consumed = 0
        while bucket.try_consume():
            consumed += 1
        assert consumed == 6


# ===========================================================================
# TokenBucketRateLimiter construction
# ===========================================================================


class TestRateLimiterConstruction:
    def test_queue_max_depth_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="queue_max_depth"):
            TokenBucketRateLimiter(queue_max_depth=0)

    def test_queue_max_depth_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            TokenBucketRateLimiter(queue_max_depth=-1)

    def test_queue_max_depth_10001_raises(self) -> None:
        with pytest.raises(ValueError, match="queue_max_depth"):
            TokenBucketRateLimiter(queue_max_depth=10_001)

    def test_burst_multiplier_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="burst_multiplier"):
            TokenBucketRateLimiter(burst_multiplier=0.0)

    def test_burst_multiplier_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="burst_multiplier"):
            TokenBucketRateLimiter(burst_multiplier=-1.0)

    def test_default_queue_depth_100(self) -> None:
        limiter = TokenBucketRateLimiter()
        assert limiter._queue_max_depth == 100

    def test_default_burst_multiplier_2(self) -> None:
        limiter = TokenBucketRateLimiter()
        assert limiter._burst_multiplier == 2.0

    def test_none_redis_is_allowed(self) -> None:
        limiter = TokenBucketRateLimiter(redis_client=None)
        assert limiter._redis is None

    def test_custom_queue_depth_applied(self) -> None:
        limiter = TokenBucketRateLimiter(queue_max_depth=500)
        assert limiter._queue_max_depth == 500

    def test_boundary_queue_depth_1(self) -> None:
        limiter = TokenBucketRateLimiter(queue_max_depth=1)
        assert limiter._queue_max_depth == 1

    def test_boundary_queue_depth_10000(self) -> None:
        limiter = TokenBucketRateLimiter(queue_max_depth=10_000)
        assert limiter._queue_max_depth == 10_000


# ===========================================================================
# _redis_key helper
# ===========================================================================


class TestRedisKey:
    def test_key_pattern(self) -> None:
        key = TokenBucketRateLimiter._redis_key("angel_one", "HISTORICAL_OHLCV")
        assert key == "mds:rl:angel_one:HISTORICAL_OHLCV"

    def test_key_uses_provider_and_capability(self) -> None:
        key = TokenBucketRateLimiter._redis_key("binance", "CRYPTO_KLINES")
        assert key == "mds:rl:binance:CRYPTO_KLINES"

    def test_key_starts_with_mds_rl(self) -> None:
        key = TokenBucketRateLimiter._redis_key("upstox", "LIVE_QUOTE")
        assert key.startswith("mds:rl:")


# ===========================================================================
# try_acquire — Redis path
# ===========================================================================


class TestTryAcquireRedisPath:
    @pytest.mark.asyncio
    async def test_returns_true_when_lua_returns_1(self) -> None:
        mock_redis = _make_redis_mock(lua_return=1)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        result = await limiter.try_acquire("angel_one", "HISTORICAL_OHLCV")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_lua_returns_0(self) -> None:
        mock_redis = _make_redis_mock(lua_return=0)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        result = await limiter.try_acquire("angel_one", "HISTORICAL_OHLCV")
        assert result is False

    @pytest.mark.asyncio
    async def test_lua_eval_called_with_correct_key(self) -> None:
        mock_redis = _make_redis_mock(lua_return=1)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        await limiter.try_acquire("upstox", "LIVE_QUOTE")

        # Verify eval was called once
        mock_redis.eval.assert_awaited_once()
        call_args = mock_redis.eval.call_args
        # KEYS[1] is the second positional arg (after the script + numkeys)
        assert call_args.args[2] == "mds:rl:upstox:LIVE_QUOTE"

    @pytest.mark.asyncio
    async def test_lua_eval_capacity_is_rps_times_burst(self) -> None:
        """capacity passed to Lua = RPS × burst_multiplier."""
        mock_redis = _make_redis_mock(lua_return=1)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis, burst_multiplier=3.0)
        await limiter.try_acquire("binance", "CRYPTO_KLINES")

        call_args = mock_redis.eval.call_args
        capacity_arg = float(call_args.args[3])   # ARGV[1]
        # Binance is 20 req/s × 3 burst = 60 capacity
        assert capacity_arg == pytest.approx(60.0)

    @pytest.mark.asyncio
    async def test_lua_eval_refill_rate_equals_rps(self) -> None:
        """refill_rate passed to Lua = RPS from PROVIDER_RATE_LIMITS."""
        mock_redis = _make_redis_mock(lua_return=1)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        await limiter.try_acquire("binance", "CRYPTO_KLINES")

        call_args = mock_redis.eval.call_args
        refill_rate_arg = float(call_args.args[4])   # ARGV[2]
        assert refill_rate_arg == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_lua_script_provided_as_first_arg(self) -> None:
        """The Lua script text is passed as the first argument to eval."""
        mock_redis = _make_redis_mock(lua_return=1)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        await limiter.try_acquire("angel_one", "LIVE_QUOTE")

        call_args = mock_redis.eval.call_args
        script_arg = call_args.args[0]
        assert "tokens" in script_arg
        assert "refill_rate" in script_arg


# ===========================================================================
# try_acquire — local fallback
# ===========================================================================


class TestTryAcquireLocalFallback:
    @pytest.mark.asyncio
    async def test_falls_back_when_redis_raises(self) -> None:
        """When Redis.eval raises, try_acquire falls back to the local bucket."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("Redis down"))
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        # The local bucket starts full — first call should succeed
        result = await limiter.try_acquire("angel_one", "HISTORICAL_OHLCV")
        assert result is True

    @pytest.mark.asyncio
    async def test_local_fallback_with_no_redis(self) -> None:
        """With redis_client=None, always uses local bucket."""
        limiter = TokenBucketRateLimiter(redis_client=None)
        result = await limiter.try_acquire("angel_one", "LIVE_QUOTE")
        assert result is True

    @pytest.mark.asyncio
    async def test_local_bucket_starts_full(self) -> None:
        """New local buckets begin with capacity (burst) tokens available."""
        limiter = TokenBucketRateLimiter(redis_client=None, burst_multiplier=2.0)
        rps = PROVIDER_RATE_LIMITS["angel_one"]           # 3 req/s
        capacity = int(rps * 2.0)                          # burst of 6

        # Should be able to acquire `capacity` tokens immediately
        acquired = 0
        for _ in range(capacity + 2):
            if await limiter.try_acquire("angel_one", "HISTORICAL_OHLCV"):
                acquired += 1
        assert acquired == capacity

    @pytest.mark.asyncio
    async def test_local_bucket_returns_false_when_exhausted(self) -> None:
        limiter = TokenBucketRateLimiter(redis_client=None)
        # Angel One burst = 3 × 2 = 6 tokens
        for _ in range(6):
            await limiter.try_acquire("angel_one", "LIVE_QUOTE")
        # 7th call should fail
        result = await limiter.try_acquire("angel_one", "LIVE_QUOTE")
        assert result is False

    @pytest.mark.asyncio
    async def test_local_buckets_are_isolated_per_capability(self) -> None:
        """Each capability gets its own independent local bucket."""
        limiter = TokenBucketRateLimiter(redis_client=None)
        # Exhaust LIVE_QUOTE bucket for angel_one
        for _ in range(20):
            await limiter.try_acquire("angel_one", "LIVE_QUOTE")
        # HISTORICAL_OHLCV should still have tokens
        result = await limiter.try_acquire("angel_one", "HISTORICAL_OHLCV")
        assert result is True

    @pytest.mark.asyncio
    async def test_local_buckets_are_isolated_per_provider(self) -> None:
        """Each provider gets its own independent local bucket."""
        limiter = TokenBucketRateLimiter(redis_client=None)
        # Exhaust angel_one bucket
        for _ in range(20):
            await limiter.try_acquire("angel_one", "LIVE_QUOTE")
        # upstox should still have tokens
        result = await limiter.try_acquire("upstox", "LIVE_QUOTE")
        assert result is True

    @pytest.mark.asyncio
    async def test_redis_failure_logs_warning(self, caplog) -> None:
        """When Redis fails, a warning is logged."""
        import logging

        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("Redis down"))
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)

        with caplog.at_level(logging.WARNING):
            await limiter.try_acquire("angel_one", "LIVE_QUOTE")

        assert any("rate_limiter_redis_unavailable_fallback" in r.message for r in caplog.records)


# ===========================================================================
# Burst capacity
# ===========================================================================


class TestBurstCapacity:
    @pytest.mark.asyncio
    async def test_burst_multiplier_doubles_initial_capacity(self) -> None:
        """burst_multiplier=2 means 2× the RPS in initial token budget."""
        limiter = TokenBucketRateLimiter(redis_client=None, burst_multiplier=2.0)
        rps = PROVIDER_RATE_LIMITS["openchart"]   # 5 req/s → burst of 10

        acquired = 0
        for _ in range(15):
            if await limiter.try_acquire("openchart", "HISTORICAL_OHLCV"):
                acquired += 1

        assert acquired == 10

    @pytest.mark.asyncio
    async def test_burst_multiplier_1_equals_rps(self) -> None:
        """burst_multiplier=1 → capacity equals exactly the RPS value."""
        limiter = TokenBucketRateLimiter(redis_client=None, burst_multiplier=1.0)
        rps = int(PROVIDER_RATE_LIMITS["jugaad_data"])   # 1 req/s → burst of 1

        acquired = 0
        for _ in range(5):
            if await limiter.try_acquire("jugaad_data", "HISTORICAL_OHLCV"):
                acquired += 1

        assert acquired == rps  # exactly 1

    @pytest.mark.asyncio
    async def test_custom_burst_multiplier_3(self) -> None:
        """burst_multiplier=3 → 3× RPS initial tokens."""
        limiter = TokenBucketRateLimiter(redis_client=None, burst_multiplier=3.0)
        # Scrapling NSE: 2 req/s × 3 = 6 burst capacity
        expected_burst = int(PROVIDER_RATE_LIMITS["scrapling_nse"] * 3.0)

        acquired = 0
        for _ in range(expected_burst + 5):
            if await limiter.try_acquire("scrapling_nse", "OPTION_CHAIN"):
                acquired += 1

        assert acquired == expected_burst


# ===========================================================================
# acquire — happy path
# ===========================================================================


class TestAcquireHappyPath:
    @pytest.mark.asyncio
    async def test_acquire_returns_immediately_when_token_available(self) -> None:
        mock_redis = _make_redis_mock(lua_return=1)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        # Should not raise or block
        await limiter.acquire("angel_one", "HISTORICAL_OHLCV", timeout_sec=1.0)

    @pytest.mark.asyncio
    async def test_acquire_eventual_success_after_token_available(self) -> None:
        """acquire() polls until a token is available (or timeout)."""
        call_count = 0

        async def fake_eval(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Return 0 for first 3 calls, then 1
            return 1 if call_count > 3 else 0

        mock_redis = AsyncMock()
        mock_redis.eval = fake_eval
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        await limiter.acquire("angel_one", "LIVE_QUOTE", timeout_sec=5.0)
        assert call_count > 3


# ===========================================================================
# acquire — queue overflow (PROVIDER_QUEUE_FULL)
# ===========================================================================


class TestAcquireQueueOverflow:
    @pytest.mark.asyncio
    async def test_raises_provider_queue_full_when_queue_at_capacity(self) -> None:
        """When waiting count equals queue_max_depth, next caller is rejected."""
        # Redis always returns 0 so callers block
        mock_redis = _make_redis_mock(lua_return=0)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis, queue_max_depth=2)

        # Manually set waiting count to the limit
        limiter._waiting[("angel_one", "LIVE_QUOTE")] = 2

        with pytest.raises(ProviderQueueFullError) as exc_info:
            await limiter.acquire("angel_one", "LIVE_QUOTE", timeout_sec=0.1)

        err = exc_info.value
        assert err.error_code == "PROVIDER_QUEUE_FULL"
        assert err.provider_id == "angel_one"
        assert err.capability == "LIVE_QUOTE"

    @pytest.mark.asyncio
    async def test_error_code_is_provider_queue_full(self) -> None:
        mock_redis = _make_redis_mock(lua_return=0)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis, queue_max_depth=1)
        limiter._waiting[("binance", "CRYPTO_KLINES")] = 1

        with pytest.raises(ProviderQueueFullError) as exc_info:
            await limiter.acquire("binance", "CRYPTO_KLINES", timeout_sec=0.01)

        assert exc_info.value.error_code == "PROVIDER_QUEUE_FULL"

    @pytest.mark.asyncio
    async def test_queue_not_full_at_depth_minus_one(self) -> None:
        """A caller that arrives when queue is one below max should be accepted."""
        mock_redis = _make_redis_mock(lua_return=1)  # token immediately available
        limiter = TokenBucketRateLimiter(redis_client=mock_redis, queue_max_depth=3)
        limiter._waiting[("upstox", "LIVE_QUOTE")] = 2  # one below max

        # Should not raise ProviderQueueFullError
        await limiter.acquire("upstox", "LIVE_QUOTE", timeout_sec=1.0)


# ===========================================================================
# acquire — timeout
# ===========================================================================


class TestAcquireTimeout:
    @pytest.mark.asyncio
    async def test_raises_timeout_error_when_no_token_arrives(self) -> None:
        """When tokens never arrive, acquire() raises asyncio.TimeoutError."""
        mock_redis = _make_redis_mock(lua_return=0)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        with pytest.raises(asyncio.TimeoutError):
            await limiter.acquire("angel_one", "LIVE_QUOTE", timeout_sec=0.1)

    @pytest.mark.asyncio
    async def test_timeout_decrements_waiting_counter(self) -> None:
        """waiting_count must be 0 after a timeout (counter is always cleaned up)."""
        mock_redis = _make_redis_mock(lua_return=0)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)

        with pytest.raises(asyncio.TimeoutError):
            await limiter.acquire("angel_one", "LIVE_QUOTE", timeout_sec=0.05)

        assert limiter.waiting_count("angel_one", "LIVE_QUOTE") == 0


# ===========================================================================
# Waiting counter behaviour
# ===========================================================================


class TestWaitingCounter:
    @pytest.mark.asyncio
    async def test_waiting_count_increments_while_blocked(self) -> None:
        """While acquire() is waiting, waiting_count > 0."""
        # Tokens never arrive
        mock_redis = _make_redis_mock(lua_return=0)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)

        started = asyncio.Event()
        observed_counts: list[int] = []

        async def observer():
            # Wait until acquire is polling
            await asyncio.sleep(0.08)
            observed_counts.append(limiter.waiting_count("angel_one", "LIVE_QUOTE"))

        async def run_acquire():
            started.set()
            with pytest.raises(asyncio.TimeoutError):
                await limiter.acquire("angel_one", "LIVE_QUOTE", timeout_sec=0.15)

        await asyncio.gather(run_acquire(), observer())

        assert any(c > 0 for c in observed_counts), (
            "waiting_count should be > 0 while acquire is blocked"
        )

    @pytest.mark.asyncio
    async def test_waiting_count_zero_after_successful_acquire(self) -> None:
        """waiting_count is 0 after a successful acquire()."""
        mock_redis = _make_redis_mock(lua_return=1)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)

        await limiter.acquire("angel_one", "LIVE_QUOTE", timeout_sec=1.0)
        assert limiter.waiting_count("angel_one", "LIVE_QUOTE") == 0

    @pytest.mark.asyncio
    async def test_waiting_count_zero_after_queue_full_error(self) -> None:
        """waiting_count is not incremented when ProviderQueueFullError is raised."""
        mock_redis = _make_redis_mock(lua_return=0)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis, queue_max_depth=1)
        limiter._waiting[("deribit", "CRYPTO_OPTIONS")] = 1

        with pytest.raises(ProviderQueueFullError):
            await limiter.acquire("deribit", "CRYPTO_OPTIONS", timeout_sec=0.01)

        # Count should remain at 1 (not incremented by the rejected caller)
        assert limiter.waiting_count("deribit", "CRYPTO_OPTIONS") == 1


# ===========================================================================
# release — no-op
# ===========================================================================


class TestRelease:
    @pytest.mark.asyncio
    async def test_release_does_not_raise(self) -> None:
        limiter = TokenBucketRateLimiter()
        await limiter.release("angel_one", "LIVE_QUOTE")

    @pytest.mark.asyncio
    async def test_release_on_unknown_provider_does_not_raise(self) -> None:
        limiter = TokenBucketRateLimiter()
        await limiter.release("nonexistent_provider", "SOME_CAP")


# ===========================================================================
# Redis unavailability — complete fallback scenario
# ===========================================================================


class TestRedisUnavailabilityFallback:
    @pytest.mark.asyncio
    async def test_local_bucket_used_after_redis_failure(self) -> None:
        """All calls fall back to local bucket when Redis is permanently down."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("Redis unavailable"))
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)

        # All calls should fall back and succeed (bucket starts full)
        for _ in range(3):
            result = await limiter.try_acquire("openchart", "HISTORICAL_OHLCV")
            assert result is True

    @pytest.mark.asyncio
    async def test_local_bucket_is_shared_across_fallback_calls(self) -> None:
        """The same local bucket instance is reused across multiple fallback calls."""
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("down"))
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)

        # OpenChart: 5 req/s × burst 2 = 10 capacity
        acquired = 0
        for _ in range(15):
            if await limiter.try_acquire("openchart", "HISTORICAL_OHLCV"):
                acquired += 1

        assert acquired == 10  # exactly burst capacity, not 15

    @pytest.mark.asyncio
    async def test_acquire_works_via_fallback(self) -> None:
        """acquire() works through the local fallback (no Redis)."""
        limiter = TokenBucketRateLimiter(redis_client=None)
        # Should complete without error
        await limiter.acquire("deribit", "CRYPTO_OPTIONS", timeout_sec=1.0)


# ===========================================================================
# Cross-replica consistency — Lua script inspection
# ===========================================================================


class TestLuaScriptAtomic:
    def test_lua_script_references_cjson_for_atomic_state(self) -> None:
        """The Lua script uses cjson for serialisation (Redis built-in)."""
        assert "cjson" in _LUA_TOKEN_BUCKET

    def test_lua_script_uses_keys_and_argv(self) -> None:
        """The script uses KEYS[1] and ARGV parameters."""
        assert "KEYS[1]" in _LUA_TOKEN_BUCKET
        assert "ARGV[1]" in _LUA_TOKEN_BUCKET
        assert "ARGV[2]" in _LUA_TOKEN_BUCKET

    def test_lua_script_performs_get_and_set(self) -> None:
        """The script reads and writes the bucket state atomically."""
        assert "redis.call('GET'" in _LUA_TOKEN_BUCKET
        assert "redis.call('SET'" in _LUA_TOKEN_BUCKET

    def test_lua_script_refills_tokens(self) -> None:
        """The script applies the refill formula (elapsed × refill_rate)."""
        assert "elapsed" in _LUA_TOKEN_BUCKET
        assert "refill_rate" in _LUA_TOKEN_BUCKET

    def test_lua_script_caps_at_capacity(self) -> None:
        """Tokens are capped at capacity after refill."""
        assert "math.min" in _LUA_TOKEN_BUCKET
        assert "capacity" in _LUA_TOKEN_BUCKET

    def test_lua_script_returns_1_on_acquire_0_on_reject(self) -> None:
        """Return values 1 (acquired) and 0 (rejected) are present."""
        assert "acquired  = 1" in _LUA_TOKEN_BUCKET or "acquired = 1" in _LUA_TOKEN_BUCKET
        assert "return acquired" in _LUA_TOKEN_BUCKET

    def test_lua_script_sets_ttl_to_prevent_stale_keys(self) -> None:
        """Keys are set with EX (TTL) to prevent accumulation."""
        assert "'EX'" in _LUA_TOKEN_BUCKET

    @pytest.mark.asyncio
    async def test_lua_eval_called_exactly_once_per_try_acquire(self) -> None:
        """try_acquire issues exactly one Lua eval call (atomic)."""
        mock_redis = _make_redis_mock(lua_return=1)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        await limiter.try_acquire("angel_one", "LIVE_QUOTE")
        assert mock_redis.eval.await_count == 1


# ===========================================================================
# current_tokens diagnostic
# ===========================================================================


class TestCurrentTokens:
    @pytest.mark.asyncio
    async def test_returns_tokens_from_redis(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(
            return_value=json.dumps({"tokens": 7.5, "last_refill_ts": time.time()})
        )
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        tokens = await limiter.current_tokens("angel_one", "LIVE_QUOTE")
        assert tokens == pytest.approx(7.5)

    @pytest.mark.asyncio
    async def test_returns_none_when_key_missing_and_no_local_bucket(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)
        tokens = await limiter.current_tokens("angel_one", "LIVE_QUOTE")
        assert tokens is None

    @pytest.mark.asyncio
    async def test_returns_local_tokens_when_redis_fails(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=ConnectionError("down"))
        limiter = TokenBucketRateLimiter(redis_client=mock_redis)

        # Populate the local bucket via a fallback try_acquire
        mock_redis.eval = AsyncMock(side_effect=ConnectionError("down"))
        await limiter.try_acquire("openchart", "HISTORICAL_OHLCV")

        tokens = await limiter.current_tokens("openchart", "HISTORICAL_OHLCV")
        assert tokens is not None
        assert tokens >= 0

    @pytest.mark.asyncio
    async def test_returns_none_when_neither_source_initialised(self) -> None:
        limiter = TokenBucketRateLimiter(redis_client=None)
        tokens = await limiter.current_tokens("deribit", "CRYPTO_OPTIONS")
        # No calls made yet, no local bucket created yet
        assert tokens is None
