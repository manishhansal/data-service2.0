"""
Property 12 — OI Semantic Integrity

OI (open interest) and tradedValue are semantically distinct fields.
OI must NEVER be populated from tradedValue.
When OI is absent, the field must be null + oiMissing=True.

Requirement: 3.3, 6.2
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.core.normaliser import Normaliser

positive_price = st.floats(min_value=0.01, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
positive_volume = st.floats(min_value=0.0, max_value=1_000_000_000.0, allow_nan=False, allow_infinity=False)
traded_value_st = st.floats(min_value=1_000.0, max_value=1_000_000_000_000.0, allow_nan=False, allow_infinity=False)


class TestOISemanticIntegrity:
    @given(positive_price, positive_price, positive_price, positive_price, positive_volume, traded_value_st)
    @settings(max_examples=100)
    def test_oi_missing_when_not_supplied(
        self,
        open_: float,
        high_delta: float,
        low_delta: float,
        close: float,
        volume: float,
        traded_value: float,
    ) -> None:
        """When oi is not in input, normalised output has oi=None or oiMissing=True."""
        high = close + abs(high_delta)
        low = close - abs(low_delta) if close > abs(low_delta) else 0.01
        raw = {
            "time": 1_700_000_000,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "tradedValue": traded_value,  # present, but oi is NOT
            # oi intentionally absent
        }
        normaliser = Normaliser()
        result, is_valid, _ = normaliser.normalise_ohlcv(raw, "angel_one")
        if not is_valid:
            return  # invalid data skipped

        oi = result.get("oi")
        oi_missing = result.get("oiMissing", False)
        # OI must be null (None) when not supplied
        assert oi is None, f"oi must be null when not supplied by provider; got {oi}"
        # OI must NOT be populated from tradedValue
        assert oi != traded_value, "oi must never be populated from tradedValue"

    @given(
        positive_price,
        st.floats(min_value=1.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False),  # oi
        traded_value_st,
    )
    @settings(max_examples=100)
    def test_oi_and_traded_value_remain_distinct(
        self,
        close: float,
        oi: float,
        traded_value: float,
    ) -> None:
        """When both oi and tradedValue are present, they remain separate fields."""
        raw = {
            "time": 1_700_000_000,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1000.0,
            "oi": oi,
            "tradedValue": traded_value,
        }
        normaliser = Normaliser()
        result, is_valid, _ = normaliser.normalise_ohlcv(raw, "angel_one")
        if not is_valid:
            return
        # If OI is preserved, it must equal what was supplied (allowing int truncation for large values)
        if result.get("oi") is not None:
            assert int(result["oi"]) == int(oi), (
                f"Supplied OI {oi} must not be overwritten by tradedValue {traded_value}"
            )
        # tradedValue must not replace oi
        if "tradedValue" in result and result.get("oi") is not None:
            assert result["oi"] != result["tradedValue"] or oi == traded_value
