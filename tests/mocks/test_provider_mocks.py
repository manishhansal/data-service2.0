"""
tests/mocks/test_provider_mocks.py
====================================

Validates that mock objects return correctly-shaped data and that factory
function outputs pass through the Normaliser without generating DataIncidents.

Coverage
--------
- ``make_valid_tick()`` → passes ``Normaliser.normalise_quote()``
- ``make_valid_ohlcv_candle()`` → passes ``Normaliser.normalise_ohlcv()``
- ``make_option_chain_row()`` → passes ``Normaliser.normalise_option_chain_row()``
  and ``QualityEngine.validate_option_chain_record()``
- ``make_deribit_ticker()`` → correct shape and null semantics
- ``MockAngelOneProvider`` → all three async methods return correct shapes
- ``MockBinanceClient`` → get_klines / get_ticker_price / get_24hr_stats return
  correct shapes; BinanceOHLCVNormaliser accepts SAMPLE_OHLCV_KLINES
- ``MockDeribitClient`` → get_instruments / get_ticker / get_index_price return
  correct shapes
- ``MockRedis`` → get/set/setex/delete/xadd/hset/hget/lpush/lrange/pipeline
- ``SAMPLE_OHLCV_KLINES`` → all 5 entries normalise without error
- ``SAMPLE_INDIA_TICKS`` → all 5 entries normalise without error
- Factory override mechanism — ``**overrides`` replaces defaults correctly
- OI/IV/Greek null semantics are preserved through the factory functions
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
    _BASE_EVENT_TIME_MS,
    make_deribit_ticker,
    make_option_chain_row,
    make_valid_ohlcv_candle,
    make_valid_tick,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_normaliser():
    """Return a shared Normaliser instance (avoids repeated imports in tests)."""
    from src.core.normaliser import Normaliser
    return Normaliser()


def _get_quality_engine():
    """Return a shared QualityEngine instance."""
    from src.engines.quality_engine import QualityEngine
    return QualityEngine()


# ===========================================================================
# Factory functions — make_valid_tick
# ===========================================================================


class TestMakeValidTick:
    """make_valid_tick() produces dicts that pass normalise_quote()."""

    def test_default_tick_passes_normaliser(self) -> None:
        normaliser = _get_normaliser()
        tick = make_valid_tick()
        output, ok, incident = normaliser.normalise_quote(tick, provider="angel_one")
        assert ok is True, f"Normaliser rejected default tick — incident: {incident}"
        assert incident is None

    def test_ltp_is_positive(self) -> None:
        tick = make_valid_tick()
        assert tick["ltp"] > 0

    def test_oi_is_none_for_equity(self) -> None:
        """Equity ticks must have oi=None and oiMissing=True (Requirement 3.3, 6.2)."""
        tick = make_valid_tick()
        assert tick["oi"] is None
        assert tick["oiMissing"] is True

    def test_bid_ask_are_none(self) -> None:
        """bid and ask must be None when not provided — zero is prohibited (Req 6.6)."""
        tick = make_valid_tick()
        assert tick["bid"] is None
        assert tick["ask"] is None

    def test_override_replaces_default(self) -> None:
        tick = make_valid_tick(ltp=9999.99, symbol="BANKNIFTY")
        assert tick["ltp"] == 9999.99
        assert tick["symbol"] == "BANKNIFTY"

    def test_normalised_output_preserves_oi_none(self) -> None:
        """The normaliser must not fabricate an OI value when input oi is None."""
        normaliser = _get_normaliser()
        tick = make_valid_tick()  # oi=None
        output, ok, _ = normaliser.normalise_quote(tick, provider="angel_one")
        assert ok is True
        assert output["oi"] is None
        assert output["oiMissing"] is True

    def test_fno_tick_with_oi_passes_normaliser(self) -> None:
        """F&O ticks with a real OI value should also pass normalisation."""
        normaliser = _get_normaliser()
        tick = make_valid_tick(
            symbol="NIFTY25JANFUT",
            instrumentId="NFO:NIFTY25JANFUT:FUTIDX",
            oi=524_800,
            oiMissing=False,
        )
        output, ok, incident = normaliser.normalise_quote(tick, provider="angel_one")
        assert ok is True
        assert output["oi"] == 524_800
        assert output["oiMissing"] is False

    def test_all_five_sample_ticks_pass_normaliser(self) -> None:
        """Every entry in SAMPLE_INDIA_TICKS must normalise without incident."""
        normaliser = _get_normaliser()
        for i, tick in enumerate(SAMPLE_INDIA_TICKS):
            output, ok, incident = normaliser.normalise_quote(tick, provider="angel_one")
            assert ok is True, f"SAMPLE_INDIA_TICKS[{i}] failed — incident: {incident}"


# ===========================================================================
# Factory functions — make_valid_ohlcv_candle
# ===========================================================================


class TestMakeValidOhlcvCandle:
    """make_valid_ohlcv_candle() produces dicts that pass normalise_ohlcv()."""

    def test_default_candle_passes_normaliser(self) -> None:
        normaliser = _get_normaliser()
        candle = make_valid_ohlcv_candle()
        output, ok, incident = normaliser.normalise_ohlcv(candle, provider="angel_one")
        assert ok is True, f"Normaliser rejected default candle — incident: {incident}"

    def test_ohlcv_invariants_hold(self) -> None:
        candle = make_valid_ohlcv_candle()
        assert candle["high"] >= max(candle["open"], candle["close"])
        assert candle["low"]  <= min(candle["open"], candle["close"])
        assert candle["volume"] >= 0
        for price_field in ("open", "high", "low", "close"):
            assert candle[price_field] > 0, f"{price_field} must be > 0"

    def test_override_price_fields(self) -> None:
        candle = make_valid_ohlcv_candle(open=3000.0, high=3100.0, low=2950.0, close=3050.0)
        assert candle["open"] == 3000.0
        assert candle["high"] == 3100.0

    def test_oi_is_none_for_equity_candle(self) -> None:
        candle = make_valid_ohlcv_candle()
        assert candle["oi"] is None
        assert candle["oiMissing"] is True

    def test_candle_with_oi_passes_normaliser(self) -> None:
        normaliser = _get_normaliser()
        candle = make_valid_ohlcv_candle(
            symbol="NIFTY25JANFUT",
            oi=524_800,
            oiMissing=False,
        )
        output, ok, incident = normaliser.normalise_ohlcv(candle, provider="jugaad_data")
        assert ok is True


# ===========================================================================
# Factory functions — make_option_chain_row
# ===========================================================================


class TestMakeOptionChainRow:
    """make_option_chain_row() produces dicts that pass option chain validation."""

    def test_default_row_passes_normaliser(self) -> None:
        normaliser = _get_normaliser()
        row = make_option_chain_row()
        output, ok, incident = normaliser.normalise_option_chain_row(row, provider="scrapling_nse")
        assert ok is True, f"Normaliser rejected option chain row — incident: {incident}"

    def test_greeks_are_none(self) -> None:
        """Greeks must be None when not provided (Req 6.5 — zero is prohibited)."""
        row = make_option_chain_row()
        for greek in ("delta", "gamma", "theta", "vega", "rho"):
            assert row[greek] is None, f"{greek} should be None, not {row[greek]}"

    def test_iv_is_positive_not_zero(self) -> None:
        """IV must be a positive float, never 0 (Req 6.4)."""
        row = make_option_chain_row()
        assert row["iv"] is not None
        assert row["iv"] > 0.0

    def test_bid_less_than_ask(self) -> None:
        """No crossed market: bid < ask (Req 7.10)."""
        row = make_option_chain_row()
        assert row["bid"] is not None
        assert row["ask"] is not None
        assert row["bid"] < row["ask"]

    def test_oi_is_positive_integer(self) -> None:
        row = make_option_chain_row()
        assert row["oi"] is not None
        assert row["oi"] > 0

    def test_option_chain_passes_quality_engine_validation(self) -> None:
        """The quality engine must accept the default option chain row with a future expiry."""
        from src.engines.quality_engine import QualityEngine
        import datetime
        engine = QualityEngine()
        # Use a future expiry so the quality engine's date check passes
        future_expiry = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        row = make_option_chain_row(expiry=future_expiry)
        # validate_option_chain_record() expects a raw dict, not a Pydantic model
        result = engine.validate_option_chain_record(row)
        assert result.valid is True, f"QualityEngine rejected row: {result.issues}"

    def test_override_strike(self) -> None:
        row = make_option_chain_row(strike=22000.0, optionType="PE")
        assert row["strike"] == 22000.0
        assert row["optionType"] == "PE"

    def test_iv_none_row_passes_normaliser(self) -> None:
        """Rows with iv=None and ivMissing=True are valid (Req 6.4)."""
        normaliser = _get_normaliser()
        row = make_option_chain_row(iv=None, ivMissing=True)
        output, ok, incident = normaliser.normalise_option_chain_row(row, provider="scrapling_nse")
        assert ok is True


# ===========================================================================
# Factory functions — make_deribit_ticker
# ===========================================================================


class TestMakeDeribitTicker:
    """make_deribit_ticker() produces correctly-shaped Deribit ticker dicts."""

    def test_default_ticker_has_expected_fields(self) -> None:
        ticker = make_deribit_ticker()
        required_fields = (
            "instrument_name", "mark_price", "mark_iv", "open_interest",
            "volume", "underlying_price", "bid", "ask",
        )
        for field in required_fields:
            assert field in ticker, f"Missing field: {field}"

    def test_instrument_name_format(self) -> None:
        """Default instrument name must follow Deribit naming convention."""
        ticker = make_deribit_ticker()
        parts = ticker["instrument_name"].split("-")
        assert len(parts) == 4
        assert parts[0] in ("BTC", "ETH", "SOL")
        assert parts[-1] in ("C", "P")

    def test_bid_less_than_ask(self) -> None:
        ticker = make_deribit_ticker()
        assert ticker["bid"] < ticker["ask"]

    def test_mark_iv_positive(self) -> None:
        ticker = make_deribit_ticker()
        assert ticker["mark_iv"] > 0

    def test_null_fields_accepted(self) -> None:
        """When provider returns null for optional fields, None must be preserved (Req 14.5)."""
        ticker = make_deribit_ticker(
            mark_iv=None,
            open_interest=None,
            underlying_price=None,
        )
        assert ticker["mark_iv"] is None
        assert ticker["open_interest"] is None
        assert ticker["underlying_price"] is None

    def test_override_strike_currency(self) -> None:
        ticker = make_deribit_ticker(instrument_name="ETH-26JAN24-2500-P")
        assert ticker["instrument_name"] == "ETH-26JAN24-2500-P"


# ===========================================================================
# MockAngelOneProvider
# ===========================================================================


@pytest.mark.asyncio
class TestMockAngelOneProvider:
    """MockAngelOneProvider returns correct shapes and supports exception injection."""

    async def test_fetch_live_quote_returns_dict_with_ltp(self) -> None:
        mock = MockAngelOneProvider()
        result = await mock.fetch_live_quote("NSE:RELIANCE:EQ")
        assert isinstance(result, dict)
        assert "ltp" in result
        assert result["ltp"] > 0

    async def test_fetch_live_quote_sets_instrument_id(self) -> None:
        mock = MockAngelOneProvider()
        result = await mock.fetch_live_quote("NSE:INFY:EQ")
        assert result["instrumentId"] == "NSE:INFY:EQ"

    async def test_fetch_option_chain_returns_list_of_dicts(self) -> None:
        mock = MockAngelOneProvider()
        rows = await mock.fetch_option_chain("NIFTY", expiry="2024-01-25")
        assert isinstance(rows, list)
        assert len(rows) >= 1
        for row in rows:
            assert "strike" in row
            assert "optionType" in row

    async def test_fetch_historical_ohlcv_returns_list_with_ohlcv(self) -> None:
        mock = MockAngelOneProvider()
        candles = await mock.fetch_historical_ohlcv(
            symbol="RELIANCE", exchange="NSE", interval="1d",
            from_date=None, to_date=None,
        )
        assert isinstance(candles, list)
        assert len(candles) == 5
        for c in candles:
            assert c["high"] >= c["open"]
            assert c["low"]  <= c["close"]

    async def test_should_raise_propagates_to_all_methods(self) -> None:
        from src.providers.adapters.base import ProviderUnavailableError
        mock = MockAngelOneProvider()
        mock.should_raise = ProviderUnavailableError("timeout", provider="angel_one")

        with pytest.raises(ProviderUnavailableError):
            await mock.fetch_live_quote("NSE:RELIANCE:EQ")

        with pytest.raises(ProviderUnavailableError):
            await mock.fetch_option_chain("NIFTY")

        with pytest.raises(ProviderUnavailableError):
            await mock.fetch_historical_ohlcv("RELIANCE", "NSE", "1d", None, None)

    async def test_response_override_works(self) -> None:
        mock = MockAngelOneProvider()
        mock.live_quote_response = make_valid_tick(symbol="HDFCBANK", ltp=1600.0)
        result = await mock.fetch_live_quote("NSE:HDFCBANK:EQ")
        assert result["ltp"] == 1600.0

    async def test_close_does_not_raise(self) -> None:
        mock = MockAngelOneProvider()
        await mock.close()  # should not raise

    async def test_live_quote_passes_normaliser(self) -> None:
        """The normaliser must accept the default mock live quote."""
        normaliser = _get_normaliser()
        mock = MockAngelOneProvider()
        result = await mock.fetch_live_quote("NSE:RELIANCE:EQ")
        output, ok, incident = normaliser.normalise_quote(result, provider="angel_one")
        assert ok is True, f"Normaliser rejected AngelOne mock quote: {incident}"


# ===========================================================================
# MockBinanceClient
# ===========================================================================


@pytest.mark.asyncio
class TestMockBinanceClient:
    """MockBinanceClient returns klines and ticker data in the expected shapes."""

    async def test_get_klines_returns_5_candles(self) -> None:
        mock = MockBinanceClient()
        klines = await mock.get_klines("BTCUSDT", "1m", limit=5)
        assert isinstance(klines, list)
        assert len(klines) == 5

    async def test_kline_has_required_fields(self) -> None:
        mock = MockBinanceClient()
        klines = await mock.get_klines("BTCUSDT", "1m")
        for k in klines:
            for field in ("time", "open", "high", "low", "close", "volume", "closeTime"):
                assert field in k, f"Missing field '{field}' in kline"

    async def test_klines_pass_binance_normaliser(self) -> None:
        """All kline dicts in SAMPLE_OHLCV_KLINES must pass BinanceOHLCVNormaliser."""
        from src.providers.binance_normaliser import BinanceOHLCVNormaliser
        normaliser = BinanceOHLCVNormaliser()
        mock = MockBinanceClient()
        klines = await mock.get_klines("BTCUSDT", "1m")
        records = normaliser.normalise_batch(klines, symbol="BTCUSDT", interval="1m")
        assert len(records) == 5, "All 5 klines should normalise without rejection"
        for rec in records:
            assert rec.high >= max(rec.open, rec.close)
            assert rec.low  <= min(rec.open, rec.close)

    async def test_get_ticker_price_returns_symbol_and_price(self) -> None:
        mock = MockBinanceClient()
        result = await mock.get_ticker_price("BTCUSDT")
        assert "symbol" in result
        assert "price" in result
        assert result["symbol"] == "BTCUSDT"
        assert float(result["price"]) > 0

    async def test_get_ticker_price_upcases_symbol(self) -> None:
        mock = MockBinanceClient()
        result = await mock.get_ticker_price("ethusdt")
        assert result["symbol"] == "ETHUSDT"

    async def test_get_24hr_stats_has_required_fields(self) -> None:
        mock = MockBinanceClient()
        stats = await mock.get_24hr_stats("BTCUSDT")
        for field in ("symbol", "highPrice", "lowPrice", "lastPrice", "volume"):
            assert field in stats, f"Missing field: {field}"

    async def test_should_raise_propagates(self) -> None:
        from src.providers.adapters.base import ProviderUnavailableError
        mock = MockBinanceClient()
        mock.should_raise = ProviderUnavailableError("network error", provider="binance")

        with pytest.raises(ProviderUnavailableError):
            await mock.get_klines("BTCUSDT", "1m")

    async def test_klines_response_override(self) -> None:
        mock = MockBinanceClient()
        custom_klines = [make_valid_ohlcv_candle(symbol="BTCUSDT", interval="1h")]
        mock.klines_response = custom_klines
        result = await mock.get_klines("BTCUSDT", "1h")
        assert len(result) == 1

    async def test_context_manager(self) -> None:
        async with MockBinanceClient() as client:
            klines = await client.get_klines("SOLUSDT", "1h")
        assert isinstance(klines, list)

    async def test_sample_ohlcv_klines_are_ordered_ascending(self) -> None:
        """Klines should be ordered oldest-first (ascending timestamps)."""
        mock = MockBinanceClient()
        klines = await mock.get_klines("BTCUSDT", "1m")
        times = [k["time"] for k in klines]
        assert times == sorted(times), "Klines must be ordered oldest-first"


# ===========================================================================
# MockDeribitClient
# ===========================================================================


@pytest.mark.asyncio
class TestMockDeribitClient:
    """MockDeribitClient returns instruments, tickers, and index prices correctly."""

    async def test_get_instruments_returns_list_of_two(self) -> None:
        mock = MockDeribitClient()
        instruments = await mock.get_instruments("BTC", kind="option")
        assert isinstance(instruments, list)
        assert len(instruments) == 2

    async def test_instruments_have_required_fields(self) -> None:
        mock = MockDeribitClient()
        instruments = await mock.get_instruments("BTC")
        for inst in instruments:
            for field in ("instrument_name", "strike", "option_type", "expiration_timestamp"):
                assert field in inst, f"Missing field: {field}"

    async def test_instrument_names_follow_deribit_format(self) -> None:
        """Instrument names must match {CURRENCY}-{DDMONYY}-{STRIKE}-{C|P}."""
        mock = MockDeribitClient()
        instruments = await mock.get_instruments("BTC")
        for inst in instruments:
            name = inst["instrument_name"]
            parts = name.split("-")
            assert len(parts) == 4, f"Unexpected name format: {name}"
            assert parts[-1] in ("C", "P")

    async def test_get_ticker_returns_mark_iv(self) -> None:
        mock = MockDeribitClient()
        ticker = await mock.get_ticker("BTC-26JAN24-42000-C")
        assert ticker["mark_iv"] is not None
        assert ticker["mark_iv"] > 0

    async def test_get_ticker_preserves_instrument_name(self) -> None:
        mock = MockDeribitClient()
        ticker = await mock.get_ticker("ETH-26JAN24-2500-P")
        assert ticker["instrument_name"] == "ETH-26JAN24-2500-P"

    async def test_get_index_price_returns_float(self) -> None:
        mock = MockDeribitClient()
        result = await mock.get_index_price("btc_usd")
        assert isinstance(result["index_price"], float)
        assert result["index_price"] > 0

    async def test_should_raise_propagates_to_all_methods(self) -> None:
        from src.providers.adapters.base import ProviderUnavailableError
        mock = MockDeribitClient()
        mock.should_raise = ProviderUnavailableError("deribit down", provider="deribit")

        with pytest.raises(ProviderUnavailableError):
            await mock.get_instruments("BTC")

        with pytest.raises(ProviderUnavailableError):
            await mock.get_ticker("BTC-26JAN24-42000-C")

        with pytest.raises(ProviderUnavailableError):
            await mock.get_index_price("btc_usd")

    async def test_ticker_bid_ask_spread(self) -> None:
        mock = MockDeribitClient()
        ticker = await mock.get_ticker("BTC-26JAN24-42000-C")
        assert ticker["bid"] < ticker["ask"]

    async def test_context_manager(self) -> None:
        async with MockDeribitClient() as client:
            result = await client.get_index_price("eth_usd")
        assert "index_price" in result


# ===========================================================================
# MockRedis
# ===========================================================================


@pytest.mark.asyncio
class TestMockRedis:
    """MockRedis implements the expected Redis command semantics in-memory."""

    async def test_set_and_get_roundtrip(self) -> None:
        redis = MockRedis()
        await redis.set("test_key", b"hello")
        result = await redis.get("test_key")
        assert result == b"hello"

    async def test_get_missing_key_returns_none(self) -> None:
        redis = MockRedis()
        assert await redis.get("nonexistent") is None

    async def test_setex_stores_value_with_ttl(self) -> None:
        redis = MockRedis()
        await redis.setex("key_with_ttl", 30, b"data")
        result = await redis.get("key_with_ttl")
        assert result == b"data"
        ttl = await redis.ttl("key_with_ttl")
        assert ttl == 30

    async def test_delete_removes_key(self) -> None:
        redis = MockRedis()
        await redis.set("to_delete", b"value")
        deleted = await redis.delete("to_delete")
        assert deleted == 1
        assert await redis.get("to_delete") is None

    async def test_delete_returns_count_of_deleted(self) -> None:
        redis = MockRedis()
        await redis.set("k1", b"v1")
        await redis.set("k2", b"v2")
        count = await redis.delete("k1", "k2", "k3_missing")
        assert count == 2

    async def test_ttl_missing_key_returns_negative_two(self) -> None:
        redis = MockRedis()
        ttl = await redis.ttl("no_such_key")
        assert ttl == -2

    async def test_exists_returns_count(self) -> None:
        redis = MockRedis()
        await redis.set("exists_key", b"v")
        assert await redis.exists("exists_key") == 1
        assert await redis.exists("missing_key") == 0

    async def test_incr_increments_from_zero(self) -> None:
        redis = MockRedis()
        v1 = await redis.incr("counter")
        v2 = await redis.incr("counter")
        assert v1 == 1
        assert v2 == 2

    async def test_hset_and_hget(self) -> None:
        redis = MockRedis()
        await redis.hset("my_hash", mapping={"field1": "value1", "field2": "value2"})
        result = await redis.hget("my_hash", "field1")
        assert result == b"value1"

    async def test_hgetall_returns_all_fields(self) -> None:
        redis = MockRedis()
        await redis.hset("cb_state", mapping={"state": "CLOSED", "failure_count": "0"})
        result = await redis.hgetall("cb_state")
        assert b"state" in result
        assert result[b"state"] == b"CLOSED"

    async def test_xadd_returns_message_id(self) -> None:
        redis = MockRedis()
        msg_id = await redis.xadd(
            "mds:events:ticks",
            {"symbol": "NIFTY", "ltp": "21850.35"},
        )
        assert isinstance(msg_id, bytes)
        assert len(msg_id) > 0

    async def test_xadd_stores_in_stream_entries(self) -> None:
        redis = MockRedis()
        await redis.xadd("mds:events:ticks", {"symbol": "RELIANCE"})
        await redis.xadd("mds:events:ticks", {"symbol": "INFY"})
        assert await redis.xlen("mds:events:ticks") == 2

    async def test_lpush_and_lrange(self) -> None:
        redis = MockRedis()
        await redis.lpush("my_list", "c", "b", "a")
        result = await redis.lrange("my_list", 0, -1)
        assert "a" in result
        assert len(result) == 3

    async def test_pipeline_executes_commands(self) -> None:
        redis = MockRedis()
        pipeline = redis.pipeline()
        await pipeline.set("p_key1", b"v1")
        await pipeline.set("p_key2", b"v2")
        results = await pipeline.execute()
        assert results == [True, True]
        assert await redis.get("p_key1") == b"v1"
        assert await redis.get("p_key2") == b"v2"

    async def test_eval_returns_one_by_default(self) -> None:
        """eval() defaults to 1 (token available) for token-bucket rate limiter."""
        redis = MockRedis()
        result = await redis.eval("SOME_LUA_SCRIPT", 1, "key")
        assert result == 1

    async def test_ping_returns_true(self) -> None:
        redis = MockRedis()
        assert await redis.ping() is True

    async def test_context_manager(self) -> None:
        async with MockRedis() as redis:
            await redis.set("ctx_key", b"ctx_val")
            val = await redis.get("ctx_key")
        assert val == b"ctx_val"


# ===========================================================================
# Sample data collections
# ===========================================================================


class TestSampleData:
    """SAMPLE_OHLCV_KLINES and SAMPLE_INDIA_TICKS have the correct shapes."""

    def test_sample_ohlcv_klines_has_five_entries(self) -> None:
        assert len(SAMPLE_OHLCV_KLINES) == 5

    def test_sample_ohlcv_klines_are_time_ordered(self) -> None:
        times = [k["time"] for k in SAMPLE_OHLCV_KLINES]
        assert times == sorted(times)

    def test_sample_ohlcv_klines_all_pass_binance_normaliser(self) -> None:
        from src.providers.binance_normaliser import BinanceOHLCVNormaliser
        normaliser = BinanceOHLCVNormaliser()
        records = normaliser.normalise_batch(SAMPLE_OHLCV_KLINES, symbol="BTCUSDT", interval="1m")
        assert len(records) == 5

    def test_sample_india_ticks_has_five_entries(self) -> None:
        assert len(SAMPLE_INDIA_TICKS) == 5

    def test_sample_india_ticks_symbols_are_distinct(self) -> None:
        symbols = [t["symbol"] for t in SAMPLE_INDIA_TICKS]
        assert len(set(symbols)) == 5

    def test_sample_india_ticks_all_pass_normaliser(self) -> None:
        normaliser = _get_normaliser()
        for i, tick in enumerate(SAMPLE_INDIA_TICKS):
            output, ok, incident = normaliser.normalise_quote(tick, provider="angel_one")
            assert ok is True, f"SAMPLE_INDIA_TICKS[{i}] failed: {incident}"

    def test_nifty_tick_oi_is_none(self) -> None:
        """The NIFTY index tick (last in sample) must have oi=None (it's an index)."""
        nifty_tick = next(t for t in SAMPLE_INDIA_TICKS if t["symbol"] == "NIFTY")
        assert nifty_tick["oi"] is None
        assert nifty_tick["oiMissing"] is True

    def test_all_klines_have_positive_prices(self) -> None:
        for kline in SAMPLE_OHLCV_KLINES:
            for price_field in ("open", "high", "low", "close"):
                assert kline[price_field] > 0

    def test_all_klines_ohlc_invariant(self) -> None:
        for k in SAMPLE_OHLCV_KLINES:
            assert k["high"] >= max(k["open"], k["close"])
            assert k["low"]  <= min(k["open"], k["close"])


# ===========================================================================
# Override mechanism edge cases
# ===========================================================================


class TestFactoryOverrides:
    """Override mechanism correctly replaces defaults without affecting sibling keys."""

    def test_tick_override_only_changes_specified_keys(self) -> None:
        tick = make_valid_tick(ltp=5000.0)
        assert tick["ltp"] == 5000.0
        # All other defaults must be preserved
        assert "symbol" in tick
        assert "exchange" in tick
        assert tick["oi"] is None

    def test_candle_override_preserves_other_keys(self) -> None:
        candle = make_valid_ohlcv_candle(interval="5m")
        assert candle["interval"] == "5m"
        assert "symbol" in candle
        assert candle["oi"] is None

    def test_option_row_override_preserves_null_greeks(self) -> None:
        row = make_option_chain_row(strike=23000.0)
        assert row["strike"] == 23000.0
        assert row["delta"] is None
        assert row["gamma"] is None

    def test_deribit_ticker_partial_null_override(self) -> None:
        """Override single field to None — other fields must remain unchanged."""
        ticker = make_deribit_ticker(mark_iv=None)
        assert ticker["mark_iv"] is None
        assert ticker["underlying_price"] is not None  # unchanged default
