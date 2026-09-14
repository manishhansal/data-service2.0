"""
Property 2 — Normaliser Round-Trip

parse(serialise(parse(raw))) == parse(raw) for all valid raw inputs.

Requirement: 4.11, 17.8
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.core.normaliser import Normaliser


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

positive_price = st.floats(min_value=0.01, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
positive_volume = st.floats(min_value=0.0, max_value=1_000_000_000.0, allow_nan=False, allow_infinity=False)
valid_timestamp = st.integers(min_value=1_000_000_000, max_value=2_000_000_000)  # epoch seconds


@st.composite
def valid_ohlcv_dict(draw):
    close = draw(positive_price)
    high = draw(st.floats(min_value=close, max_value=close * 2.0, allow_nan=False, allow_infinity=False))
    low = draw(st.floats(min_value=close * 0.5, max_value=close, allow_nan=False, allow_infinity=False))
    open_ = draw(st.floats(min_value=low, max_value=high, allow_nan=False, allow_infinity=False))
    return {
        "time": draw(valid_timestamp),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": draw(positive_volume),
        "provider": "angel_one",
        "interval": "1d",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNormaliserRoundTrip:
    @given(valid_ohlcv_dict())
    @settings(max_examples=100)
    def test_round_trip_idempotent(self, raw: dict) -> None:
        """parse(serialise(parse(raw))) == parse(raw)

        normalise_ohlcv returns (normalised_dict, is_valid, warnings_dict).
        We verify that running the normaliser twice on semantically identical
        input produces identical output — i.e. normalisation is idempotent.
        """
        normaliser = Normaliser()
        provider = raw["provider"]
        result1, is_valid1, _ = normaliser.normalise_ohlcv(raw, provider)
        if not is_valid1:
            return  # invalid input filtered — acceptable

        # Serialise and re-normalise
        serialised = {
            "time": result1["time"],
            "open": result1["open"],
            "high": result1["high"],
            "low": result1["low"],
            "close": result1["close"],
            "volume": result1["volume"],
            "provider": provider,
            "interval": raw.get("interval", "1d"),
        }
        result2, is_valid2, _ = normaliser.normalise_ohlcv(serialised, provider)
        assert is_valid2, "Second normalisation pass must be valid if first was valid"
        assert result2["open"] == result1["open"]
        assert result2["high"] == result1["high"]
        assert result2["low"] == result1["low"]
        assert result2["close"] == result1["close"]
        assert result2["volume"] == result1["volume"]
