"""
src/cache/redis_client.py

Redis async client and L2 cache primitives for DATA-SERVICE 2.0.

Implements:
  - ``create_redis_pool``    — async Redis client with connection pool
  - ``build_cache_key``      — canonical ``mds:`` key-namespace helper
  - ``RedisClient``          — async primitives: get, set_with_ttl, delete, ttl_remaining
  - TTL constants            — all L2 TTLs from the design (Requirements 9.2, 9.3)

Key namespace pattern (design §Multi-Level Cache / L2 Key Namespace):
    mds:{dataType}:{provider}:{exchange}:{symbol}:{interval}:{from}:{to}

Fields that are not applicable for a given request are substituted with ``_``.

Requirements: 9.2, 9.3
"""

from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# L2 TTL constants (seconds) — sourced from design §Multi-Level Cache / TTL Table
# All tunable via environment / cache_manager overrides; these are the defaults.
# ---------------------------------------------------------------------------

#: Single live quote TTL — 3 seconds
TTL_LIVE_QUOTE: int = 3

#: Batch live quotes TTL — 3 seconds
TTL_BATCH_QUOTES: int = 3

#: Intraday OHLCV candles TTL — 30 seconds
TTL_INTRADAY_CANDLES: int = 30

#: Daily+ OHLCV candles TTL — 4 hours (14 400 seconds)
TTL_DAILY_CANDLES: int = 14_400

#: Option chain snapshot TTL — 15 seconds
TTL_OPTION_CHAIN: int = 15

#: Option Greeks snapshot TTL — 15 seconds
TTL_OPTION_GREEKS: int = 15

#: Market depth TTL — 2 seconds (high-frequency depth data)
TTL_MARKET_DEPTH: int = 2

#: Reconciliation result TTL — 3 seconds
TTL_RECONCILIATION: int = 3

#: Instrument master TTL — 12 hours (43 200 seconds)
TTL_INSTRUMENT_MASTER: int = 43_200

#: Provider health TTL — 5 seconds
TTL_PROVIDER_HEALTH: int = 5

#: Closing auction snapshot TTL — 10 seconds
TTL_CLOSING_AUCTION: int = 10

#: Exchange status TTL — 30 seconds
TTL_EXCHANGE_STATUS: int = 30

#: Market holidays TTL — 24 hours
TTL_MARKET_HOLIDAYS: int = 86_400

# ---------------------------------------------------------------------------
# Key namespace
# ---------------------------------------------------------------------------

#: Prefix for every L2 Redis key (design §L2 Key Namespace).
_KEY_PREFIX = "mds"

#: Placeholder substituted for inapplicable fields.
_FIELD_PLACEHOLDER = "_"


def build_cache_key(
    data_type: str,
    provider: str | None = None,
    exchange: str | None = None,
    symbol: str | None = None,
    interval: str | None = None,
    from_ts: int | str | None = None,
    to_ts: int | str | None = None,
) -> str:
    """Build a canonical ``mds:`` namespaced L2 cache key.

    Pattern::

        mds:{dataType}:{provider}:{exchange}:{symbol}:{interval}:{from}:{to}

    Any field that is ``None`` or an empty string is replaced with ``_``.

    Args:
        data_type: Logical data type, e.g. ``"quote"``, ``"candle"``,
                   ``"optchain"``, ``"instrument_master"``.
        provider:  Provider identifier, e.g. ``"angel_one"``, ``"upstox"``.
        exchange:  Exchange code, e.g. ``"NSE"``, ``"NFO"``.
        symbol:    Instrument symbol, e.g. ``"NIFTY"``, ``"RELIANCE"``.
        interval:  Candle interval string, e.g. ``"1m"``, ``"1d"``.
        from_ts:   Start timestamp (epoch ms or ISO-8601 string).
        to_ts:     End timestamp (epoch ms or ISO-8601 string).

    Returns:
        A Redis key string such as
        ``"mds:quote:angel_one:NSE:NIFTY:_:_:_"``.

    Examples::

        >>> build_cache_key("quote", "angel_one", "NSE", "NIFTY")
        'mds:quote:angel_one:NSE:NIFTY:_:_:_'

        >>> build_cache_key("candle", "upstox", "NSE", "RELIANCE", "1m",
        ...                 1705300000000, 1705386400000)
        'mds:candle:upstox:NSE:RELIANCE:1m:1705300000000:1705386400000'

        >>> build_cache_key("instrument_master")
        'mds:instrument_master:_:_:_:_:_:_'
    """

    def _or_placeholder(val: str | int | None) -> str:
        if val is None or (isinstance(val, str) and not val.strip()):
            return _FIELD_PLACEHOLDER
        return str(val)

    parts = [
        _KEY_PREFIX,
        _or_placeholder(data_type),
        _or_placeholder(provider),
        _or_placeholder(exchange),
        _or_placeholder(symbol),
        _or_placeholder(interval),
        _or_placeholder(from_ts),
        _or_placeholder(to_ts),
    ]
    return ":".join(parts)


# ---------------------------------------------------------------------------
# Connection pool factory
# ---------------------------------------------------------------------------


async def create_redis_pool(redis_url: str) -> "Redis[str]":
    """Create and verify an async Redis connection pool.

    Configures a ``redis.asyncio.Redis`` client backed by a connection pool
    with:
    - UTF-8 encoding + decoded responses (strings, not bytes)
    - 5-second connect and command socket timeouts
    - Automatic retry on timeout
    - Background health-check every 30 seconds

    A ``PING`` is issued immediately to verify connectivity.  If Redis is
    unreachable the raised ``RedisConnectionError`` propagates to the caller
    so the server can enter degraded mode gracefully (Requirement 20.2).

    Args:
        redis_url: Full Redis connection URL, e.g. ``redis://localhost:6379/0``.

    Returns:
        A connected ``redis.asyncio.Redis[str]`` client.

    Raises:
        redis.exceptions.ConnectionError: If the initial PING fails.
        redis.exceptions.RedisError: For any other Redis protocol error on startup.
    """
    client: "Redis[str]" = aioredis.from_url(
        redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=True,
        health_check_interval=30,
        max_connections=50,
    )
    # Verify connectivity — raises if Redis is unreachable.
    await client.ping()
    logger.info("redis_connected", extra={"redis_url": redis_url})
    return client


# ---------------------------------------------------------------------------
# Async primitive wrappers
# ---------------------------------------------------------------------------


class RedisUnavailableError(RuntimeError):
    """Raised when the Redis client is not available (degraded mode)."""


class RedisClient:
    """Thin async wrapper around ``redis.asyncio.Redis`` providing the four
    L2 cache primitives used throughout the platform.

    All methods accept an optional ``client`` override so callers can inject
    a mock during testing without patching module-level state.

    Design decisions:
    - All exceptions from ``redis-py`` are caught and re-raised as
      ``RedisUnavailableError`` with a structured warning log, satisfying
      Requirement 9.7: *"When L2 Redis is unavailable, fall back to direct
      provider calls … do not return error to consumer."*  The caller decides
      whether to fall back.
    - ``get`` returns ``None`` on cache miss (Redis returns ``None`` for
      absent keys); callers must treat ``None`` as a miss.
    - ``ttl_remaining`` returns ``-2`` when the key does not exist and ``-1``
      when the key exists but has no TTL, matching Redis semantics.
    """

    def __init__(self, client: "Redis[str]") -> None:
        self._client: "Redis[str]" = client

    # ------------------------------------------------------------------
    # get
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Optional[str]:
        """Fetch the value stored at *key*.

        Args:
            key: The exact Redis key to look up.

        Returns:
            The stored string value, or ``None`` if the key does not exist
            or has expired.

        Raises:
            RedisUnavailableError: If the Redis connection fails.
        """
        try:
            return await self._client.get(key)
        except RedisError as exc:
            logger.warning(
                "cache_l2_unavailable",
                extra={"operation": "get", "key": key, "error": str(exc)},
            )
            raise RedisUnavailableError(f"Redis GET failed for key={key!r}: {exc}") from exc

    # ------------------------------------------------------------------
    # set_with_ttl
    # ------------------------------------------------------------------

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store *value* at *key* with an absolute TTL.

        Args:
            key:         The Redis key.
            value:       String value to store (callers serialise to JSON etc.).
            ttl_seconds: Time-to-live in seconds (must be > 0).

        Raises:
            ValueError:            If ``ttl_seconds`` is not a positive integer.
            RedisUnavailableError: If the Redis connection fails.
        """
        if ttl_seconds <= 0:
            raise ValueError(
                f"ttl_seconds must be a positive integer; got {ttl_seconds!r}"
            )
        try:
            await self._client.set(key, value, ex=ttl_seconds)
        except RedisError as exc:
            logger.warning(
                "cache_l2_unavailable",
                extra={
                    "operation": "set_with_ttl",
                    "key": key,
                    "ttl_seconds": ttl_seconds,
                    "error": str(exc),
                },
            )
            raise RedisUnavailableError(
                f"Redis SET failed for key={key!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete(self, key: str) -> int:
        """Delete *key* from Redis.

        Args:
            key: The Redis key to remove.

        Returns:
            The number of keys deleted (0 if the key did not exist, 1 if it did).

        Raises:
            RedisUnavailableError: If the Redis connection fails.
        """
        try:
            result: int = await self._client.delete(key)
            return result
        except RedisError as exc:
            logger.warning(
                "cache_l2_unavailable",
                extra={"operation": "delete", "key": key, "error": str(exc)},
            )
            raise RedisUnavailableError(
                f"Redis DELETE failed for key={key!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # ttl_remaining
    # ------------------------------------------------------------------

    async def ttl_remaining(self, key: str) -> int:
        """Return the remaining TTL for *key* in seconds.

        Follows standard Redis TTL semantics:
        - Positive integer: remaining seconds
        - ``-1``: key exists but has no TTL (persistent)
        - ``-2``: key does not exist

        Args:
            key: The Redis key to inspect.

        Returns:
            Remaining TTL in seconds, or ``-1`` / ``-2`` per Redis semantics.

        Raises:
            RedisUnavailableError: If the Redis connection fails.
        """
        try:
            result: int = await self._client.ttl(key)
            return result
        except RedisError as exc:
            logger.warning(
                "cache_l2_unavailable",
                extra={"operation": "ttl_remaining", "key": key, "error": str(exc)},
            )
            raise RedisUnavailableError(
                f"Redis TTL failed for key={key!r}: {exc}"
            ) from exc
