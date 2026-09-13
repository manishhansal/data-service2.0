"""
tests/mocks/provider_mocks.py
==============================

Reusable provider mock classes and factory functions for DATA-SERVICE 2.0
integration and unit tests.

Design goals
------------
- Zero external I/O — all mocks return pre-configured in-memory responses.
- Factory functions produce minimal-but-valid dicts that pass through the
  Normaliser without rejection.
- Every mock exposes the same async interface as the real adapter it replaces,
  so tests can substitute the mock via dependency injection or ``unittest.mock.patch``
  without altering test logic.
- ``MockRedis`` faithfully reproduces the async call semantics of the real
  ``redis.asyncio.client.Redis`` for the subset of commands used by the Platform
  (GET, SET, SETEX, DELETE, XADD, TTL, EXPIRE).

Null-semantic invariants (Requirements 3.3, 6.2–6.6)
------------------------------------------------------
Factory functions follow the same strict null semantics as the Normaliser:
  - ``oi``          : None (+ oiMissing: True) for equities; never populated
                      from ``tradedValue``
  - ``iv``          : None when not provided; zero is NOT a substitute
  - Greeks          : None when not provided; placeholder zeros are prohibited
  - ``bid`` / ``ask``: None when not provided
  - ``volume``      : 0 + ``volumeUnavailable: True`` when the source doesn't
                      supply a value
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Reference timestamps (fixed so tests are deterministic)
# ---------------------------------------------------------------------------

# 2024-01-15 09:30:00 UTC in milliseconds — a realistic NSE REGULAR-session
# instant
_BASE_EVENT_TIME_MS: int = 1705300200_000

# One minute of milliseconds (used for candle offsets)
_ONE_MIN_MS: int = 60_000


# ===========================================================================
# Factory functions
# ===========================================================================


def make_valid_tick(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid NSE tick dict compatible with ``Normaliser.normalise_quote()``.

    The dict contains every field expected by the normaliser so that the tick
    passes through the validation pipeline without generating a ``DataIncident``.

    Null semantics (Requirements 3.3, 6.2):
    - ``oi`` is ``None`` and ``oiMissing`` is ``True`` (equity — no OI).
    - ``bid`` / ``ask`` are ``None`` (not provided by source).

    Args:
        **overrides: Key-value pairs that replace the corresponding defaults.

    Returns:
        A dict ready to pass to ``Normaliser.normalise_quote(raw, provider)``.
    """
    tick: dict[str, Any] = {
        "instrumentId": "NSE:RELIANCE:EQ",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "ltp": 2850.75,
        "open": 2820.00,
        "high": 2870.50,
        "low": 2810.25,
        "prevClose": 2830.00,
        "change": 20.75,
        "changePct": 0.733,
        "volume": 1_234_567,
        "tradedValue": 3_521_987_432.50,
        # OI semantics: equity has no OI — never populated from tradedValue
        "oi": None,
        "oiMissing": True,
        "totalBuyQty": 450_000,
        "totalSellQty": 380_000,
        "upperCircuit": 3113.00,
        "lowerCircuit": 2547.00,
        "weekHigh52": 3100.00,
        "weekLow52": 2100.00,
        "lastTradeTime": _BASE_EVENT_TIME_MS,
        # bid/ask absent from this provider feed — must be None, not 0
        "bid": None,
        "ask": None,
        "eventTimeMs": _BASE_EVENT_TIME_MS,
        "receivedAtMs": _BASE_EVENT_TIME_MS + 150,
        "source": "angel_one",
    }
    tick.update(overrides)
    return tick


def make_valid_ohlcv_candle(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid OHLCV candle dict compatible with ``Normaliser.normalise_ohlcv()``.

    All OHLCV invariants hold by default:
    - ``high >= max(open, close)``
    - ``low  <= min(open, close)``
    - All prices ``> 0``
    - ``volume >= 0``

    Args:
        **overrides: Key-value pairs that replace corresponding defaults.

    Returns:
        A dict ready to pass to ``Normaliser.normalise_ohlcv(raw, provider)``.
    """
    candle: dict[str, Any] = {
        "instrumentId": "NSE:RELIANCE:EQ",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "interval": "1d",
        # time in UTC epoch seconds (candle open time)
        "time": int(_BASE_EVENT_TIME_MS / 1000),
        "open": 2820.00,
        "high": 2870.50,
        "low": 2810.25,
        "close": 2850.75,
        "volume": 1_234_567,
        # OI absent for equity — None + flag
        "oi": None,
        "oiMissing": True,
        "provider": "angel_one",
        "sourceType": "BROKER_AUTHENTICATED",
    }
    candle.update(overrides)
    return candle


def make_option_chain_row(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid option chain row dict.

    Suitable for constructing ``OptionChainRecord`` objects and for passing
    through ``QualityEngine.validate_option_chain_record()``.

    Null semantics (Requirements 6.4–6.6, 3.7):
    - ``iv``  : provided (realistic positive value)
    - ``bid`` / ``ask``: provided (realistic spread; ask > bid — no crossed market)
    - ``oi``  : provided (realistic positive integer)
    - Greeks  : explicitly ``None`` — not provided by this data source; zero
                is NOT a valid substitute

    Args:
        **overrides: Key-value pairs that replace corresponding defaults.

    Returns:
        A dict ready for ``OptionChainRecord(**row)`` or direct normaliser use.
    """
    row: dict[str, Any] = {
        "symbol": "NIFTY",
        "expiry": "2024-01-25",
        "strike": 21800.0,
        "optionType": "CE",
        "ltp": 125.50,
        "bid": 124.80,
        "ask": 126.20,
        "oi": 542_600,
        "oiChange": 12_400,
        "volume": 98_750,
        "tradedValue": 12_395_812.50,
        # IV provided by this source — realistic value; zero would be invalid
        "iv": 14.35,
        # Greeks absent — must be None, not 0 (Requirements 6.5)
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "rho": None,
        # Completeness flags
        "oiMissing": False,
        "oiChangeMissing": False,
        "volumeMissing": False,
        "ivMissing": False,
        "timestamp": _BASE_EVENT_TIME_MS,
    }
    row.update(overrides)
    return row


def make_deribit_ticker(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid Deribit ticker dict.

    Matches the shape returned by ``DeribitClient.get_ticker()`` after the
    client unwraps the JSON-RPC ``"result"`` envelope.

    Null semantics (Requirement 14.5):
    - ``mark_iv`` : provided (realistic positive value)
    - ``open_interest``: provided
    - ``underlying_price``: provided

    Args:
        **overrides: Key-value pairs that replace corresponding defaults.

    Returns:
        A dict matching the Deribit ticker ``result`` payload shape.
    """
    ticker: dict[str, Any] = {
        "instrument_name": "BTC-26JAN24-42000-C",
        "mark_price": 0.0532,
        "mark_iv": 58.45,
        "open_interest": 1_250.0,
        "volume": 312.5,
        "volume_usd": 13_168_750.0,
        "underlying_price": 41_850.00,
        "last": 0.0528,
        "bid": 0.0530,
        "ask": 0.0535,
        "best_bid_price": 0.0530,
        "best_ask_price": 0.0535,
        "delta": 0.4821,
        "gamma": 0.000_021,
        "theta": -12.35,
        "vega": 28.70,
        "timestamp": _BASE_EVENT_TIME_MS,
    }
    ticker.update(overrides)
    return ticker


# ---------------------------------------------------------------------------
# Sample data lists (5-element collections for use in multiple tests)
# ---------------------------------------------------------------------------

#: Five realistic Binance kline dicts as returned by ``BinanceClient.get_klines()``.
#: Each entry is already in the normalised dict form (post ``_normalise_kline``),
#: so they can be fed directly to ``BinanceOHLCVNormaliser.normalise_batch()``.
SAMPLE_OHLCV_KLINES: list[dict[str, Any]] = [
    {
        "time": 1_705_296_000_000,  # 2024-01-15 08:00:00 UTC ms
        "open": 42_800.00,
        "high": 43_150.00,
        "low": 42_650.00,
        "close": 43_020.50,
        "volume": 1_245.678,
        "closeTime": 1_705_296_059_999,
    },
    {
        "time": 1_705_296_060_000,  # +1 min
        "open": 43_020.50,
        "high": 43_200.00,
        "low": 42_980.00,
        "close": 43_150.00,
        "volume": 987.123,
        "closeTime": 1_705_296_119_999,
    },
    {
        "time": 1_705_296_120_000,  # +2 min
        "open": 43_150.00,
        "high": 43_350.00,
        "low": 43_100.00,
        "close": 43_280.75,
        "volume": 1_102.456,
        "closeTime": 1_705_296_179_999,
    },
    {
        "time": 1_705_296_180_000,  # +3 min
        "open": 43_280.75,
        "high": 43_400.00,
        "low": 43_200.00,
        "close": 43_310.25,
        "volume": 834.789,
        "closeTime": 1_705_296_239_999,
    },
    {
        "time": 1_705_296_240_000,  # +4 min
        "open": 43_310.25,
        "high": 43_500.00,
        "low": 43_250.00,
        "close": 43_460.00,
        "volume": 1_567.321,
        "closeTime": 1_705_296_299_999,
    },
]

#: Five realistic NSE tick dicts as returned by the Angel One / Scrapling feed.
#: Each entry is valid for ``Normaliser.normalise_quote()``.
SAMPLE_INDIA_TICKS: list[dict[str, Any]] = [
    make_valid_tick(
        symbol="RELIANCE",
        instrumentId="NSE:RELIANCE:EQ",
        ltp=2850.75,
        eventTimeMs=_BASE_EVENT_TIME_MS,
    ),
    make_valid_tick(
        symbol="INFY",
        instrumentId="NSE:INFY:EQ",
        ltp=1_485.20,
        open=1_470.00,
        high=1_492.50,
        low=1_465.00,
        prevClose=1_480.00,
        change=5.20,
        changePct=0.351,
        volume=2_345_678,
        tradedValue=3_484_527_736.0,
        eventTimeMs=_BASE_EVENT_TIME_MS + _ONE_MIN_MS,
    ),
    make_valid_tick(
        symbol="HDFCBANK",
        instrumentId="NSE:HDFCBANK:EQ",
        ltp=1_635.80,
        open=1_620.00,
        high=1_642.00,
        low=1_615.50,
        prevClose=1_628.00,
        change=7.80,
        changePct=0.479,
        volume=3_456_789,
        tradedValue=5_649_513_462.0,
        eventTimeMs=_BASE_EVENT_TIME_MS + 2 * _ONE_MIN_MS,
    ),
    make_valid_tick(
        symbol="TCS",
        instrumentId="NSE:TCS:EQ",
        ltp=3_920.40,
        open=3_900.00,
        high=3_935.00,
        low=3_892.00,
        prevClose=3_905.00,
        change=15.40,
        changePct=0.394,
        volume=876_543,
        tradedValue=3_435_820_572.0,
        eventTimeMs=_BASE_EVENT_TIME_MS + 3 * _ONE_MIN_MS,
    ),
    make_valid_tick(
        symbol="NIFTY",
        instrumentId="NSE:NIFTY:IDX",
        ltp=21_850.35,
        open=21_750.00,
        high=21_890.00,
        low=21_720.00,
        prevClose=21_800.00,
        change=50.35,
        changePct=0.231,
        volume=0,
        tradedValue=0.0,
        # NIFTY is an index — OI is null, not 0
        oi=None,
        oiMissing=True,
        eventTimeMs=_BASE_EVENT_TIME_MS + 4 * _ONE_MIN_MS,
    ),
]


# ===========================================================================
# MockAngelOneProvider
# ===========================================================================


class MockAngelOneProvider:
    """Mock replacement for ``AngelOneAdapter``.

    Pre-configures responses for the three most common calls used across
    integration tests.  All async methods return the configured data
    immediately without any network I/O.

    Usage::

        mock = MockAngelOneProvider()
        # override a single response
        mock.live_quote_response = make_valid_tick(symbol="INFY", ltp=1500.0)

        result = await mock.fetch_live_quote("NSE:INFY:EQ")
        assert result["symbol"] == "INFY"

    Attributes:
        live_quote_response: dict returned by ``fetch_live_quote()``.
        option_chain_response: list returned by ``fetch_option_chain()``.
        historical_ohlcv_response: list returned by ``fetch_historical_ohlcv()``.
        should_raise: if set to an Exception instance, all methods raise it.
    """

    def __init__(self) -> None:
        self.live_quote_response: dict[str, Any] = make_valid_tick()
        self.option_chain_response: list[dict[str, Any]] = [
            make_option_chain_row(),
            make_option_chain_row(strike=21900.0, optionType="PE", ltp=110.25,
                                  bid=109.50, ask=111.00, iv=15.20),
        ]
        self.historical_ohlcv_response: list[dict[str, Any]] = [
            make_valid_ohlcv_candle(
                time=int(_BASE_EVENT_TIME_MS / 1000) - i * 86_400,
                open=2820.00 + i * 5,
                high=2870.50 + i * 5,
                low=2810.25 + i * 5,
                close=2850.75 + i * 5,
            )
            for i in range(5)
        ]
        self.should_raise: Optional[Exception] = None

    async def fetch_live_quote(self, instrument_id: str) -> dict[str, Any]:
        """Return the pre-configured live quote dict."""
        if self.should_raise is not None:
            raise self.should_raise
        return {**self.live_quote_response, "instrumentId": instrument_id}

    async def fetch_option_chain(
        self,
        underlying: str,
        expiry: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return the pre-configured option chain rows."""
        if self.should_raise is not None:
            raise self.should_raise
        return list(self.option_chain_response)

    async def fetch_historical_ohlcv(
        self,
        symbol: str,
        exchange: str,
        interval: str,
        from_date: Any,
        to_date: Any,
    ) -> list[dict[str, Any]]:
        """Return the pre-configured OHLCV candle list."""
        if self.should_raise is not None:
            raise self.should_raise
        return list(self.historical_ohlcv_response)

    async def fetch_pcr(self) -> dict[str, Any]:
        """Return a minimal PCR response."""
        if self.should_raise is not None:
            raise self.should_raise
        return {"pcr": 0.92, "source": "angel_one"}

    async def fetch_oi_buildup(self) -> list[dict[str, Any]]:
        """Return a minimal OI buildup list."""
        if self.should_raise is not None:
            raise self.should_raise
        return [
            {"symbol": "RELIANCE", "oiChange": 12_400, "direction": "long"},
            {"symbol": "INFY",     "oiChange": -5_200, "direction": "short"},
        ]

    async def fetch_gainers_losers(self) -> dict[str, Any]:
        """Return a minimal gainers/losers dict."""
        if self.should_raise is not None:
            raise self.should_raise
        return {
            "gainers": [{"symbol": "RELIANCE", "changePct": 1.25}],
            "losers":  [{"symbol": "INFY",     "changePct": -0.85}],
        }

    async def close(self) -> None:
        """No-op teardown."""


# ===========================================================================
# MockBinanceClient
# ===========================================================================


class MockBinanceClient:
    """Mock replacement for ``BinanceClient``.

    Pre-configures responses for the three most common REST calls.  No network
    I/O is performed.

    Usage::

        mock = MockBinanceClient()
        klines = await mock.get_klines("BTCUSDT", "1m", limit=5)
        assert len(klines) == 5
    """

    def __init__(self) -> None:
        self.klines_response: list[dict[str, Any]] = list(SAMPLE_OHLCV_KLINES)
        self.ticker_price_response: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "price": "43460.00",
        }
        self.stats_24hr_response: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "priceChange": "660.00",
            "priceChangePercent": "1.54",
            "weightedAvgPrice": "43150.25",
            "openPrice": "42800.00",
            "highPrice": "43500.00",
            "lowPrice": "42650.00",
            "lastPrice": "43460.00",
            "volume": "28452.123",
            "quoteVolume": "1228546789.50",
            "openTime": 1_705_209_600_000,
            "closeTime": 1_705_296_059_999,
            "count": 354_628,
        }
        self.should_raise: Optional[Exception] = None

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Return the pre-configured kline list."""
        if self.should_raise is not None:
            raise self.should_raise
        return list(self.klines_response)

    async def get_ticker_price(self, symbol: str) -> dict[str, Any]:
        """Return the pre-configured ticker price dict."""
        if self.should_raise is not None:
            raise self.should_raise
        return {**self.ticker_price_response, "symbol": symbol.upper()}

    async def get_24hr_stats(self, symbol: str) -> dict[str, Any]:
        """Return the pre-configured 24-hour statistics dict."""
        if self.should_raise is not None:
            raise self.should_raise
        return {**self.stats_24hr_response, "symbol": symbol.upper()}

    async def get_futures_mark_price(self, symbol: str) -> dict[str, Any]:
        """Return a minimal perpetual futures mark price dict."""
        if self.should_raise is not None:
            raise self.should_raise
        return {
            "symbol": symbol.upper(),
            "markPrice": "43460.00",
            "indexPrice": "43455.25",
            "estimatedSettlePrice": "43458.00",
            "lastFundingRate": "0.00010",
            "nextFundingTime": _BASE_EVENT_TIME_MS + 28_800_000,
            "interestRate": "0.00010",
            "time": _BASE_EVENT_TIME_MS,
        }

    async def get_futures_open_interest(self, symbol: str) -> dict[str, Any]:
        """Return a minimal open interest dict."""
        if self.should_raise is not None:
            raise self.should_raise
        return {
            "symbol": symbol.upper(),
            "openInterest": "12345.678",
            "time": _BASE_EVENT_TIME_MS,
        }

    async def get_long_short_ratio(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Return a minimal long/short ratio list."""
        if self.should_raise is not None:
            raise self.should_raise
        return [
            {
                "symbol": symbol.upper(),
                "longShortRatio": "1.2345",
                "longAccount": "0.5523",
                "shortAccount": "0.4477",
                "timestamp": _BASE_EVENT_TIME_MS - i * 300_000,
            }
            for i in range(min(limit, 5))
        ]

    async def close(self) -> None:
        """No-op teardown."""

    async def __aenter__(self) -> "MockBinanceClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


# ===========================================================================
# MockDeribitClient
# ===========================================================================


class MockDeribitClient:
    """Mock replacement for ``DeribitClient``.

    Pre-configures responses for instruments, ticker, and index price calls.

    Usage::

        mock = MockDeribitClient()
        instruments = await mock.get_instruments("BTC", kind="option")
        assert instruments[0]["instrument_name"].startswith("BTC-")
    """

    def __init__(self) -> None:
        self.instruments_response: list[dict[str, Any]] = [
            {
                "instrument_name": "BTC-26JAN24-42000-C",
                "kind": "option",
                "base_currency": "BTC",
                "quote_currency": "USD",
                "expiration_timestamp": 1706256000000,  # 2024-01-26 08:00 UTC ms
                "strike": 42000.0,
                "option_type": "call",
                "is_active": True,
                "settlement_currency": "BTC",
                "tick_size": 0.0001,
                "contract_size": 1.0,
            },
            {
                "instrument_name": "BTC-26JAN24-42000-P",
                "kind": "option",
                "base_currency": "BTC",
                "quote_currency": "USD",
                "expiration_timestamp": 1706256000000,
                "strike": 42000.0,
                "option_type": "put",
                "is_active": True,
                "settlement_currency": "BTC",
                "tick_size": 0.0001,
                "contract_size": 1.0,
            },
        ]
        self.ticker_response: dict[str, Any] = make_deribit_ticker()
        self.index_price_response: dict[str, Any] = {
            "index_name": "btc_usd",
            "index_price": 41_850.00,
        }
        self.should_raise: Optional[Exception] = None

    async def get_instruments(
        self,
        currency: str,
        kind: str = "option",
        expired: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the pre-configured instruments list."""
        if self.should_raise is not None:
            raise self.should_raise
        return list(self.instruments_response)

    async def get_ticker(self, instrument_name: str) -> dict[str, Any]:
        """Return the pre-configured ticker dict for the given instrument."""
        if self.should_raise is not None:
            raise self.should_raise
        return {**self.ticker_response, "instrument_name": instrument_name}

    async def get_index_price(self, index_name: str) -> dict[str, Any]:
        """Return the pre-configured index price dict."""
        if self.should_raise is not None:
            raise self.should_raise
        currency = index_name.split("_")[0].upper()
        return {
            "index_name": index_name,
            "index_price": self.index_price_response["index_price"],
            "estimated_delivery_price": self.index_price_response["index_price"],
        }

    async def get_order_book(self, instrument_name: str) -> dict[str, Any]:
        """Return a minimal order book dict."""
        if self.should_raise is not None:
            raise self.should_raise
        return {
            "instrument_name": instrument_name,
            "bids": [[0.0530, 10.0], [0.0525, 25.0]],
            "asks": [[0.0535, 8.0],  [0.0540, 20.0]],
            "timestamp": _BASE_EVENT_TIME_MS,
        }

    async def close(self) -> None:
        """No-op teardown."""

    async def __aenter__(self) -> "MockDeribitClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


# ===========================================================================
# MockRedis
# ===========================================================================


class MockRedis:
    """Minimal in-memory mock for ``redis.asyncio.client.Redis``.

    Implements the subset of commands used by the Platform:
    - ``get`` / ``set`` / ``setex`` / ``delete``
    - ``xadd`` (Event Bus)
    - ``ttl`` / ``expire``
    - ``eval`` (Lua — returns 1 by default, used by token-bucket rate limiter)
    - ``exists``
    - ``lpush`` / ``lrange`` (lineage store helpers)
    - ``hset`` / ``hget`` / ``hmget`` / ``hgetall`` (circuit-breaker state)
    - ``incr``
    - ``pipeline`` (returns self — flushes immediately on execute())

    All methods are **async**.  Internal storage uses plain Python dicts so
    tests can inspect or mutate state directly via ``mock_redis._store``.

    Note on TTL: this mock does **not** auto-expire keys.  Tests that need
    expiry behaviour should manipulate ``_store`` and ``_ttl`` directly.

    Usage::

        redis = MockRedis()
        await redis.set("mds:quote:_:NSE:RELIANCE:_:_:_", b"data")
        val = await redis.get("mds:quote:_:NSE:RELIANCE:_:_:_")
        assert val == b"data"
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._ttl:   dict[str, int] = {}      # TTL in seconds (recorded, not enforced)
        self._lists: dict[str, list] = {}      # for lpush/lrange
        self._hashes: dict[str, dict] = {}    # for hset/hget
        self._stream_entries: dict[str, list[dict]] = {}  # for xadd

    # ------------------------------------------------------------------ #
    # String operations
    # ------------------------------------------------------------------ #

    async def get(self, key: str) -> Optional[bytes]:
        value = self._store.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            return value.encode()
        if isinstance(value, bytes):
            return value
        return str(value).encode()

    async def set(self, key: str, value: Any, ex: Optional[int] = None) -> bool:
        self._store[key] = value
        if ex is not None:
            self._ttl[key] = ex
        return True

    async def setex(self, key: str, seconds: int, value: Any) -> bool:
        self._store[key] = value
        self._ttl[key] = seconds
        return True

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                self._ttl.pop(key, None)
                deleted += 1
        return deleted

    async def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self._store)

    async def ttl(self, key: str) -> int:
        """Return the stored TTL in seconds, or -2 if the key does not exist."""
        if key not in self._store:
            return -2
        return self._ttl.get(key, -1)

    async def expire(self, key: str, seconds: int) -> bool:
        if key not in self._store:
            return False
        self._ttl[key] = seconds
        return True

    async def incr(self, key: str) -> int:
        current = int(self._store.get(key, 0))
        self._store[key] = current + 1
        return current + 1

    # ------------------------------------------------------------------ #
    # Hash operations (circuit breaker state)
    # ------------------------------------------------------------------ #

    async def hset(self, name: str, mapping: Optional[dict] = None, **fields: Any) -> int:
        if name not in self._hashes:
            self._hashes[name] = {}
        data = mapping or {}
        data.update(fields)
        self._hashes[name].update(data)
        return len(data)

    async def hget(self, name: str, key: str) -> Optional[bytes]:
        value = self._hashes.get(name, {}).get(key)
        if value is None:
            return None
        return str(value).encode() if not isinstance(value, bytes) else value

    async def hmget(self, name: str, keys: list[str]) -> list[Optional[bytes]]:
        h = self._hashes.get(name, {})
        result = []
        for k in keys:
            v = h.get(k)
            if v is None:
                result.append(None)
            else:
                result.append(str(v).encode() if not isinstance(v, bytes) else v)
        return result

    async def hgetall(self, name: str) -> dict[bytes, bytes]:
        return {
            k.encode() if isinstance(k, str) else k: v.encode() if isinstance(v, str) else v
            for k, v in self._hashes.get(name, {}).items()
        }

    # ------------------------------------------------------------------ #
    # List operations (lineage store)
    # ------------------------------------------------------------------ #

    async def lpush(self, key: str, *values: Any) -> int:
        if key not in self._lists:
            self._lists[key] = []
        for v in reversed(values):
            self._lists[key].insert(0, v)
        return len(self._lists[key])

    async def lrange(self, key: str, start: int, end: int) -> list:
        lst = self._lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start: end + 1]

    async def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    # ------------------------------------------------------------------ #
    # Redis Streams (Event Bus)
    # ------------------------------------------------------------------ #

    async def xadd(
        self,
        name: str,
        fields: dict[str, Any],
        id: str = "*",
        maxlen: Optional[int] = None,
        approximate: bool = True,
    ) -> bytes:
        """Append an entry to the in-memory stream and return a synthetic message ID."""
        if name not in self._stream_entries:
            self._stream_entries[name] = []
        entry = {"id": id, "fields": fields}
        self._stream_entries[name].append(entry)
        # Return a Redislike message ID as bytes
        seq = len(self._stream_entries[name])
        msg_id = f"{_BASE_EVENT_TIME_MS}-{seq}"
        return msg_id.encode()

    async def xlen(self, name: str) -> int:
        return len(self._stream_entries.get(name, []))

    # ------------------------------------------------------------------ #
    # Lua eval (token-bucket rate limiter)
    # ------------------------------------------------------------------ #

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        """Return 1 (token available) by default; override in tests as needed."""
        return 1

    # ------------------------------------------------------------------ #
    # Pipeline (no-op passthrough)
    # ------------------------------------------------------------------ #

    def pipeline(self, transaction: bool = True) -> "_MockPipeline":
        """Return a pipeline object that executes commands immediately on execute()."""
        return _MockPipeline(self)

    # ------------------------------------------------------------------ #
    # Connection lifecycle (no-op)
    # ------------------------------------------------------------------ #

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> "MockRedis":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass


class _MockPipeline:
    """Minimal pipeline that accumulates commands and runs them on ``execute()``."""

    def __init__(self, redis: MockRedis) -> None:
        self._redis = redis
        self._commands: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str) -> Any:
        """Record any command call for deferred execution."""
        async def _record(*args: Any, **kwargs: Any) -> "_MockPipeline":
            self._commands.append((name, args, kwargs))
            return self
        return _record

    async def execute(self) -> list[Any]:
        """Execute all recorded commands and return their results."""
        results = []
        for cmd_name, args, kwargs in self._commands:
            method = getattr(self._redis, cmd_name)
            result = await method(*args, **kwargs)
            results.append(result)
        self._commands.clear()
        return results
