"""
Unit tests for Deribit instrument name parser.

Covers:
- Parsing valid instrument names for BTC, ETH, SOL
- Parsing various strike formats (integer and decimal)
- Option type mapping: C → CE, P → PE
- expiryTs is UTC epoch ms at 08:00 UTC on expiry date
- Serialise round-trip produces identical string for all supported currencies
- verify_round_trip returns True for all valid names
- parse returns None for invalid/malformed names
- serialise raises ValueError on missing/invalid fields
- Edge cases: day/month boundaries, leading zeros in day

Requirements: 14.3, 14.7
"""

from __future__ import annotations

import datetime

import pytest

from src.core.parsers.deribit_parser import (
    SUPPORTED_CURRENCIES,
    parse,
    serialise,
    verify_round_trip,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_epoch_ms(year: int, month: int, day: int, hour: int = 8) -> int:
    """Return UTC epoch ms for the given date at ``hour`` UTC."""
    dt = datetime.datetime(year, month, day, hour, 0, 0, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# parse() — valid inputs
# ---------------------------------------------------------------------------


class TestParseValid:
    def test_btc_call_basic(self) -> None:
        result = parse("BTC-27JAN23-20000-C")
        assert result is not None
        assert result["baseCurrency"] == "BTC"
        assert result["optionType"] == "CE"
        assert result["strike"] == 20000.0
        assert result["expiryTs"] == _utc_epoch_ms(2023, 1, 27)

    def test_eth_put_basic(self) -> None:
        result = parse("ETH-03JUN24-2500-P")
        assert result is not None
        assert result["baseCurrency"] == "ETH"
        assert result["optionType"] == "PE"
        assert result["strike"] == 2500.0
        assert result["expiryTs"] == _utc_epoch_ms(2024, 6, 3)

    def test_sol_call(self) -> None:
        result = parse("SOL-15MAR25-150-C")
        assert result is not None
        assert result["baseCurrency"] == "SOL"
        assert result["optionType"] == "CE"
        assert result["strike"] == 150.0

    def test_decimal_strike(self) -> None:
        result = parse("BTC-10DEC23-25000.5-C")
        assert result is not None
        assert result["strike"] == 25000.5

    def test_integer_strike_stored_as_float(self) -> None:
        result = parse("BTC-10DEC23-30000-C")
        assert result is not None
        assert isinstance(result["strike"], float)
        assert result["strike"] == 30000.0

    def test_day_zero_padded_01(self) -> None:
        result = parse("ETH-01JAN24-1000-P")
        assert result is not None
        assert result["expiryTs"] == _utc_epoch_ms(2024, 1, 1)

    def test_expiry_ts_is_08_utc(self) -> None:
        result = parse("BTC-15SEP25-40000-C")
        assert result is not None
        dt = datetime.datetime.fromtimestamp(result["expiryTs"] / 1000, tz=datetime.timezone.utc)
        assert dt.hour == 8
        assert dt.minute == 0
        assert dt.second == 0

    @pytest.mark.parametrize("currency", ["BTC", "ETH", "SOL"])
    def test_all_supported_currencies_parse(self, currency: str) -> None:
        name = f"{currency}-15JAN25-1000-C"
        result = parse(name)
        assert result is not None
        assert result["baseCurrency"] == currency

    @pytest.mark.parametrize("month_str,expected_month", [
        ("JAN", 1), ("FEB", 2), ("MAR", 3), ("APR", 4),
        ("MAY", 5), ("JUN", 6), ("JUL", 7), ("AUG", 8),
        ("SEP", 9), ("OCT", 10), ("NOV", 11), ("DEC", 12),
    ])
    def test_all_month_abbreviations(self, month_str: str, expected_month: int) -> None:
        name = f"BTC-15{month_str}25-20000-C"
        result = parse(name)
        assert result is not None
        dt = datetime.datetime.fromtimestamp(result["expiryTs"] / 1000, tz=datetime.timezone.utc)
        assert dt.month == expected_month

    def test_call_option_type_mapped_to_ce(self) -> None:
        result = parse("BTC-15JAN25-20000-C")
        assert result is not None
        assert result["optionType"] == "CE"

    def test_put_option_type_mapped_to_pe(self) -> None:
        result = parse("BTC-15JAN25-20000-P")
        assert result is not None
        assert result["optionType"] == "PE"

    def test_result_has_all_four_keys(self) -> None:
        result = parse("BTC-15JAN25-20000-C")
        assert result is not None
        assert set(result.keys()) == {"baseCurrency", "expiryTs", "strike", "optionType"}

    def test_large_strike_value(self) -> None:
        result = parse("BTC-15JAN25-100000-C")
        assert result is not None
        assert result["strike"] == 100000.0


# ---------------------------------------------------------------------------
# parse() — invalid / edge case inputs
# ---------------------------------------------------------------------------


class TestParseInvalid:
    def test_none_returns_none(self) -> None:
        assert parse(None) is None  # type: ignore[arg-type]

    def test_empty_string_returns_none(self) -> None:
        assert parse("") is None

    def test_missing_separator_returns_none(self) -> None:
        assert parse("BTC27JAN2320000C") is None

    def test_invalid_month_abbreviation_returns_none(self) -> None:
        assert parse("BTC-15XYZ23-20000-C") is None

    def test_invalid_option_type_returns_none(self) -> None:
        assert parse("BTC-15JAN23-20000-X") is None

    def test_non_string_type_returns_none(self) -> None:
        assert parse(12345) is None  # type: ignore[arg-type]

    def test_invalid_day_31_in_feb_returns_none(self) -> None:
        # Feb 31 doesn't exist
        assert parse("BTC-31FEB23-20000-C") is None

    def test_wrong_number_of_parts_returns_none(self) -> None:
        assert parse("BTC-15JAN23-C") is None
        assert parse("BTC-15JAN23-20000") is None
        assert parse("BTC-15JAN23") is None

    def test_negative_strike_returns_none(self) -> None:
        # Regex won't match negative numbers (no leading -)
        assert parse("BTC-15JAN23--20000-C") is None

    def test_extra_parts_returns_none(self) -> None:
        assert parse("BTC-15JAN23-20000-C-EXTRA") is None


# ---------------------------------------------------------------------------
# serialise() — valid inputs
# ---------------------------------------------------------------------------


class TestSerialiseValid:
    def _btc_call(self) -> dict:
        return {
            "baseCurrency": "BTC",
            "expiryTs": _utc_epoch_ms(2023, 1, 27),
            "strike": 20000.0,
            "optionType": "CE",
        }

    def test_serialise_btc_call(self) -> None:
        result = serialise(self._btc_call())
        assert result == "BTC-27JAN23-20000-C"

    def test_serialise_eth_put(self) -> None:
        canonical = {
            "baseCurrency": "ETH",
            "expiryTs": _utc_epoch_ms(2024, 6, 3),
            "strike": 2500.0,
            "optionType": "PE",
        }
        result = serialise(canonical)
        assert result == "ETH-03JUN24-2500-P"

    def test_ce_maps_to_c(self) -> None:
        c = {**self._btc_call(), "optionType": "CE"}
        assert serialise(c).endswith("-C")

    def test_pe_maps_to_p(self) -> None:
        c = {**self._btc_call(), "optionType": "PE"}
        assert serialise(c).endswith("-P")

    def test_integer_strike_has_no_decimal(self) -> None:
        c = {**self._btc_call(), "strike": 30000.0}
        name = serialise(c)
        assert "-30000-" in name
        assert ".0" not in name

    def test_decimal_strike_preserved(self) -> None:
        c = {**self._btc_call(), "strike": 25000.5}
        name = serialise(c)
        assert "-25000.5-" in name

    def test_day_is_zero_padded(self) -> None:
        c = {**self._btc_call(), "expiryTs": _utc_epoch_ms(2024, 6, 1)}
        name = serialise(c)
        # Should be "01JUN24" not "1JUN24"
        assert "-01JUN24-" in name

    def test_month_is_uppercase_3_letter(self) -> None:
        c = {**self._btc_call(), "expiryTs": _utc_epoch_ms(2024, 11, 15)}
        name = serialise(c)
        assert "-15NOV24-" in name

    def test_year_is_2_digit(self) -> None:
        c = {**self._btc_call(), "expiryTs": _utc_epoch_ms(2030, 3, 10)}
        name = serialise(c)
        assert "-10MAR30-" in name

    def test_year_2000_edge_case(self) -> None:
        c = {**self._btc_call(), "expiryTs": _utc_epoch_ms(2000, 1, 15)}
        name = serialise(c)
        assert "-15JAN00-" in name


# ---------------------------------------------------------------------------
# serialise() — invalid inputs
# ---------------------------------------------------------------------------


class TestSerialiseInvalid:
    def test_raises_on_missing_base_currency(self) -> None:
        with pytest.raises(ValueError, match="baseCurrency"):
            serialise({"expiryTs": 0, "strike": 20000.0, "optionType": "CE"})

    def test_raises_on_missing_expiry_ts(self) -> None:
        with pytest.raises(ValueError, match="expiryTs"):
            serialise({"baseCurrency": "BTC", "strike": 20000.0, "optionType": "CE"})

    def test_raises_on_missing_strike(self) -> None:
        with pytest.raises(ValueError, match="strike"):
            serialise({"baseCurrency": "BTC", "expiryTs": 0, "optionType": "CE"})

    def test_raises_on_missing_option_type(self) -> None:
        with pytest.raises(ValueError, match="optionType"):
            serialise({"baseCurrency": "BTC", "expiryTs": 0, "strike": 20000.0})

    def test_raises_on_invalid_option_type(self) -> None:
        with pytest.raises(ValueError, match="optionType"):
            serialise({
                "baseCurrency": "BTC",
                "expiryTs": _utc_epoch_ms(2024, 1, 15),
                "strike": 20000.0,
                "optionType": "CALL",
            })

    def test_raises_on_none_option_type(self) -> None:
        with pytest.raises(ValueError):
            serialise({
                "baseCurrency": "BTC",
                "expiryTs": _utc_epoch_ms(2024, 1, 15),
                "strike": 20000.0,
                "optionType": None,
            })


# ---------------------------------------------------------------------------
# Round-trip: parse → serialise → parse
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize("name", [
        "BTC-27JAN23-20000-C",
        "ETH-03JUN24-2500-P",
        "SOL-15MAR25-150-C",
        "BTC-01DEC24-30000-P",
        "ETH-31MAY25-3000-C",
        "BTC-10NOV24-25000.5-C",
    ])
    def test_parse_serialise_parse_equals_parse(self, name: str) -> None:
        first = parse(name)
        assert first is not None, f"parse failed for {name!r}"
        reserialized = serialise(first)
        second = parse(reserialized)
        assert first == second, (
            f"Round-trip failed:\n"
            f"  original:     {name!r}\n"
            f"  first parse:  {first}\n"
            f"  re-serialised: {reserialized!r}\n"
            f"  second parse: {second}"
        )

    @pytest.mark.parametrize("name", [
        "BTC-27JAN23-20000-C",
        "ETH-03JUN24-2500-P",
        "SOL-15MAR25-150-C",
    ])
    def test_verify_round_trip_returns_true_for_valid(self, name: str) -> None:
        assert verify_round_trip(name) is True

    def test_verify_round_trip_returns_false_for_invalid(self) -> None:
        assert verify_round_trip("NOT-A-VALID-INSTRUMENT") is False

    def test_serialise_output_parses_to_same_currency(self) -> None:
        canonical = {
            "baseCurrency": "ETH",
            "expiryTs": _utc_epoch_ms(2025, 9, 26),
            "strike": 4000.0,
            "optionType": "CE",
        }
        name = serialise(canonical)
        reparsed = parse(name)
        assert reparsed is not None
        assert reparsed["baseCurrency"] == "ETH"
        assert reparsed["optionType"] == "CE"
        assert reparsed["strike"] == 4000.0

    def test_serialise_output_parses_to_same_expiry(self) -> None:
        canonical = {
            "baseCurrency": "BTC",
            "expiryTs": _utc_epoch_ms(2025, 12, 26),
            "strike": 50000.0,
            "optionType": "PE",
        }
        name = serialise(canonical)
        reparsed = parse(name)
        assert reparsed is not None
        assert reparsed["expiryTs"] == canonical["expiryTs"]

    def test_verify_round_trip_false_for_empty_string(self) -> None:
        assert verify_round_trip("") is False

    def test_verify_round_trip_false_for_none(self) -> None:
        assert verify_round_trip(None) is False  # type: ignore[arg-type]

    @pytest.mark.parametrize("currency", ["BTC", "ETH", "SOL"])
    def test_all_supported_currencies_round_trip(self, currency: str) -> None:
        name = f"{currency}-15JUN25-1000-C"
        assert verify_round_trip(name) is True
