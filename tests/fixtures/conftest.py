"""
tests/fixtures/conftest.py
===========================

Shared pytest fixtures exposing provider mock objects and sample data.

All fixtures in this module are automatically available to any test that is
collected from ``tests/`` or its sub-directories.  No explicit import is
needed in test files — pytest's fixture-discovery mechanism handles it.

Fixture scope summary
---------------------
+---------------------------+----------+------------------------------------------+
| Fixture name              | Scope    | What it provides                         |
+---------------------------+----------+------------------------------------------+
| angel_one_provider        | function | Fresh MockAngelOneProvider per test      |
| binance_client            | function | Fresh MockBinanceClient per test         |
| deribit_client            | function | Fresh MockDeribitClient per test         |
| mock_redis                | function | Fresh MockRedis per test                 |
| valid_tick                | function | Single valid NSE tick dict               |
| valid_ohlcv_candle        | function | Single valid OHLCV candle dict           |
| option_chain_row          | function | Single valid option chain row dict       |
| deribit_ticker            | function | Single valid Deribit ticker dict         |
| sample_ohlcv_klines       | session  | List of 5 Binance kline dicts (read-only)|
| sample_india_ticks        | session  | List of 5 NSE tick dicts (read-only)     |
+---------------------------+----------+------------------------------------------+

Session-scoped fixtures (``sample_ohlcv_klines``, ``sample_india_ticks``) are
read-only constants — tests must NOT mutate them.  All other fixtures are
function-scoped so each test receives a fresh, isolated instance.

Usage::

    async def test_angel_one_quote(angel_one_provider):
        result = await angel_one_provider.fetch_live_quote("NSE:RELIANCE:EQ")
        assert result["ltp"] == 2850.75

    def test_tick_shape(valid_tick):
        assert "ltp" in valid_tick
        assert valid_tick["oi"] is None          # equity — no OI
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.mocks.provider_mocks import (
    SAMPLE_INDIA_TICKS,
    SAMPLE_OHLCV_KLINES,
    MockAngelOneProvider,
    MockBinanceClient,
    MockDeribitClient,
    MockRedis,
    make_deribit_ticker,
    make_option_chain_row,
    make_valid_ohlcv_candle,
    make_valid_tick,
)


# ===========================================================================
# Provider mock fixtures
# ===========================================================================


@pytest.fixture()
def angel_one_provider() -> MockAngelOneProvider:
    """Return a fresh ``MockAngelOneProvider`` with default responses.

    Scope: function — each test receives an isolated instance.

    Example::

        async def test_live_quote(angel_one_provider):
            q = await angel_one_provider.fetch_live_quote("NSE:RELIANCE:EQ")
            assert q["ltp"] > 0
    """
    return MockAngelOneProvider()


@pytest.fixture()
def binance_client() -> MockBinanceClient:
    """Return a fresh ``MockBinanceClient`` with default kline / ticker responses.

    Scope: function — each test receives an isolated instance.

    Example::

        async def test_klines(binance_client):
            klines = await binance_client.get_klines("BTCUSDT", "1m", limit=5)
            assert len(klines) == 5
    """
    return MockBinanceClient()


@pytest.fixture()
def deribit_client() -> MockDeribitClient:
    """Return a fresh ``MockDeribitClient`` with default BTC responses.

    Scope: function — each test receives an isolated instance.

    Example::

        async def test_ticker(deribit_client):
            t = await deribit_client.get_ticker("BTC-26JAN24-42000-C")
            assert t["mark_iv"] == 58.45
    """
    return MockDeribitClient()


@pytest.fixture()
def mock_redis() -> MockRedis:
    """Return a fresh ``MockRedis`` with empty in-memory store.

    Scope: function — each test receives an isolated, empty Redis instance.

    Example::

        async def test_redis_set_get(mock_redis):
            await mock_redis.set("key", b"value")
            assert await mock_redis.get("key") == b"value"
    """
    return MockRedis()


# ===========================================================================
# Data factory fixtures
# ===========================================================================


@pytest.fixture()
def valid_tick() -> dict[str, Any]:
    """Return a single valid NSE tick dict (RELIANCE equity, default values).

    Scope: function.

    The tick has ``oi=None`` and ``oiMissing=True`` (equity — no OI).
    ``bid`` and ``ask`` are ``None`` (not provided by source).
    All fields pass ``Normaliser.normalise_quote()``.
    """
    return make_valid_tick()


@pytest.fixture()
def valid_ohlcv_candle() -> dict[str, Any]:
    """Return a single valid OHLCV candle dict (RELIANCE 1d, default values).

    Scope: function.

    All OHLCV invariants hold: ``high >= max(open, close)``,
    ``low <= min(open, close)``, all prices ``> 0``, ``volume >= 0``.
    """
    return make_valid_ohlcv_candle()


@pytest.fixture()
def option_chain_row() -> dict[str, Any]:
    """Return a single valid option chain row dict (NIFTY 21800 CE).

    Scope: function.

    The row includes a valid ``iv`` value and ``bid < ask`` (no crossed
    market).  Greeks are ``None`` (not provided by this data source — zero
    would violate Requirements 6.5).
    """
    return make_option_chain_row()


@pytest.fixture()
def deribit_ticker() -> dict[str, Any]:
    """Return a single valid Deribit ticker dict (BTC-26JAN24-42000-C).

    Scope: function.

    All optional Deribit fields (``mark_iv``, ``open_interest``,
    ``underlying_price``) are populated with realistic values.  Tests that
    need to verify ``None`` preservation should call
    ``make_deribit_ticker(mark_iv=None, ...)`` directly.
    """
    return make_deribit_ticker()


# ===========================================================================
# Sample collection fixtures (session-scoped — read-only)
# ===========================================================================


@pytest.fixture(scope="session")
def sample_ohlcv_klines() -> list[dict[str, Any]]:
    """Return the ``SAMPLE_OHLCV_KLINES`` constant (5 Binance kline dicts).

    Scope: session — shared across all tests in a run.

    **Do NOT mutate the returned list.**  Each element is a normalised
    Binance kline dict ready for ``BinanceOHLCVNormaliser.normalise_batch()``.
    """
    return SAMPLE_OHLCV_KLINES


@pytest.fixture(scope="session")
def sample_india_ticks() -> list[dict[str, Any]]:
    """Return the ``SAMPLE_INDIA_TICKS`` constant (5 NSE tick dicts).

    Scope: session — shared across all tests in a run.

    **Do NOT mutate the returned list.**  Each element passes
    ``Normaliser.normalise_quote()``.
    """
    return SAMPLE_INDIA_TICKS
