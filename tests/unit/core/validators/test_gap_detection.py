"""
Unit tests for src/core/validators/gap_detection.py — pipeline step 7.

Covers:
- INTERVAL_SECONDS mapping: all 9 canonical intervals present, values correct
- classify_gap_severity: all three categories (LOW=1, MEDIUM=2-5, HIGH>5)
- classify_gap_severity: boundary values (1, 2, 5, 6)
- classify_gap_severity: raises ValueError for < 1
- detect_candle_gaps: no gap returns empty list
- detect_candle_gaps: single gap detected with correct fields
- detect_candle_gaps: multiple gaps in one sequence
- detect_candle_gaps: gap severity LOW (1 missing)
- detect_candle_gaps: gap severity MEDIUM (2–5 missing)
- detect_candle_gaps: gap severity HIGH (>5 missing)
- detect_candle_gaps: fewer than 2 candles → empty list
- detect_candle_gaps: unsupported interval raises ValueError
- detect_candle_gaps: candle without 'time' field raises ValueError
- detect_candle_gaps: accepts epoch milliseconds (values >= 1e10)
- detect_candle_gaps: gap start / end times are correct in milliseconds
- 3m interval is not in INTERVAL_SECONDS (Indian market ban)

Requirements: 17.5
"""

from __future__ import annotations

import pytest

from src.core.validators.gap_detection import (
    INTERVAL_SECONDS,
    DataGapEvent,
    classify_gap_severity,
    detect_candle_gaps,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_candles_no_gap(start_epoch_sec: int, count: int, interval_sec: int) -> list[dict]:
    """Generate a gapless sequence of candles."""
    return [{"time": start_epoch_sec + i * interval_sec} for i in range(count)]


# ---------------------------------------------------------------------------
# INTERVAL_SECONDS
# ---------------------------------------------------------------------------


class TestIntervalSeconds:

    def test_all_nine_canonical_intervals_present(self):
        expected = {"1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"}
        assert expected == set(INTERVAL_SECONDS.keys())

    def test_3m_not_present(self):
        """3m interval is permanently banned for Indian market data."""
        assert "3m" not in INTERVAL_SECONDS

    def test_1m_is_60_seconds(self):
        assert INTERVAL_SECONDS["1m"] == 60

    def test_5m_is_300_seconds(self):
        assert INTERVAL_SECONDS["5m"] == 300

    def test_10m_is_600_seconds(self):
        assert INTERVAL_SECONDS["10m"] == 600

    def test_15m_is_900_seconds(self):
        assert INTERVAL_SECONDS["15m"] == 900

    def test_30m_is_1800_seconds(self):
        assert INTERVAL_SECONDS["30m"] == 1800

    def test_1h_is_3600_seconds(self):
        assert INTERVAL_SECONDS["1h"] == 3600

    def test_1d_is_86400_seconds(self):
        assert INTERVAL_SECONDS["1d"] == 86400

    def test_1w_is_604800_seconds(self):
        assert INTERVAL_SECONDS["1w"] == 604_800

    def test_1M_is_2592000_seconds(self):
        assert INTERVAL_SECONDS["1M"] == 2_592_000


# ---------------------------------------------------------------------------
# classify_gap_severity
# ---------------------------------------------------------------------------


class TestClassifyGapSeverity:

    def test_one_missing_is_low(self):
        assert classify_gap_severity(1) == "LOW"

    def test_two_missing_is_medium(self):
        assert classify_gap_severity(2) == "MEDIUM"

    def test_five_missing_is_medium(self):
        assert classify_gap_severity(5) == "MEDIUM"

    def test_six_missing_is_high(self):
        assert classify_gap_severity(6) == "HIGH"

    def test_large_count_is_high(self):
        assert classify_gap_severity(1000) == "HIGH"

    def test_zero_raises_value_error(self):
        with pytest.raises(ValueError):
            classify_gap_severity(0)

    def test_negative_raises_value_error(self):
        with pytest.raises(ValueError):
            classify_gap_severity(-1)

    def test_boundary_five_is_medium_not_high(self):
        assert classify_gap_severity(5) == "MEDIUM"

    def test_boundary_six_is_high_not_medium(self):
        assert classify_gap_severity(6) == "HIGH"


# ---------------------------------------------------------------------------
# detect_candle_gaps — no-gap cases
# ---------------------------------------------------------------------------


class TestDetectCandleGapsNoGap:

    def test_perfectly_consecutive_1m_candles(self):
        candles = make_candles_no_gap(1_705_300_000, 10, 60)
        gaps = detect_candle_gaps(candles, "1m")
        assert gaps == []

    def test_perfectly_consecutive_5m_candles(self):
        candles = make_candles_no_gap(1_705_300_000, 5, 300)
        gaps = detect_candle_gaps(candles, "5m")
        assert gaps == []

    def test_single_candle_no_gap(self):
        candles = [{"time": 1_705_300_000}]
        gaps = detect_candle_gaps(candles, "1m")
        assert gaps == []

    def test_empty_candle_list(self):
        gaps = detect_candle_gaps([], "1m")
        assert gaps == []

    def test_two_candles_exactly_one_apart(self):
        candles = [{"time": 1_705_300_000}, {"time": 1_705_300_060}]
        gaps = detect_candle_gaps(candles, "1m")
        assert gaps == []


# ---------------------------------------------------------------------------
# detect_candle_gaps — single gap cases
# ---------------------------------------------------------------------------


class TestDetectCandleGapsSingleGap:

    def test_one_missing_candle_low_severity(self):
        # Two 1m candles with 2*60=120s gap = 1 missing candle
        candles = [{"time": 1_705_300_000}, {"time": 1_705_300_120}]
        gaps = detect_candle_gaps(candles, "1m")
        assert len(gaps) == 1
        assert gaps[0].severity == "LOW"
        assert gaps[0].expectedCount == 1
        assert gaps[0].actualCount == 0
        assert gaps[0].intervalStr == "1m"

    def test_three_missing_candles_medium_severity(self):
        # 4*60=240s gap = 3 missing 1m candles
        candles = [{"time": 1_705_300_000}, {"time": 1_705_300_240}]
        gaps = detect_candle_gaps(candles, "1m")
        assert len(gaps) == 1
        assert gaps[0].severity == "MEDIUM"
        assert gaps[0].expectedCount == 3

    def test_six_missing_candles_high_severity(self):
        # 7*60=420s gap = 6 missing 1m candles
        candles = [{"time": 1_705_300_000}, {"time": 1_705_300_420}]
        gaps = detect_candle_gaps(candles, "1m")
        assert len(gaps) == 1
        assert gaps[0].severity == "HIGH"
        assert gaps[0].expectedCount == 6

    def test_gap_start_ms_is_correct(self):
        # start=1000s, next=1120s, interval=60s → missing candle at 1060s
        candles = [{"time": 1000}, {"time": 1120}]
        gaps = detect_candle_gaps(candles, "1m")
        assert len(gaps) == 1
        # gapStartMs = (1000 + 60) * 1000 = 1_060_000 ms
        assert gaps[0].gapStartMs == 1_060_000

    def test_gap_end_ms_single_missing_equals_start(self):
        # 1 missing candle: start and end are the same missing candle
        candles = [{"time": 1000}, {"time": 1120}]
        gaps = detect_candle_gaps(candles, "1m")
        # gapEndMs = (1000 + 1*60) * 1000 = 1_060_000 ms
        assert gaps[0].gapEndMs == 1_060_000

    def test_gap_end_ms_multiple_missing(self):
        # start=1000s, next=1300s, interval=60s → 4 missing: 1060, 1120, 1180, 1240
        candles = [{"time": 1000}, {"time": 1300}]
        gaps = detect_candle_gaps(candles, "1m")
        assert len(gaps) == 1
        # gapEndMs = (1000 + 4*60) * 1000 = 1_240_000 ms
        assert gaps[0].gapEndMs == 1_240_000
        assert gaps[0].expectedCount == 4


# ---------------------------------------------------------------------------
# detect_candle_gaps — multiple gaps
# ---------------------------------------------------------------------------


class TestDetectCandleGapsMultipleGaps:

    def test_two_separate_gaps(self):
        # Candles at t=0, t=120 (gap 1: 1 missing), t=180, t=420 (gap 2: 3 missing)
        candles = [
            {"time": 0},
            {"time": 120},   # gap of 2*60 → 1 missing
            {"time": 180},   # OK (60s)
            {"time": 420},   # gap of 4*60 → 3 missing
            {"time": 480},   # OK
        ]
        gaps = detect_candle_gaps(candles, "1m")
        assert len(gaps) == 2
        assert gaps[0].severity == "LOW"
        assert gaps[1].severity == "MEDIUM"

    def test_no_spurious_gaps_in_complete_sequence(self):
        candles = make_candles_no_gap(0, 100, 300)
        gaps = detect_candle_gaps(candles, "5m")
        assert gaps == []


# ---------------------------------------------------------------------------
# detect_candle_gaps — epoch milliseconds support
# ---------------------------------------------------------------------------


class TestDetectCandleGapsEpochMs:

    def test_accepts_epoch_milliseconds(self):
        # Values >= 1e10 treated as epoch ms; internally divided by 1000
        base_ms = 1_705_300_000_000
        interval_ms = 60_000
        candles = [
            {"time": base_ms},
            {"time": base_ms + 2 * interval_ms},  # 1 missing
        ]
        gaps = detect_candle_gaps(candles, "1m")
        assert len(gaps) == 1
        assert gaps[0].severity == "LOW"

    def test_ms_no_gap(self):
        base_ms = 1_705_300_000_000
        candles = [
            {"time": base_ms},
            {"time": base_ms + 60_000},
        ]
        gaps = detect_candle_gaps(candles, "1m")
        assert gaps == []


# ---------------------------------------------------------------------------
# detect_candle_gaps — interval-specific tests
# ---------------------------------------------------------------------------


class TestDetectCandleGapsIntervals:

    def test_5m_gap_detection(self):
        # 1200s apart = 4*300 → 3 missing 5m candles
        candles = [{"time": 0}, {"time": 1200}]
        gaps = detect_candle_gaps(candles, "5m")
        assert len(gaps) == 1
        assert gaps[0].expectedCount == 3

    def test_1h_gap_detection(self):
        # 7200s apart = 2*3600 → 1 missing 1h candle
        candles = [{"time": 0}, {"time": 7200}]
        gaps = detect_candle_gaps(candles, "1h")
        assert len(gaps) == 1
        assert gaps[0].expectedCount == 1

    def test_1d_gap_detection(self):
        # 3 days apart = 3*86400 → 2 missing 1d candles
        candles = [{"time": 0}, {"time": 3 * 86400}]
        gaps = detect_candle_gaps(candles, "1d")
        assert len(gaps) == 1
        assert gaps[0].expectedCount == 2


# ---------------------------------------------------------------------------
# detect_candle_gaps — error cases
# ---------------------------------------------------------------------------


class TestDetectCandleGapsErrors:

    def test_unsupported_interval_raises(self):
        candles = [{"time": 0}, {"time": 180}]
        with pytest.raises(ValueError, match="Unsupported interval"):
            detect_candle_gaps(candles, "3m")

    def test_unknown_interval_raises(self):
        candles = [{"time": 0}, {"time": 120}]
        with pytest.raises(ValueError):
            detect_candle_gaps(candles, "2m")

    def test_candle_missing_time_field_raises(self):
        candles = [{"time": 0}, {"open": 100, "close": 100}]
        with pytest.raises(ValueError, match="'time' field"):
            detect_candle_gaps(candles, "1m")

    def test_returns_data_gap_event_instances(self):
        candles = [{"time": 0}, {"time": 120}]
        gaps = detect_candle_gaps(candles, "1m")
        assert len(gaps) == 1
        assert isinstance(gaps[0], DataGapEvent)
