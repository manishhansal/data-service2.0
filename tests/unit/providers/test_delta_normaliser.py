"""
Unit tests for src/providers/delta_normaliser.py

Mirrors test_binance_normaliser.py in structure.

Requirements: DS2-RCA-001
"""

from __future__ import annotations

from typing import Any

import pytest

from src.providers.delta_normaliser import DeltaCandleRecord, DeltaOHLCVNormaliser

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw(
    *,
    time_ms: int = 1_700_000_000_000,
    close_time_ms: int = 1_700_003_599_999,
    open_: float = 43_000.0,
    high: float = 43_500.0,
    low: float = 42_800.0,
    close: float = 43_200.0,
    volume: float = 15.5,
) -> dict[str, Any]:
    return {
        "time":      time_ms,
        "open":      open_,
        "high":      high,
        "low":       low,
        "close":     close,
        "volume":    volume,
        "closeTime": close_time_ms,
        "symbol":    "BTCUSD",
        "interval":  "1h",
        "exchange":  "DELTA",
    }


# ---------------------------------------------------------------------------
# DeltaCandleRecord
# ---------------------------------------------------------------------------


class TestDeltaCandleRecord:
    def test_valid_record(self) -> None:
        rec = DeltaCandleRecord(
            symbol="BTCUSD",
            interval="1h",
            time=1_700_000_000_000,
            open=43_000.0,
            high=43_500.0,
            low=42_800.0,
            close=43_200.0,
            volume=15.5,
            closeTime=1_700_003_599_999,
        )
        assert rec.exchange == "DELTA"
        assert rec.poor_quality is False

    def test_symbol_uppercased(self) -> None:
        rec = DeltaCandleRecord(
            symbol="btcusd",
            interval="1h",
            time=1_700_000_000_001,
            open=1.0, high=1.0, low=1.0, close=1.0,
            volume=0.0, closeTime=1_700_003_600_000,
        )
        assert rec.symbol == "BTCUSD"


# ---------------------------------------------------------------------------
# DeltaOHLCVNormaliser
# ---------------------------------------------------------------------------


class TestDeltaOHLCVNormaliser:
    def setup_method(self) -> None:
        self.normaliser = DeltaOHLCVNormaliser()

    def test_valid_candle_accepted(self) -> None:
        rec = self.normaliser.normalise(_raw(), symbol="BTCUSD", interval="1h")
        assert rec is not None
        assert isinstance(rec, DeltaCandleRecord)

    def test_exchange_is_delta(self) -> None:
        rec = self.normaliser.normalise(_raw(), symbol="BTCUSD", interval="1h")
        assert rec is not None
        assert rec.exchange == "DELTA"

    def test_3m_interval_accepted(self) -> None:
        """3m is NOT banned for Delta crypto."""
        rec = self.normaliser.normalise(_raw(), symbol="BTCUSD", interval="3m")
        assert rec is not None

    def test_unknown_interval_returns_none(self) -> None:
        rec = self.normaliser.normalise(_raw(), symbol="BTCUSD", interval="10m")
        assert rec is None

    def test_high_below_max_open_close_rejected(self) -> None:
        raw = _raw(open_=100.0, high=99.0, low=95.0, close=98.0)
        rec = self.normaliser.normalise(raw, symbol="BTCUSD", interval="1h")
        assert rec is None

    def test_low_above_min_open_close_rejected(self) -> None:
        raw = _raw(open_=100.0, high=110.0, low=105.0, close=108.0)
        rec = self.normaliser.normalise(raw, symbol="BTCUSD", interval="1h")
        assert rec is None

    def test_missing_time_returns_none(self) -> None:
        raw = _raw()
        del raw["time"]
        rec = self.normaliser.normalise(raw, symbol="BTCUSD", interval="1h")
        assert rec is None

    def test_negative_close_returns_none(self) -> None:
        raw = _raw(open_=1.0, high=1.0, low=1.0, close=-1.0)
        rec = self.normaliser.normalise(raw, symbol="BTCUSD", interval="1h")
        assert rec is None

    def test_zero_volume_accepted(self) -> None:
        raw = _raw(volume=0.0)
        rec = self.normaliser.normalise(raw, symbol="BTCUSD", interval="1h")
        assert rec is not None
        assert rec.volume == 0.0

    def test_batch_skips_invalid(self) -> None:
        good = _raw()
        bad = _raw(open_=100.0, high=99.0, low=95.0, close=98.0)  # high < max(open, close)
        records = self.normaliser.normalise_batch([good, bad, good], "BTCUSD", "1h")
        assert len(records) == 2

    def test_batch_empty_input(self) -> None:
        records = self.normaliser.normalise_batch([], "BTCUSD", "1h")
        assert records == []

    def test_ohlcv_values_preserved(self) -> None:
        rec = self.normaliser.normalise(
            _raw(open_=43_000.0, high=43_500.0, low=42_800.0, close=43_200.0, volume=15.5),
            symbol="BTCUSD",
            interval="1h",
        )
        assert rec is not None
        assert rec.open == 43_000.0
        assert rec.high == 43_500.0
        assert rec.low == 42_800.0
        assert rec.close == 43_200.0
        assert rec.volume == 15.5
