"""
Unit tests for the round-trip parser property utility.

Covers:
- verify_round_trip() functional interface: (True, "") on success
- verify_round_trip() returns (False, msg) when property violated
- verify_round_trip() returns (False, msg) when parse returns None
- verify_round_trip() handles parser/serialiser exceptions gracefully
- RoundTripProperty.check() returns RoundTripResult with correct fields
- RoundTripProperty.check() ok=True when property holds
- RoundTripProperty.check() ok=False with error message when violated
- JSON round-trip: dict → json str → dict is stable
- Placeholder stubs (protobuf, bhavcopy, nse_charting) function correctly
  with dict inputs
- Pre-built instances (JSON_ROUND_TRIP, etc.) are properly wired

Requirements: 4.11, 17.8
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from src.core.parsers.round_trip import (
    BHAVCOPY_ROUND_TRIP,
    JSON_ROUND_TRIP,
    NSE_CHARTING_ROUND_TRIP,
    PROTOBUF_ROUND_TRIP,
    RoundTripProperty,
    RoundTripResult,
    bhavcopy_parser,
    bhavcopy_serialiser,
    json_parser,
    json_serialiser,
    nse_charting_parser,
    nse_charting_serialiser,
    protobuf_parser,
    protobuf_serialiser,
    verify_round_trip,
)


# ---------------------------------------------------------------------------
# Helpers — simple identity parser/serialiser pair for testing
# ---------------------------------------------------------------------------


def _identity_parser(raw: Any) -> Optional[Any]:
    """Returns raw unchanged (simulates a no-op parser)."""
    return raw


def _identity_serialiser(canonical: Any) -> Any:
    """Returns canonical unchanged."""
    return canonical


def _none_parser(raw: Any) -> Optional[Any]:
    """Always returns None (simulates a failing parser)."""
    return None


def _mutation_parser(raw: Any) -> Optional[dict]:
    """Parser that produces DIFFERENT output for string vs dict input,
    simulating a broken round-trip.

    parse("test_value")           → {"value": "test_value", "extra": "added"}
    parse(serialise(parse(...)))  → serialise returns "test_value" string,
                                    but parse("test_value") → {"value": ..., "extra": "added"}

    Actually both produce the same dict from a string, so we need to
    break the round-trip by using different logic for string vs dict:
    - parse(str) → {"value": str, "version": 1}    (adds version)
    - parse(dict with version=1) → {"value": ..., "version": 2}  (increments version)
    """
    if isinstance(raw, str):
        return {"value": raw, "version": 1}
    if isinstance(raw, dict):
        # Simulate a parsing mutation: each parse of a dict increments version
        return {"value": raw.get("value", ""), "version": raw.get("version", 0) + 1}
    return None


def _mutation_serialiser(canonical: dict) -> dict:
    """Serialise dict passthrough — returns the canonical dict as-is.

    This means parse(serialise(parse(raw))) will parse the dict with the
    incremented-version path, producing a different result from parse(raw).
    """
    return dict(canonical)


def _raising_parser(raw: Any) -> Optional[Any]:
    raise RuntimeError("parser failed")


def _raising_serialiser(canonical: Any) -> Any:
    raise RuntimeError("serialiser failed")


# ---------------------------------------------------------------------------
# verify_round_trip() — functional interface
# ---------------------------------------------------------------------------


class TestVerifyRoundTripFunction:
    def test_identity_parser_returns_true(self) -> None:
        ok, err = verify_round_trip("hello", _identity_parser, _identity_serialiser)
        assert ok is True
        assert err == ""

    def test_returns_false_when_parser_returns_none(self) -> None:
        ok, err = verify_round_trip("anything", _none_parser, _identity_serialiser)
        assert ok is False
        assert err != ""

    def test_returns_false_when_round_trip_broken(self) -> None:
        ok, err = verify_round_trip(
            "test_value", _mutation_parser, _mutation_serialiser
        )
        assert ok is False
        assert err != ""

    def test_returns_false_when_parser_raises(self) -> None:
        ok, err = verify_round_trip("x", _raising_parser, _identity_serialiser)
        assert ok is False
        assert "parser raised" in err.lower() or err != ""

    def test_returns_false_when_serialiser_raises(self) -> None:
        ok, err = verify_round_trip("x", _identity_parser, _raising_serialiser)
        assert ok is False
        assert err != ""

    def test_returns_false_when_reparse_raises(self) -> None:
        call_count = [0]

        def sometimes_raising_parser(raw: Any) -> Optional[Any]:
            call_count[0] += 1
            if call_count[0] == 2:  # second call raises
                raise RuntimeError("reparse failed")
            return raw

        ok, err = verify_round_trip("x", sometimes_raising_parser, _identity_serialiser)
        assert ok is False

    def test_error_message_is_empty_string_on_success(self) -> None:
        ok, err = verify_round_trip({"a": 1}, _identity_parser, _identity_serialiser)
        assert ok is True
        assert err == ""

    def test_error_message_is_non_empty_on_failure(self) -> None:
        ok, err = verify_round_trip("bad", _none_parser, _identity_serialiser)
        assert ok is False
        assert len(err) > 0

    def test_none_input_with_none_returning_parser(self) -> None:
        ok, err = verify_round_trip(None, _none_parser, _identity_serialiser)
        assert ok is False


# ---------------------------------------------------------------------------
# RoundTripProperty.check() — class interface
# ---------------------------------------------------------------------------


class TestRoundTripPropertyCheck:
    def _make_prop(self, parser=None, serialiser=None, name="test") -> RoundTripProperty:
        p = parser or _identity_parser
        s = serialiser or _identity_serialiser
        return RoundTripProperty(parser=p, serialiser=s, name=name)

    def test_check_returns_round_trip_result(self) -> None:
        prop = self._make_prop()
        result = prop.check("hello")
        assert isinstance(result, RoundTripResult)

    def test_check_ok_true_on_identity_parser(self) -> None:
        prop = self._make_prop()
        result = prop.check("hello")
        assert result.ok is True
        assert result.error == ""

    def test_check_ok_false_when_parser_returns_none(self) -> None:
        prop = self._make_prop(parser=_none_parser)
        result = prop.check("anything")
        assert result.ok is False
        assert result.error != ""

    def test_check_ok_false_when_round_trip_broken(self) -> None:
        prop = self._make_prop(
            parser=_mutation_parser, serialiser=_mutation_serialiser
        )
        result = prop.check("test_value")
        assert result.ok is False
        assert result.error != ""

    def test_check_preserves_raw_in_result(self) -> None:
        prop = self._make_prop()
        raw = {"key": "value"}
        result = prop.check(raw)
        assert result.raw == raw

    def test_check_ok_result_has_both_parses(self) -> None:
        prop = self._make_prop()
        result = prop.check({"x": 1})
        assert result.ok is True
        assert result.first_parse is not None
        assert result.second_parse is not None
        assert result.first_parse == result.second_parse

    def test_check_stores_first_parse_on_serialiser_failure(self) -> None:
        prop = self._make_prop(serialiser=_raising_serialiser)
        result = prop.check("hello")
        assert result.ok is False
        assert result.first_parse is not None  # first parse succeeded

    def test_name_property(self) -> None:
        prop = RoundTripProperty(
            parser=_identity_parser,
            serialiser=_identity_serialiser,
            name="my_parser",
        )
        assert prop.name == "my_parser"

    def test_default_name_is_unnamed(self) -> None:
        prop = RoundTripProperty(
            parser=_identity_parser,
            serialiser=_identity_serialiser,
        )
        assert prop.name == "unnamed"

    def test_error_mentions_parser_name(self) -> None:
        prop = RoundTripProperty(
            parser=_none_parser,
            serialiser=_identity_serialiser,
            name="my_special_parser",
        )
        result = prop.check("bad input")
        assert result.ok is False
        assert "my_special_parser" in result.error


# ---------------------------------------------------------------------------
# RoundTripResult — immutability
# ---------------------------------------------------------------------------


class TestRoundTripResult:
    def test_result_is_frozen(self) -> None:
        result = RoundTripResult(ok=True, error="", raw="x")
        with pytest.raises((AttributeError, TypeError)):
            result.ok = False  # type: ignore[misc]

    def test_result_fields_accessible(self) -> None:
        result = RoundTripResult(
            ok=False, error="boom", raw="r", first_parse="fp", second_parse="sp"
        )
        assert result.ok is False
        assert result.error == "boom"
        assert result.raw == "r"
        assert result.first_parse == "fp"
        assert result.second_parse == "sp"


# ---------------------------------------------------------------------------
# JSON parser/serialiser
# ---------------------------------------------------------------------------


class TestJsonParserSerialiser:
    def test_json_parser_parses_string(self) -> None:
        result = json_parser('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_parser_copies_dict(self) -> None:
        d = {"a": 1}
        result = json_parser(d)
        assert result == d
        assert result is not d  # deep copy

    def test_json_parser_returns_none_for_invalid_json(self) -> None:
        assert json_parser("{invalid json}") is None

    def test_json_parser_returns_none_for_non_dict_json(self) -> None:
        assert json_parser("[1, 2, 3]") is None

    def test_json_parser_returns_none_for_integer(self) -> None:
        assert json_parser(42) is None

    def test_json_serialiser_produces_valid_json_string(self) -> None:
        d = {"key": "value", "num": 42}
        s = json_serialiser(d)
        assert isinstance(s, str)
        parsed = json.loads(s)
        assert parsed == d

    def test_json_serialiser_is_deterministic(self) -> None:
        d = {"z": 1, "a": 2, "m": 3}
        assert json_serialiser(d) == json_serialiser(d)

    def test_json_round_trip_property_holds(self) -> None:
        d = {"instrument": "NIFTY", "ltp": 22000.5, "volume": 12345}
        ok, err = verify_round_trip(d, json_parser, json_serialiser)
        assert ok is True, err

    def test_json_round_trip_property_holds_for_nested_dict(self) -> None:
        d = {"outer": {"inner": [1, 2, 3]}, "flag": True}
        ok, err = verify_round_trip(d, json_parser, json_serialiser)
        assert ok is True, err


# ---------------------------------------------------------------------------
# Placeholder stubs
# ---------------------------------------------------------------------------


class TestPlaceholderStubs:
    @pytest.mark.parametrize("parser,serialiser,name", [
        (protobuf_parser, protobuf_serialiser, "protobuf"),
        (bhavcopy_parser, bhavcopy_serialiser, "bhavcopy"),
        (nse_charting_parser, nse_charting_serialiser, "nse_charting"),
    ])
    def test_placeholder_round_trips_dict_input(
        self, parser, serialiser, name: str
    ) -> None:
        d = {"field": "value", "num": 42}
        ok, err = verify_round_trip(d, parser, serialiser)
        assert ok is True, f"[{name}] round-trip failed: {err}"

    @pytest.mark.parametrize("parser", [
        protobuf_parser, bhavcopy_parser, nse_charting_parser
    ])
    def test_placeholder_parsers_return_none_for_strings(self, parser) -> None:
        # Placeholders only handle dict input; strings return None
        result = parser("some raw string")
        assert result is None

    @pytest.mark.parametrize("serialiser", [
        protobuf_serialiser, bhavcopy_serialiser, nse_charting_serialiser
    ])
    def test_placeholder_serialisers_return_dict(self, serialiser) -> None:
        d = {"a": 1}
        result = serialiser(d)
        assert result == d
        assert result is not d  # deep copy

    def test_protobuf_parser_returns_copy_not_same_object(self) -> None:
        d = {"key": "value"}
        result = protobuf_parser(d)
        assert result == d
        assert result is not d


# ---------------------------------------------------------------------------
# Pre-built instances
# ---------------------------------------------------------------------------


class TestPrebuiltInstances:
    def test_json_round_trip_instance_works(self) -> None:
        result = JSON_ROUND_TRIP.check({"ltp": 100.0})
        assert result.ok is True

    def test_json_round_trip_instance_name(self) -> None:
        assert JSON_ROUND_TRIP.name == "json"

    def test_protobuf_round_trip_instance_name(self) -> None:
        assert PROTOBUF_ROUND_TRIP.name == "protobuf"

    def test_bhavcopy_round_trip_instance_name(self) -> None:
        assert BHAVCOPY_ROUND_TRIP.name == "bhavcopy"

    def test_nse_charting_round_trip_instance_name(self) -> None:
        assert NSE_CHARTING_ROUND_TRIP.name == "nse_charting"

    def test_protobuf_round_trip_instance_works_with_dict(self) -> None:
        result = PROTOBUF_ROUND_TRIP.check({"msg": "tick"})
        assert result.ok is True

    def test_bhavcopy_round_trip_instance_works_with_dict(self) -> None:
        result = BHAVCOPY_ROUND_TRIP.check({"symbol": "RELIANCE", "close": 2500.0})
        assert result.ok is True

    def test_nse_charting_round_trip_instance_works_with_dict(self) -> None:
        result = NSE_CHARTING_ROUND_TRIP.check({"candle": [100, 105, 98, 102]})
        assert result.ok is True


# ---------------------------------------------------------------------------
# Deribit parser integration — verify the property holds end-to-end
# ---------------------------------------------------------------------------


class TestDeribitParserRoundTripIntegration:
    """Verify the Deribit parser/serialiser satisfies the round-trip property
    when used with the RoundTripProperty infrastructure.

    Requirements: 14.7, 4.11, 17.8
    """

    @pytest.fixture(autouse=True)
    def _setup_deribit_prop(self) -> None:
        from src.core.parsers.deribit_parser import parse as dp, serialise as ds
        self.prop = RoundTripProperty(parser=dp, serialiser=ds, name="deribit")

    @pytest.mark.parametrize("name", [
        "BTC-27JAN23-20000-C",
        "ETH-03JUN24-2500-P",
        "SOL-15MAR25-150-C",
        "BTC-01DEC24-30000-P",
    ])
    def test_deribit_round_trip_via_property_class(self, name: str) -> None:
        result = self.prop.check(name)
        assert result.ok is True, f"Deribit round-trip failed for {name!r}: {result.error}"

    def test_deribit_invalid_name_gives_false(self) -> None:
        result = self.prop.check("NOT-VALID")
        assert result.ok is False
