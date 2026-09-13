"""
Unit tests for src/core/validators/timestamps.py — pipeline step 4.

Covers:
- ISO-8601 strings (timezone-aware, naive, Z-suffix, +offset)
- Epoch seconds (int and float)
- Epoch milliseconds (int and float)
- datetime.datetime objects (aware and naive)
- datetime.date objects
- Numeric strings
- Invalid / unparseable inputs
- IST to UTC conversion correctness
- UTC passthrough
- format_api_timestamp produces correct Z-suffix ISO-8601 string
- Millisecond precision is preserved

Requirements: 6.7, 6.8, 17.3
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from src.core.validators.timestamps import (
    EXCHANGE_TIMEZONE_CRYPTO,
    EXCHANGE_TIMEZONE_INDIAN,
    format_api_timestamp,
    normalise_timestamp,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = datetime.timezone.utc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ist_epoch_ms(year, month, day, hour, minute, second, ms=0) -> int:
    """Return UTC epoch ms for the given IST wall-clock time."""
    dt = datetime.datetime(year, month, day, hour, minute, second,
                           ms * 1000, tzinfo=IST)
    return int(dt.timestamp() * 1000)


def _utc_epoch_ms(year, month, day, hour, minute, second, ms=0) -> int:
    dt = datetime.datetime(year, month, day, hour, minute, second,
                           ms * 1000, tzinfo=UTC)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# normalise_timestamp — success paths
# ---------------------------------------------------------------------------


class TestNormaliseTimestampSuccess:

    def test_iso8601_with_z_suffix(self):
        # 2026-01-15 09:15:00 UTC
        raw = "2026-01-15T09:15:00Z"
        ms, ok, err = normalise_timestamp(raw, "UTC")
        assert ok is True
        assert err == ""
        expected = _utc_epoch_ms(2026, 1, 15, 9, 15, 0)
        assert ms == expected

    def test_iso8601_with_plus_offset(self):
        # 2026-01-15 14:45:00+05:30 == 2026-01-15 09:15:00 UTC
        raw = "2026-01-15T14:45:00+05:30"
        ms, ok, err = normalise_timestamp(raw, "UTC")
        assert ok is True
        expected = _utc_epoch_ms(2026, 1, 15, 9, 15, 0)
        assert ms == expected

    def test_iso8601_naive_string_treated_as_ist(self):
        # A naive string "2026-01-15T09:15:00" with IST timezone means
        # 09:15 IST → 03:45 UTC
        raw = "2026-01-15T09:15:00"
        ms, ok, err = normalise_timestamp(raw, EXCHANGE_TIMEZONE_INDIAN)
        assert ok is True
        expected = _ist_epoch_ms(2026, 1, 15, 9, 15, 0)
        assert ms == expected

    def test_iso8601_naive_string_treated_as_utc_for_crypto(self):
        raw = "2026-01-15T09:15:00"
        ms, ok, err = normalise_timestamp(raw, EXCHANGE_TIMEZONE_CRYPTO)
        assert ok is True
        expected = _utc_epoch_ms(2026, 1, 15, 9, 15, 0)
        assert ms == expected

    def test_iso8601_with_milliseconds(self):
        raw = "2026-01-15T09:15:00.123Z"
        ms, ok, err = normalise_timestamp(raw, "UTC")
        assert ok is True
        expected = _utc_epoch_ms(2026, 1, 15, 9, 15, 0, 123)
        assert ms == expected

    def test_epoch_seconds_int(self):
        # 1_705_300_000 seconds → well-known timestamp
        epoch_sec = 1_705_300_000
        ms, ok, err = normalise_timestamp(epoch_sec)
        assert ok is True
        assert ms == epoch_sec * 1000

    def test_epoch_seconds_float(self):
        epoch_sec = 1_705_300_000.5
        ms, ok, err = normalise_timestamp(epoch_sec)
        assert ok is True
        assert ms == int(epoch_sec * 1000)

    def test_epoch_milliseconds_int(self):
        # Value >= 1e10 is treated as epoch ms
        epoch_ms = 1_705_300_000_000
        ms, ok, err = normalise_timestamp(epoch_ms)
        assert ok is True
        assert ms == epoch_ms

    def test_epoch_milliseconds_float(self):
        epoch_ms = 1_705_300_000_123.0
        ms, ok, err = normalise_timestamp(epoch_ms)
        assert ok is True
        assert ms == int(epoch_ms)

    def test_datetime_aware_utc(self):
        dt = datetime.datetime(2026, 1, 15, 9, 15, 0, tzinfo=UTC)
        ms, ok, err = normalise_timestamp(dt)
        assert ok is True
        assert ms == _utc_epoch_ms(2026, 1, 15, 9, 15, 0)

    def test_datetime_aware_ist(self):
        dt = datetime.datetime(2026, 1, 15, 14, 45, 0, tzinfo=IST)
        ms, ok, err = normalise_timestamp(dt)
        assert ok is True
        # 14:45 IST == 09:15 UTC
        assert ms == _utc_epoch_ms(2026, 1, 15, 9, 15, 0)

    def test_datetime_naive_localised_to_ist(self):
        dt = datetime.datetime(2026, 1, 15, 9, 15, 0)  # naive
        ms, ok, err = normalise_timestamp(dt, EXCHANGE_TIMEZONE_INDIAN)
        assert ok is True
        expected = _ist_epoch_ms(2026, 1, 15, 9, 15, 0)
        assert ms == expected

    def test_date_object_midnight_ist(self):
        d = datetime.date(2026, 1, 15)
        ms, ok, err = normalise_timestamp(d, EXCHANGE_TIMEZONE_INDIAN)
        assert ok is True
        # midnight IST on 2026-01-15
        expected = _ist_epoch_ms(2026, 1, 15, 0, 0, 0)
        assert ms == expected

    def test_date_object_midnight_utc(self):
        d = datetime.date(2026, 1, 15)
        ms, ok, err = normalise_timestamp(d, EXCHANGE_TIMEZONE_CRYPTO)
        assert ok is True
        expected = _utc_epoch_ms(2026, 1, 15, 0, 0, 0)
        assert ms == expected

    def test_numeric_string_seconds(self):
        raw = "1705300000"
        ms, ok, err = normalise_timestamp(raw)
        assert ok is True
        assert ms == 1_705_300_000 * 1000

    def test_numeric_string_milliseconds(self):
        raw = "1705300000000"
        ms, ok, err = normalise_timestamp(raw)
        assert ok is True
        assert ms == 1_705_300_000_000

    def test_plain_date_string(self):
        raw = "2026-01-15"
        ms, ok, err = normalise_timestamp(raw, EXCHANGE_TIMEZONE_CRYPTO)
        assert ok is True
        expected = _utc_epoch_ms(2026, 1, 15, 0, 0, 0)
        assert ms == expected

    def test_string_with_space_separator(self):
        # "2026-01-15 09:15:00Z" — space instead of T
        raw = "2026-01-15 09:15:00Z"
        ms, ok, err = normalise_timestamp(raw, "UTC")
        assert ok is True
        expected = _utc_epoch_ms(2026, 1, 15, 9, 15, 0)
        assert ms == expected

    def test_ist_utc_offset_is_330_minutes(self):
        """Verify IST is exactly UTC+05:30."""
        # 09:15 IST should be 03:45 UTC
        raw_ist = "2026-01-15T09:15:00"
        ms_ist, ok_ist, _ = normalise_timestamp(raw_ist, EXCHANGE_TIMEZONE_INDIAN)
        assert ok_ist
        raw_utc = "2026-01-15T03:45:00Z"
        ms_utc, ok_utc, _ = normalise_timestamp(raw_utc, "UTC")
        assert ok_utc
        assert ms_ist == ms_utc


# ---------------------------------------------------------------------------
# normalise_timestamp — failure paths
# ---------------------------------------------------------------------------


class TestNormaliseTimestampFailure:

    def test_none_input(self):
        ms, ok, err = normalise_timestamp(None)
        assert ok is False
        assert ms == 0
        assert "None" in err

    def test_empty_string(self):
        ms, ok, err = normalise_timestamp("")
        assert ok is False
        assert ms == 0
        assert err != ""

    def test_garbage_string(self):
        ms, ok, err = normalise_timestamp("not-a-date-at-all!!!")
        assert ok is False
        assert ms == 0
        assert err != ""

    def test_unsupported_type_list(self):
        ms, ok, err = normalise_timestamp([2026, 1, 15])
        assert ok is False
        assert ms == 0
        assert "list" in err

    def test_unsupported_type_dict(self):
        ms, ok, err = normalise_timestamp({"year": 2026})
        assert ok is False
        assert ms == 0

    def test_partial_iso_string_missing_time(self):
        # "2026-01" is not a valid date nor ISO-8601 datetime
        ms, ok, err = normalise_timestamp("2026-01")
        assert ok is False
        assert ms == 0


# ---------------------------------------------------------------------------
# format_api_timestamp
# ---------------------------------------------------------------------------


class TestFormatApiTimestamp:

    def test_basic_format(self):
        # 2026-01-15 09:15:00.000 UTC
        epoch_ms = _utc_epoch_ms(2026, 1, 15, 9, 15, 0)
        result = format_api_timestamp(epoch_ms)
        assert result == "2026-01-15T09:15:00.000Z"

    def test_millisecond_precision(self):
        epoch_ms = _utc_epoch_ms(2026, 1, 15, 9, 15, 0, 123)
        result = format_api_timestamp(epoch_ms)
        assert result.endswith("Z")
        assert ".123Z" in result

    def test_always_has_z_suffix(self):
        for epoch_ms in [0, 1_705_300_000_000, 1_705_300_000_999]:
            result = format_api_timestamp(epoch_ms)
            assert result.endswith("Z"), f"Missing Z in: {result}"

    def test_epoch_zero(self):
        result = format_api_timestamp(0)
        assert result == "1970-01-01T00:00:00.000Z"

    def test_roundtrip(self):
        # Parse the output of format_api_timestamp back to epoch ms and verify.
        epoch_ms = _utc_epoch_ms(2026, 6, 15, 12, 30, 45, 500)
        formatted = format_api_timestamp(epoch_ms)
        # Parse back
        parsed = datetime.datetime.fromisoformat(formatted.replace("Z", "+00:00"))
        recovered_ms = int(parsed.timestamp() * 1000)
        assert recovered_ms == epoch_ms

    def test_three_decimal_places(self):
        epoch_ms = _utc_epoch_ms(2026, 1, 15, 9, 15, 0, 7)
        result = format_api_timestamp(epoch_ms)
        # Should have exactly 3 decimal digits
        time_part = result.split("T")[1]
        decimal_part = time_part.split(".")[1].rstrip("Z")
        assert len(decimal_part) == 3, f"Expected 3 decimal digits, got: {result}"
