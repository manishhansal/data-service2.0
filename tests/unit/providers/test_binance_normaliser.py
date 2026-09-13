"""
Unit tests for BinanceOHLCVNormaliser and BinanceCandleRecord.

Covers:
- Valid candle normalisation (spot and futures symbols)
- All BINANCE_INTERVALS accepted, including 3m (crypto exception)
- OHLC invariant violations → returns None
- Price constraints (prices > 0, volume >= 0)
- Invalid / missing fields → returns None
- Unsupported interval → returns None
- normalise_batch: skips invalid, returns valid subset
- BinanceCandleRecord field defaults (exchange="BINANCE", poor_quality=False)
- Symbol is upper-cased in the record

Requirements: 13.1, 13.8, 13.10
"""

from __future__ import annotations

import pytest

from src.providers.binance_normaliser import BinanceCandleRecord, BinanceOHLCVNormaliser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw(
    *,
    time: int = 1_700_000_000_000,
    open: float = 30_000.0,
    high: float = 30_500.0,
    low: float = 29_800.0,
    close: float = 30_200.0,
    volume: float = 100.5,
    close_time: int = 1_700_003_599_999,
) -> dict:
    """Build a minimal valid raw kline dict (BinanceClient.get_klines output)."""
    return {
        "time":      time,
        "open":      open,
        "high":      high,
        "low":       low,
        "close":     close,
        "volume":    volume,
        "closeTime": close_time,
    }


# ---------------------------------------------------------------------------
# BinanceCandleRecord model tests
# ---------------------------------------------------------------------------


class TestBinanceCandleRecord:
    def test_defaults(self) -> None:
        record = BinanceCandleRecord(
            symbol="BTCUSDT",
            interval="1h",
            time=1_700_000_000_000,
            open=30_000.0,
            high=30_500.0,
            low=29_800.0,
            close=30_200.0,
            volume=50.0,
            closeTime=1_700_003_599_999,
        )
        assert record.exchange == "BINANCE"
        assert record.poor_quality is False

    def test_symbol_is_upper_cased(self) -> None:
        record = BinanceCandleRecord(
            symbol="btcusdt",
            interval="1h",
            time=1_700_000_000_000,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10.0,
            closeTime=1_700_003_599_999,
        )
        assert record.symbol == "BTCUSDT"

    def test_poor_quality_can_be_set(self) -> None:
        record = BinanceCandleRecord(
            symbol="ETHUSDT",
            interval="1m",
            time=1_700_000_000_000,
            open=2000.0,
            high=2100.0,
            low=1950.0,
            close=2050.0,
            volume=5.0,
            closeTime=1_700_000_059_999,
            poor_quality=True,
        )
        assert record.poor_quality is True

    def test_time_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            BinanceCandleRecord(
                symbol="BTCUSDT",
                interval="1h",
                time=0,  # must be > 0
                open=100.0,
                high=110.0,
                low=90.0,
                close=105.0,
                volume=1.0,
                closeTime=1_700_003_599_999,
            )

    def test_prices_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            BinanceCandleRecord(
                symbol="BTCUSDT",
                interval="1h",
                time=1_700_000_000_000,
                open=0.0,  # must be > 0
                high=1.0,
                low=0.1,
                close=0.5,
                volume=1.0,
                closeTime=1_700_003_599_999,
            )

    def test_volume_can_be_zero(self) -> None:
        record = BinanceCandleRecord(
            symbol="BTCUSDT",
            interval="1h",
            time=1_700_000_000_000,
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=0.0,
            closeTime=1_700_003_599_999,
        )
        assert record.volume == 0.0

    def test_volume_cannot_be_negative(self) -> None:
        with pytest.raises(Exception):
            BinanceCandleRecord(
                symbol="BTCUSDT",
                interval="1h",
                time=1_700_000_000_000,
                open=100.0,
                high=110.0,
                low=90.0,
                close=105.0,
                volume=-1.0,
                closeTime=1_700_003_599_999,
            )


# ---------------------------------------------------------------------------
# BinanceOHLCVNormaliser.normalise — happy paths
# ---------------------------------------------------------------------------


class TestNormaliseValid:
    def setup_method(self) -> None:
        self.n = BinanceOHLCVNormaliser()

    def test_valid_1h_candle(self) -> None:
        raw = _make_raw()
        record = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert record is not None
        assert record.symbol == "BTCUSDT"
        assert record.interval == "1h"
        assert record.time == 1_700_000_000_000
        assert record.open == 30_000.0
        assert record.high == 30_500.0
        assert record.low == 29_800.0
        assert record.close == 30_200.0
        assert record.volume == 100.5
        assert record.closeTime == 1_700_003_599_999
        assert record.exchange == "BINANCE"
        assert record.poor_quality is False

    def test_valid_1m_candle(self) -> None:
        raw = _make_raw(high=100.1, low=99.9, open=100.0, close=100.05)
        record = self.n.normalise(raw, symbol="SOLUSDT", interval="1m")
        assert record is not None
        assert record.interval == "1m"

    def test_valid_1d_candle(self) -> None:
        raw = _make_raw()
        record = self.n.normalise(raw, symbol="BTCUSDT", interval="1d")
        assert record is not None

    def test_valid_4h_candle(self) -> None:
        raw = _make_raw()
        record = self.n.normalise(raw, symbol="ETHUSDT", interval="4h")
        assert record is not None

    def test_valid_3m_candle_allowed_for_crypto(self) -> None:
        """3m is a valid Binance interval — must NOT be rejected."""
        raw = _make_raw()
        record = self.n.normalise(raw, symbol="BTCUSDT", interval="3m")
        assert record is not None, "3m interval must be allowed for Binance crypto"
        assert record.interval == "3m"

    def test_high_equals_max_open_close_boundary(self) -> None:
        """high == max(open, close) is valid (boundary equality)."""
        raw = _make_raw(open=100.0, close=105.0, high=105.0, low=99.0)
        record = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert record is not None

    def test_low_equals_min_open_close_boundary(self) -> None:
        """low == min(open, close) is valid (boundary equality)."""
        raw = _make_raw(open=100.0, close=95.0, high=102.0, low=95.0)
        record = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert record is not None

    def test_zero_volume_accepted(self) -> None:
        raw = _make_raw(volume=0.0)
        record = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert record is not None
        assert record.volume == 0.0

    def test_symbol_lower_case_is_upper_cased(self) -> None:
        raw = _make_raw()
        record = self.n.normalise(raw, symbol="ethusdt", interval="1h")
        assert record is not None
        assert record.symbol == "ETHUSDT"

    def test_all_supported_binance_intervals(self) -> None:
        """Every Binance interval should produce a valid record."""
        from src.providers.adapters.binance_rest import BINANCE_INTERVALS
        for interval in BINANCE_INTERVALS:
            raw = _make_raw()
            record = self.n.normalise(raw, symbol="BTCUSDT", interval=interval)
            assert record is not None, f"Expected valid record for interval={interval!r}"


# ---------------------------------------------------------------------------
# BinanceOHLCVNormaliser.normalise — rejection cases
# ---------------------------------------------------------------------------


class TestNormaliseRejection:
    def setup_method(self) -> None:
        self.n = BinanceOHLCVNormaliser()

    def test_unsupported_interval_returns_none(self) -> None:
        raw = _make_raw()
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="10m")
        assert result is None

    def test_empty_interval_returns_none(self) -> None:
        raw = _make_raw()
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="")
        assert result is None

    def test_high_below_open_returns_none(self) -> None:
        # high(29_900) < open(30_000) — invariant violation
        raw = _make_raw(open=30_000.0, high=29_900.0, low=29_800.0, close=30_200.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_high_below_close_returns_none(self) -> None:
        # high(30_100) < close(30_200) — invariant violation
        raw = _make_raw(open=30_000.0, high=30_100.0, low=29_800.0, close=30_200.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_low_above_open_returns_none(self) -> None:
        # low(30_100) > open(30_000) — invariant violation
        raw = _make_raw(open=30_000.0, high=30_500.0, low=30_100.0, close=30_200.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_low_above_close_returns_none(self) -> None:
        # low(30_250) > close(30_200) — invariant violation
        raw = _make_raw(open=30_300.0, high=30_500.0, low=30_250.0, close=30_200.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_zero_open_returns_none(self) -> None:
        raw = _make_raw(open=0.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_zero_high_returns_none(self) -> None:
        raw = _make_raw(high=0.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_zero_low_returns_none(self) -> None:
        raw = _make_raw(low=0.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_zero_close_returns_none(self) -> None:
        raw = _make_raw(close=0.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_negative_price_returns_none(self) -> None:
        raw = _make_raw(open=-1.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_negative_volume_returns_none(self) -> None:
        raw = _make_raw(volume=-5.0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_zero_time_returns_none(self) -> None:
        raw = _make_raw(time=0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_negative_time_returns_none(self) -> None:
        raw = _make_raw(time=-1_000)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_zero_close_time_returns_none(self) -> None:
        raw = _make_raw(close_time=0)
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_missing_time_returns_none(self) -> None:
        raw = _make_raw()
        del raw["time"]
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_missing_open_returns_none(self) -> None:
        raw = _make_raw()
        del raw["open"]
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_missing_high_returns_none(self) -> None:
        raw = _make_raw()
        del raw["high"]
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_missing_close_time_returns_none(self) -> None:
        raw = _make_raw()
        del raw["closeTime"]
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_string_prices_converted_if_numeric(self) -> None:
        """BinanceClient already coerces; if strings slip through, test fallback."""
        raw = _make_raw()
        raw["open"] = "30000.0"
        raw["high"] = "30500.0"
        raw["low"] = "29800.0"
        raw["close"] = "30200.0"
        raw["volume"] = "100.5"
        raw["time"] = "1700000000000"
        raw["closeTime"] = "1700003599999"
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is not None, "Numeric strings should coerce successfully"
        assert result.open == 30_000.0

    def test_non_numeric_string_price_returns_none(self) -> None:
        raw = _make_raw()
        raw["open"] = "NaN"
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None

    def test_none_open_returns_none(self) -> None:
        raw = _make_raw()
        raw["open"] = None
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="1h")
        assert result is None


# ---------------------------------------------------------------------------
# BinanceOHLCVNormaliser.normalise_batch
# ---------------------------------------------------------------------------


class TestNormaliseBatch:
    def setup_method(self) -> None:
        self.n = BinanceOHLCVNormaliser()

    def test_empty_list_returns_empty(self) -> None:
        result = self.n.normalise_batch([], symbol="BTCUSDT", interval="1h")
        assert result == []

    def test_all_valid_candles_returned(self) -> None:
        raws = [_make_raw(time=1_700_000_000_000 + i * 3_600_000) for i in range(5)]
        result = self.n.normalise_batch(raws, symbol="BTCUSDT", interval="1h")
        assert len(result) == 5

    def test_invalid_candles_are_skipped(self) -> None:
        valid = _make_raw()
        invalid_high = _make_raw(open=30_000.0, high=29_000.0, low=29_800.0, close=30_200.0)
        invalid_zero_open = _make_raw(open=0.0)

        result = self.n.normalise_batch(
            [valid, invalid_high, invalid_zero_open],
            symbol="BTCUSDT",
            interval="1h",
        )
        assert len(result) == 1
        assert result[0].time == valid["time"]

    def test_all_invalid_returns_empty(self) -> None:
        raws = [_make_raw(high=0.0) for _ in range(3)]
        result = self.n.normalise_batch(raws, symbol="BTCUSDT", interval="1h")
        assert result == []

    def test_batch_with_3m_interval(self) -> None:
        raws = [_make_raw(time=1_700_000_000_000 + i * 180_000) for i in range(3)]
        result = self.n.normalise_batch(raws, symbol="BTCUSDT", interval="3m")
        assert len(result) == 3, "3m must be allowed for Binance crypto batch"

    def test_batch_preserves_order(self) -> None:
        timestamps = [1_700_000_000_000 + i * 3_600_000 for i in range(4)]
        raws = [_make_raw(time=t) for t in timestamps]
        result = self.n.normalise_batch(raws, symbol="BTCUSDT", interval="1h")
        assert [r.time for r in result] == timestamps

    def test_batch_mixed_symbols_same_interval(self) -> None:
        """normalise_batch is single-symbol; symbol param is applied to all."""
        raws = [_make_raw(time=1_700_000_000_000 + i * 60_000) for i in range(2)]
        result = self.n.normalise_batch(raws, symbol="ETHUSDT", interval="1m")
        for r in result:
            assert r.symbol == "ETHUSDT"


# ---------------------------------------------------------------------------
# 3m interval — explicit crypto exception regression tests
# ---------------------------------------------------------------------------


class TestThreeMIntervalException:
    def setup_method(self) -> None:
        self.n = BinanceOHLCVNormaliser()

    def test_3m_produces_record_not_none(self) -> None:
        raw = _make_raw()
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="3m")
        assert result is not None

    def test_3m_interval_stored_in_record(self) -> None:
        raw = _make_raw()
        result = self.n.normalise(raw, symbol="BTCUSDT", interval="3m")
        assert result is not None
        assert result.interval == "3m"

    def test_3m_batch_all_valid(self) -> None:
        raws = [_make_raw(time=1_700_000_000_000 + i * 180_000) for i in range(10)]
        result = self.n.normalise_batch(raws, symbol="SOLUSDT", interval="3m")
        assert len(result) == 10
