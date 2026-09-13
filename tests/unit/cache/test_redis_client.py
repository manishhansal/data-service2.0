"""
tests/unit/cache/test_redis_client.py

Unit tests for src/cache/redis_client.py.

Requirements: 9.2, 9.3

Coverage:
  TTL constants
    - All seven constants have the exact values mandated by the design.

  build_cache_key
    - Full key with all fields populated
    - Partial keys with None fields substituted with ``_``
    - Empty-string fields treated as inapplicable
    - Instrument master pattern (most fields inapplicable)
    - All-placeholder key when only data_type provided
    - Numeric timestamps converted to strings
    - Prefix is always ``mds``

  create_redis_pool
    - Returns the Redis client on successful PING
    - Propagates RedisConnectionError when PING fails
    - Client is configured with expected kwargs (decode_responses=True, etc.)

  RedisClient.get
    - Returns value on cache hit
    - Returns None on cache miss
    - Raises RedisUnavailableError on RedisError

  RedisClient.set_with_ttl
    - Calls SET with correct key, value, and ex parameter
    - Raises ValueError for ttl_seconds <= 0
    - Raises RedisUnavailableError on RedisError

  RedisClient.delete
    - Returns 1 when key existed and was deleted
    - Returns 0 when key did not exist
    - Raises RedisUnavailableError on RedisError

  RedisClient.ttl_remaining
    - Returns positive integer for key with TTL
    - Returns -1 for persistent key (no TTL)
    - Returns -2 for absent key
    - Raises RedisUnavailableError on RedisError
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError, TimeoutError as RedisTimeoutError

from src.cache.redis_client import (
    TTL_LIVE_QUOTE,
    TTL_BATCH_QUOTES,
    TTL_INTRADAY_CANDLES,
    TTL_DAILY_CANDLES,
    TTL_OPTION_CHAIN,
    TTL_INSTRUMENT_MASTER,
    TTL_PROVIDER_HEALTH,
    RedisClient,
    RedisUnavailableError,
    build_cache_key,
    create_redis_pool,
)


# ===========================================================================
# TTL Constants
# ===========================================================================


class TestTtlConstants:
    """Verify the exact values mandated by the design (Requirement 9.2)."""

    def test_live_quote_ttl(self) -> None:
        assert TTL_LIVE_QUOTE == 3

    def test_batch_quotes_ttl(self) -> None:
        assert TTL_BATCH_QUOTES == 3

    def test_intraday_candles_ttl(self) -> None:
        assert TTL_INTRADAY_CANDLES == 30

    def test_daily_candles_ttl_4_hours(self) -> None:
        assert TTL_DAILY_CANDLES == 14_400

    def test_option_chain_ttl(self) -> None:
        assert TTL_OPTION_CHAIN == 15

    def test_instrument_master_ttl_12_hours(self) -> None:
        assert TTL_INSTRUMENT_MASTER == 43_200

    def test_provider_health_ttl(self) -> None:
        assert TTL_PROVIDER_HEALTH == 5

    def test_all_ttls_are_positive(self) -> None:
        for name, val in [
            ("TTL_LIVE_QUOTE", TTL_LIVE_QUOTE),
            ("TTL_BATCH_QUOTES", TTL_BATCH_QUOTES),
            ("TTL_INTRADAY_CANDLES", TTL_INTRADAY_CANDLES),
            ("TTL_DAILY_CANDLES", TTL_DAILY_CANDLES),
            ("TTL_OPTION_CHAIN", TTL_OPTION_CHAIN),
            ("TTL_INSTRUMENT_MASTER", TTL_INSTRUMENT_MASTER),
            ("TTL_PROVIDER_HEALTH", TTL_PROVIDER_HEALTH),
        ]:
            assert val > 0, f"{name} must be positive, got {val}"


# ===========================================================================
# build_cache_key
# ===========================================================================


class TestBuildCacheKey:
    """Verify the key-namespace helper (Requirement 9.3)."""

    def test_prefix_is_always_mds(self) -> None:
        key = build_cache_key("quote")
        assert key.startswith("mds:")

    def test_full_key_all_fields(self) -> None:
        key = build_cache_key(
            "candle", "upstox", "NSE", "RELIANCE", "1m", 1705300000000, 1705386400000
        )
        assert key == "mds:candle:upstox:NSE:RELIANCE:1m:1705300000000:1705386400000"

    def test_live_quote_pattern(self) -> None:
        # mds:quote:angel_one:NSE:NIFTY:_:_:_
        key = build_cache_key("quote", "angel_one", "NSE", "NIFTY")
        assert key == "mds:quote:angel_one:NSE:NIFTY:_:_:_"

    def test_option_chain_pattern(self) -> None:
        # interval, from, to are not applicable for option chain
        key = build_cache_key("optchain", "scrapling", "NSE", "NIFTY")
        assert key == "mds:optchain:scrapling:NSE:NIFTY:_:_:_"

    def test_instrument_master_pattern(self) -> None:
        # All fields except data_type and exchange are not applicable
        key = build_cache_key("instrument_master", exchange="NSE")
        assert key == "mds:instrument_master:_:NSE:_:_:_:_"

    def test_all_none_fields_become_placeholders(self) -> None:
        key = build_cache_key("instrument_master")
        assert key == "mds:instrument_master:_:_:_:_:_:_"

    def test_empty_string_field_treated_as_placeholder(self) -> None:
        key = build_cache_key("quote", provider="", exchange="NSE", symbol="")
        assert key == "mds:quote:_:NSE:_:_:_:_"

    def test_whitespace_only_string_treated_as_placeholder(self) -> None:
        key = build_cache_key("quote", provider="   ")
        assert key == "mds:quote:_:_:_:_:_:_"

    def test_numeric_timestamps_converted_to_str(self) -> None:
        key = build_cache_key("candle", from_ts=1705300000000, to_ts=1705386400000)
        assert "1705300000000" in key
        assert "1705386400000" in key

    def test_string_timestamps_preserved(self) -> None:
        key = build_cache_key("candle", from_ts="2024-01-15T09:15:00Z", to_ts="2024-01-15T15:30:00Z")
        assert "2024-01-15T09:15:00Z" in key

    def test_key_has_eight_segments(self) -> None:
        # mds + 7 fields = 8 segments separated by ':'
        key = build_cache_key("candle", "angel_one", "NSE", "RELIANCE", "1m", 100, 200)
        assert key.count(":") == 7

    def test_data_type_is_second_segment(self) -> None:
        key = build_cache_key("provider_health")
        segments = key.split(":")
        assert segments[1] == "provider_health"

    def test_provider_is_third_segment(self) -> None:
        key = build_cache_key("quote", "upstox")
        segments = key.split(":")
        assert segments[2] == "upstox"

    def test_exchange_is_fourth_segment(self) -> None:
        key = build_cache_key("quote", exchange="NFO")
        segments = key.split(":")
        assert segments[3] == "NFO"

    def test_symbol_is_fifth_segment(self) -> None:
        key = build_cache_key("quote", symbol="BANKNIFTY")
        segments = key.split(":")
        assert segments[4] == "BANKNIFTY"

    def test_interval_is_sixth_segment(self) -> None:
        key = build_cache_key("candle", interval="5m")
        segments = key.split(":")
        assert segments[5] == "5m"

    def test_from_ts_is_seventh_segment(self) -> None:
        key = build_cache_key("candle", from_ts=12345)
        segments = key.split(":")
        assert segments[6] == "12345"

    def test_to_ts_is_eighth_segment(self) -> None:
        key = build_cache_key("candle", to_ts=99999)
        segments = key.split(":")
        assert segments[7] == "99999"

    def test_special_characters_in_symbol_preserved(self) -> None:
        # Symbols won't contain ':', but other chars should pass through
        key = build_cache_key("quote", symbol="NIFTY50")
        assert "NIFTY50" in key


# ===========================================================================
# create_redis_pool
# ===========================================================================


class TestCreateRedisPool:
    """Tests for the connection pool factory (Requirement 9.3)."""

    @pytest.mark.asyncio
    async def test_returns_redis_client_on_success(self) -> None:
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)

        with patch("src.cache.redis_client.aioredis.from_url", return_value=mock_client):
            result = await create_redis_pool("redis://localhost:6379/0")

        assert result is mock_client

    @pytest.mark.asyncio
    async def test_ping_is_called_on_startup(self) -> None:
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)

        with patch("src.cache.redis_client.aioredis.from_url", return_value=mock_client):
            await create_redis_pool("redis://localhost:6379/0")

        mock_client.ping.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_propagates_connection_error_on_failed_ping(self) -> None:
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(side_effect=RedisConnectionError("refused"))

        with patch("src.cache.redis_client.aioredis.from_url", return_value=mock_client):
            with pytest.raises(RedisConnectionError):
                await create_redis_pool("redis://localhost:6379/0")

    @pytest.mark.asyncio
    async def test_decode_responses_true(self) -> None:
        """Client must be configured with decode_responses=True."""
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        kwargs_captured: list[dict] = []

        def _capture(url: str, **kwargs: object) -> AsyncMock:
            kwargs_captured.append(kwargs)
            return mock_client

        with patch("src.cache.redis_client.aioredis.from_url", side_effect=_capture):
            await create_redis_pool("redis://localhost:6379/0")

        assert kwargs_captured[0]["decode_responses"] is True

    @pytest.mark.asyncio
    async def test_encoding_is_utf8(self) -> None:
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        kwargs_captured: list[dict] = []

        def _capture(url: str, **kwargs: object) -> AsyncMock:
            kwargs_captured.append(kwargs)
            return mock_client

        with patch("src.cache.redis_client.aioredis.from_url", side_effect=_capture):
            await create_redis_pool("redis://localhost:6379/0")

        assert kwargs_captured[0]["encoding"] == "utf-8"

    @pytest.mark.asyncio
    async def test_retry_on_timeout_enabled(self) -> None:
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        kwargs_captured: list[dict] = []

        def _capture(url: str, **kwargs: object) -> AsyncMock:
            kwargs_captured.append(kwargs)
            return mock_client

        with patch("src.cache.redis_client.aioredis.from_url", side_effect=_capture):
            await create_redis_pool("redis://localhost:6379/0")

        assert kwargs_captured[0]["retry_on_timeout"] is True

    @pytest.mark.asyncio
    async def test_url_passed_to_from_url(self) -> None:
        mock_client = AsyncMock()
        mock_client.ping = AsyncMock(return_value=True)
        urls_captured: list[str] = []

        def _capture(url: str, **kwargs: object) -> AsyncMock:
            urls_captured.append(url)
            return mock_client

        target_url = "redis://myredis:6380/1"
        with patch("src.cache.redis_client.aioredis.from_url", side_effect=_capture):
            await create_redis_pool(target_url)

        assert urls_captured[0] == target_url


# ===========================================================================
# Helpers for RedisClient tests
# ===========================================================================


def _make_redis_client(
    get_return: str | None = None,
    get_raises: Exception | None = None,
    set_raises: Exception | None = None,
    delete_return: int = 1,
    delete_raises: Exception | None = None,
    ttl_return: int = 30,
    ttl_raises: Exception | None = None,
) -> tuple[RedisClient, MagicMock]:
    """Build a RedisClient with a fully mocked inner Redis connection."""
    mock_redis = MagicMock()

    # get
    if get_raises:
        mock_redis.get = AsyncMock(side_effect=get_raises)
    else:
        mock_redis.get = AsyncMock(return_value=get_return)

    # set
    if set_raises:
        mock_redis.set = AsyncMock(side_effect=set_raises)
    else:
        mock_redis.set = AsyncMock(return_value=True)

    # delete
    if delete_raises:
        mock_redis.delete = AsyncMock(side_effect=delete_raises)
    else:
        mock_redis.delete = AsyncMock(return_value=delete_return)

    # ttl
    if ttl_raises:
        mock_redis.ttl = AsyncMock(side_effect=ttl_raises)
    else:
        mock_redis.ttl = AsyncMock(return_value=ttl_return)

    return RedisClient(mock_redis), mock_redis


# ===========================================================================
# RedisClient.get
# ===========================================================================


class TestRedisClientGet:
    """Tests for the async get() primitive."""

    @pytest.mark.asyncio
    async def test_returns_value_on_cache_hit(self) -> None:
        client, _ = _make_redis_client(get_return='{"ltp": 22150.5}')
        result = await client.get("mds:quote:angel_one:NSE:NIFTY:_:_:_")
        assert result == '{"ltp": 22150.5}'

    @pytest.mark.asyncio
    async def test_returns_none_on_cache_miss(self) -> None:
        client, _ = _make_redis_client(get_return=None)
        result = await client.get("mds:quote:angel_one:NSE:NIFTY:_:_:_")
        assert result is None

    @pytest.mark.asyncio
    async def test_calls_redis_get_with_correct_key(self) -> None:
        client, mock_redis = _make_redis_client(get_return="value")
        key = "mds:candle:upstox:NSE:RELIANCE:1m:100:200"
        await client.get(key)
        mock_redis.get.assert_awaited_once_with(key)

    @pytest.mark.asyncio
    async def test_raises_redis_unavailable_on_connection_error(self) -> None:
        client, _ = _make_redis_client(get_raises=RedisConnectionError("down"))
        with pytest.raises(RedisUnavailableError):
            await client.get("some:key")

    @pytest.mark.asyncio
    async def test_raises_redis_unavailable_on_response_error(self) -> None:
        client, _ = _make_redis_client(get_raises=ResponseError("WRONGTYPE"))
        with pytest.raises(RedisUnavailableError):
            await client.get("some:key")

    @pytest.mark.asyncio
    async def test_raises_redis_unavailable_on_timeout(self) -> None:
        client, _ = _make_redis_client(get_raises=RedisTimeoutError("timeout"))
        with pytest.raises(RedisUnavailableError):
            await client.get("some:key")

    @pytest.mark.asyncio
    async def test_redis_unavailable_error_wraps_original(self) -> None:
        original = RedisConnectionError("connection refused")
        client, _ = _make_redis_client(get_raises=original)
        with pytest.raises(RedisUnavailableError) as exc_info:
            await client.get("some:key")
        assert exc_info.value.__cause__ is original


# ===========================================================================
# RedisClient.set_with_ttl
# ===========================================================================


class TestRedisClientSetWithTtl:
    """Tests for the async set_with_ttl() primitive."""

    @pytest.mark.asyncio
    async def test_calls_set_with_correct_args(self) -> None:
        client, mock_redis = _make_redis_client()
        await client.set_with_ttl("mds:quote:angel_one:NSE:NIFTY:_:_:_", '{"ltp":100}', 3)
        mock_redis.set.assert_awaited_once_with(
            "mds:quote:angel_one:NSE:NIFTY:_:_:_", '{"ltp":100}', ex=3
        )

    @pytest.mark.asyncio
    async def test_live_quote_ttl_constant(self) -> None:
        """Using TTL_LIVE_QUOTE (3) results in ex=3."""
        client, mock_redis = _make_redis_client()
        await client.set_with_ttl("key", "val", TTL_LIVE_QUOTE)
        mock_redis.set.assert_awaited_once_with("key", "val", ex=3)

    @pytest.mark.asyncio
    async def test_daily_candles_ttl_constant(self) -> None:
        """Using TTL_DAILY_CANDLES (14400) results in ex=14400."""
        client, mock_redis = _make_redis_client()
        await client.set_with_ttl("key", "val", TTL_DAILY_CANDLES)
        mock_redis.set.assert_awaited_once_with("key", "val", ex=14_400)

    @pytest.mark.asyncio
    async def test_instrument_master_ttl_constant(self) -> None:
        client, mock_redis = _make_redis_client()
        await client.set_with_ttl("key", "val", TTL_INSTRUMENT_MASTER)
        mock_redis.set.assert_awaited_once_with("key", "val", ex=43_200)

    @pytest.mark.asyncio
    async def test_raises_value_error_on_zero_ttl(self) -> None:
        client, _ = _make_redis_client()
        with pytest.raises(ValueError, match="positive"):
            await client.set_with_ttl("key", "val", 0)

    @pytest.mark.asyncio
    async def test_raises_value_error_on_negative_ttl(self) -> None:
        client, _ = _make_redis_client()
        with pytest.raises(ValueError, match="positive"):
            await client.set_with_ttl("key", "val", -1)

    @pytest.mark.asyncio
    async def test_raises_redis_unavailable_on_connection_error(self) -> None:
        client, _ = _make_redis_client(set_raises=RedisConnectionError("down"))
        with pytest.raises(RedisUnavailableError):
            await client.set_with_ttl("key", "val", 30)

    @pytest.mark.asyncio
    async def test_raises_redis_unavailable_on_timeout(self) -> None:
        client, _ = _make_redis_client(set_raises=RedisTimeoutError("timeout"))
        with pytest.raises(RedisUnavailableError):
            await client.set_with_ttl("key", "val", 30)

    @pytest.mark.asyncio
    async def test_does_not_raise_on_valid_ttl(self) -> None:
        client, _ = _make_redis_client()
        # Should complete without raising.
        await client.set_with_ttl("key", "val", 1)


# ===========================================================================
# RedisClient.delete
# ===========================================================================


class TestRedisClientDelete:
    """Tests for the async delete() primitive."""

    @pytest.mark.asyncio
    async def test_returns_1_when_key_deleted(self) -> None:
        client, _ = _make_redis_client(delete_return=1)
        result = await client.delete("some:key")
        assert result == 1

    @pytest.mark.asyncio
    async def test_returns_0_when_key_not_found(self) -> None:
        client, _ = _make_redis_client(delete_return=0)
        result = await client.delete("nonexistent:key")
        assert result == 0

    @pytest.mark.asyncio
    async def test_calls_redis_delete_with_correct_key(self) -> None:
        client, mock_redis = _make_redis_client()
        key = "mds:quote:angel_one:NSE:NIFTY:_:_:_"
        await client.delete(key)
        mock_redis.delete.assert_awaited_once_with(key)

    @pytest.mark.asyncio
    async def test_raises_redis_unavailable_on_connection_error(self) -> None:
        client, _ = _make_redis_client(delete_raises=RedisConnectionError("down"))
        with pytest.raises(RedisUnavailableError):
            await client.delete("key")

    @pytest.mark.asyncio
    async def test_raises_redis_unavailable_on_timeout(self) -> None:
        client, _ = _make_redis_client(delete_raises=RedisTimeoutError("timeout"))
        with pytest.raises(RedisUnavailableError):
            await client.delete("key")

    @pytest.mark.asyncio
    async def test_redis_unavailable_wraps_original_cause(self) -> None:
        original = RedisConnectionError("ECONNREFUSED")
        client, _ = _make_redis_client(delete_raises=original)
        with pytest.raises(RedisUnavailableError) as exc_info:
            await client.delete("key")
        assert exc_info.value.__cause__ is original


# ===========================================================================
# RedisClient.ttl_remaining
# ===========================================================================


class TestRedisClientTtlRemaining:
    """Tests for the async ttl_remaining() primitive."""

    @pytest.mark.asyncio
    async def test_returns_positive_for_key_with_ttl(self) -> None:
        client, _ = _make_redis_client(ttl_return=25)
        result = await client.ttl_remaining("some:key")
        assert result == 25

    @pytest.mark.asyncio
    async def test_returns_minus_one_for_persistent_key(self) -> None:
        """Redis returns -1 when the key exists but has no TTL."""
        client, _ = _make_redis_client(ttl_return=-1)
        result = await client.ttl_remaining("persistent:key")
        assert result == -1

    @pytest.mark.asyncio
    async def test_returns_minus_two_for_absent_key(self) -> None:
        """Redis returns -2 when the key does not exist."""
        client, _ = _make_redis_client(ttl_return=-2)
        result = await client.ttl_remaining("absent:key")
        assert result == -2

    @pytest.mark.asyncio
    async def test_calls_redis_ttl_with_correct_key(self) -> None:
        client, mock_redis = _make_redis_client(ttl_return=10)
        key = "mds:candle:upstox:NSE:RELIANCE:1m:100:200"
        await client.ttl_remaining(key)
        mock_redis.ttl.assert_awaited_once_with(key)

    @pytest.mark.asyncio
    async def test_raises_redis_unavailable_on_connection_error(self) -> None:
        client, _ = _make_redis_client(ttl_raises=RedisConnectionError("down"))
        with pytest.raises(RedisUnavailableError):
            await client.ttl_remaining("key")

    @pytest.mark.asyncio
    async def test_raises_redis_unavailable_on_timeout(self) -> None:
        client, _ = _make_redis_client(ttl_raises=RedisTimeoutError("timeout"))
        with pytest.raises(RedisUnavailableError):
            await client.ttl_remaining("key")

    @pytest.mark.asyncio
    async def test_redis_unavailable_wraps_original_cause(self) -> None:
        original = RedisConnectionError("EOF")
        client, _ = _make_redis_client(ttl_raises=original)
        with pytest.raises(RedisUnavailableError) as exc_info:
            await client.ttl_remaining("key")
        assert exc_info.value.__cause__ is original


# ===========================================================================
# Integration-style: key-builder + client together
# ===========================================================================


class TestKeyBuilderWithClient:
    """Verify that build_cache_key output feeds correctly into the client."""

    @pytest.mark.asyncio
    async def test_get_with_built_key(self) -> None:
        client, mock_redis = _make_redis_client(get_return='{"data": 42}')
        key = build_cache_key("quote", "angel_one", "NSE", "NIFTY")
        result = await client.get(key)
        assert result == '{"data": 42}'
        mock_redis.get.assert_awaited_once_with("mds:quote:angel_one:NSE:NIFTY:_:_:_")

    @pytest.mark.asyncio
    async def test_set_then_get_uses_same_key(self) -> None:
        """set_with_ttl and get must operate on the exact same key string."""
        client, mock_redis = _make_redis_client(get_return='{"oi": 12345}')
        key = build_cache_key("quote", "upstox", "NFO", "BANKNIFTY")

        await client.set_with_ttl(key, '{"oi": 12345}', TTL_LIVE_QUOTE)
        result = await client.get(key)

        # The exact same key string must have been used for both calls.
        set_key = mock_redis.set.call_args[0][0]
        get_key = mock_redis.get.call_args[0][0]
        assert set_key == get_key == "mds:quote:upstox:NFO:BANKNIFTY:_:_:_"
        assert result == '{"oi": 12345}'

    @pytest.mark.asyncio
    async def test_delete_with_built_key(self) -> None:
        client, mock_redis = _make_redis_client(delete_return=1)
        key = build_cache_key("optchain", "scrapling", "NSE", "NIFTY")
        count = await client.delete(key)
        assert count == 1
        mock_redis.delete.assert_awaited_once_with("mds:optchain:scrapling:NSE:NIFTY:_:_:_")

    @pytest.mark.asyncio
    async def test_ttl_remaining_with_built_key(self) -> None:
        client, mock_redis = _make_redis_client(ttl_return=12)
        key = build_cache_key("instrument_master")
        remaining = await client.ttl_remaining(key)
        assert remaining == 12
        mock_redis.ttl.assert_awaited_once_with("mds:instrument_master:_:_:_:_:_:_")
