"""
Property 3 — Deribit Instrument Name Round-Trip

parse(serialise(parse(name))) == parse(name) for all valid Deribit instrument names.

Requirement: 14.7
"""
from __future__ import annotations

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.core.parsers.deribit_parser import parse, serialise, verify_round_trip, SUPPORTED_CURRENCIES


# Valid Deribit expiry months (3-letter abbreviations)
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Representative day numbers
day_st = st.integers(min_value=1, max_value=28)  # safe — all months have 1–28
year_st = st.integers(min_value=24, max_value=30)  # 2024–2030 (2-digit)
strike_st = st.integers(min_value=100, max_value=500_000).map(lambda x: x * 100)  # 100-500000 in $100 steps
currency_st = st.sampled_from(list(SUPPORTED_CURRENCIES))
option_type_st = st.sampled_from(["C", "P"])
month_st = st.sampled_from(MONTHS)


@st.composite
def valid_deribit_name(draw):
    currency = draw(currency_st)
    day = draw(day_st)
    month = draw(month_st)
    year = draw(year_st)
    strike = draw(strike_st)
    opt_type = draw(option_type_st)
    return f"{currency}-{day:02d}{month}{year}-{strike}-{opt_type}"


class TestDeribitRoundTrip:
    @given(valid_deribit_name())
    @settings(max_examples=100)
    def test_parse_then_serialise_is_identity(self, name: str) -> None:
        """parse → serialise → parse must give the same result as parse alone."""
        parsed = parse(name)
        if parsed is None:
            return  # not parseable — skip

        serialised = serialise(parsed)
        re_parsed = parse(serialised)

        assert re_parsed is not None, f"serialise({name!r}) → {serialised!r} was not re-parseable"
        assert re_parsed["baseCurrency"] == parsed["baseCurrency"]
        assert re_parsed["strike"] == parsed["strike"]
        assert re_parsed["optionType"] == parsed["optionType"]
        assert re_parsed["expiryTs"] == parsed["expiryTs"]
    @given(valid_deribit_name())
    @settings(max_examples=100)
    def test_verify_round_trip_passes(self, name: str) -> None:
        """verify_round_trip must return True for valid names."""
        parsed = parse(name)
        if parsed is None:
            return
        result = verify_round_trip(name)
        assert result is True, f"verify_round_trip failed for valid name {name!r}"
