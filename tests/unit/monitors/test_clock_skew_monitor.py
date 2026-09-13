"""
Unit tests for src/monitors/clock_skew_monitor.py

Coverage areas:
  1.  Empty window returns zero stats and isAcceptable=True
  2.  Single sample — stats equal the one skew value
  3.  Multiple samples with known skews — median/min/max correct
  4.  is_skew_acceptable reflects the medianSkewMs vs threshold comparison
  5.  Samples older than the window are pruned
  6.  Custom window size is respected
  7.  ClockSkewStats.isAcceptable mirrors the threshold comparison
  8.  Record uses local time.time() (mocked)
  9.  Zero-skew events (event_time_ms == local_time_ms)
  10. Large skew triggers isAcceptable=False
  11. Boundary: skew exactly equal to threshold → acceptable (≤, not <)
  12. Invalid constructor arguments raise ValueError
  13. Invalid threshold in get_skew_stats raises ValueError
  14. WindowSizeMs field matches constructor argument * 1000
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.monitors.clock_skew_monitor import ClockSkewMonitor, ClockSkewStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    """Return the current UTC epoch in milliseconds."""
    return int(time.time() * 1_000)


# ---------------------------------------------------------------------------
# ClockSkewStats model tests
# ---------------------------------------------------------------------------


class TestClockSkewStats:
    """Pydantic model field validation."""

    def test_valid_construction(self) -> None:
        stats = ClockSkewStats(
            sampleCount=5,
            medianSkewMs=100.0,
            maxSkewMs=200.0,
            minSkewMs=50.0,
            windowSizeMs=60_000,
            isAcceptable=True,
        )
        assert stats.sampleCount == 5
        assert stats.medianSkewMs == 100.0
        assert stats.maxSkewMs == 200.0
        assert stats.minSkewMs == 50.0
        assert stats.windowSizeMs == 60_000
        assert stats.isAcceptable is True

    def test_is_acceptable_false(self) -> None:
        stats = ClockSkewStats(
            sampleCount=1,
            medianSkewMs=600.0,
            maxSkewMs=600.0,
            minSkewMs=600.0,
            windowSizeMs=60_000,
            isAcceptable=False,
        )
        assert stats.isAcceptable is False

    def test_zero_sample_count_allowed(self) -> None:
        """sampleCount=0 is valid (empty window)."""
        stats = ClockSkewStats(
            sampleCount=0,
            medianSkewMs=0.0,
            maxSkewMs=0.0,
            minSkewMs=0.0,
            windowSizeMs=60_000,
            isAcceptable=True,
        )
        assert stats.sampleCount == 0


# ---------------------------------------------------------------------------
# ClockSkewMonitor tests
# ---------------------------------------------------------------------------


class TestClockSkewMonitorEmptyWindow:
    """Behaviour when no samples have been recorded."""

    def test_empty_window_returns_zero_stats(self) -> None:
        monitor = ClockSkewMonitor(window_size_sec=60)
        stats = monitor.get_skew_stats()

        assert stats.sampleCount == 0
        assert stats.medianSkewMs == 0.0
        assert stats.maxSkewMs == 0.0
        assert stats.minSkewMs == 0.0

    def test_empty_window_is_acceptable(self) -> None:
        monitor = ClockSkewMonitor()
        assert monitor.is_skew_acceptable() is True

    def test_empty_window_stats_is_acceptable_true(self) -> None:
        monitor = ClockSkewMonitor()
        stats = monitor.get_skew_stats()
        assert stats.isAcceptable is True

    def test_empty_window_window_size_ms_matches_config(self) -> None:
        monitor = ClockSkewMonitor(window_size_sec=30)
        stats = monitor.get_skew_stats()
        assert stats.windowSizeMs == 30_000


class TestClockSkewMonitorSingleSample:
    """Single recorded sample."""

    def test_single_sample_stats(self) -> None:
        monitor = ClockSkewMonitor()
        now_ms = _now_ms()
        event_ms = now_ms - 200  # 200ms skew

        with patch("src.monitors.clock_skew_monitor.time.time", return_value=now_ms / 1000):
            monitor.record(event_ms)

        stats = monitor.get_skew_stats()
        assert stats.sampleCount == 1
        assert stats.medianSkewMs == pytest.approx(200.0, abs=1.0)
        assert stats.maxSkewMs == pytest.approx(200.0, abs=1.0)
        assert stats.minSkewMs == pytest.approx(200.0, abs=1.0)

    def test_single_sample_zero_skew(self) -> None:
        monitor = ClockSkewMonitor()
        now_ms = _now_ms()

        with patch("src.monitors.clock_skew_monitor.time.time", return_value=now_ms / 1000):
            monitor.record(now_ms)  # event_time == local_time

        stats = monitor.get_skew_stats()
        assert stats.sampleCount == 1
        assert stats.medianSkewMs == pytest.approx(0.0, abs=1.0)
        assert stats.isAcceptable is True


class TestClockSkewMonitorMultipleSamples:
    """Multiple samples with known skew values."""

    def _feed_skews(
        self, monitor: ClockSkewMonitor, skews_ms: list[int], base_ms: int | None = None
    ) -> None:
        """Record samples with pre-determined skew values by mocking time."""
        if base_ms is None:
            base_ms = _now_ms()
        for skew in skews_ms:
            local_ms = base_ms
            event_ms = local_ms - skew  # positive skew = event is in the past
            with patch(
                "src.monitors.clock_skew_monitor.time.time",
                return_value=local_ms / 1000,
            ):
                monitor.record(event_ms)

    def test_median_of_odd_count(self) -> None:
        # Skews: 100, 300, 500 → median = 300
        monitor = ClockSkewMonitor()
        self._feed_skews(monitor, [100, 300, 500])
        stats = monitor.get_skew_stats()
        assert stats.sampleCount == 3
        assert stats.medianSkewMs == pytest.approx(300.0, abs=1.0)

    def test_median_of_even_count(self) -> None:
        # Skews: 100, 200, 300, 400 → median = (200 + 300) / 2 = 250
        monitor = ClockSkewMonitor()
        self._feed_skews(monitor, [100, 200, 300, 400])
        stats = monitor.get_skew_stats()
        assert stats.sampleCount == 4
        assert stats.medianSkewMs == pytest.approx(250.0, abs=1.0)

    def test_min_max_correct(self) -> None:
        monitor = ClockSkewMonitor()
        self._feed_skews(monitor, [50, 150, 600])
        stats = monitor.get_skew_stats()
        assert stats.minSkewMs == pytest.approx(50.0, abs=1.0)
        assert stats.maxSkewMs == pytest.approx(600.0, abs=1.0)

    def test_absolute_skew_negative_difference(self) -> None:
        """event_time_ms > local_time_ms (future event timestamp) — still absolute."""
        monitor = ClockSkewMonitor()
        now_ms = _now_ms()
        # event_time is 300ms in the future relative to local clock
        with patch("src.monitors.clock_skew_monitor.time.time", return_value=now_ms / 1000):
            monitor.record(now_ms + 300)

        stats = monitor.get_skew_stats()
        assert stats.medianSkewMs == pytest.approx(300.0, abs=1.0)


class TestClockSkewMonitorAcceptability:
    """is_skew_acceptable and threshold logic."""

    def _record_with_skew(self, monitor: ClockSkewMonitor, skew_ms: int) -> None:
        now_ms = _now_ms()
        with patch("src.monitors.clock_skew_monitor.time.time", return_value=now_ms / 1000):
            monitor.record(now_ms - skew_ms)

    def test_skew_below_threshold_is_acceptable(self) -> None:
        monitor = ClockSkewMonitor()
        self._record_with_skew(monitor, skew_ms=100)
        assert monitor.is_skew_acceptable(threshold_ms=500) is True

    def test_skew_above_threshold_not_acceptable(self) -> None:
        monitor = ClockSkewMonitor()
        self._record_with_skew(monitor, skew_ms=600)
        assert monitor.is_skew_acceptable(threshold_ms=500) is False

    def test_skew_exactly_at_threshold_is_acceptable(self) -> None:
        """Boundary: median == threshold is acceptable (≤ not <)."""
        monitor = ClockSkewMonitor()
        self._record_with_skew(monitor, skew_ms=500)
        assert monitor.is_skew_acceptable(threshold_ms=500) is True

    def test_default_threshold_is_500ms(self) -> None:
        monitor = ClockSkewMonitor()
        self._record_with_skew(monitor, skew_ms=499)
        assert monitor.is_skew_acceptable() is True

        monitor2 = ClockSkewMonitor()
        self._record_with_skew(monitor2, skew_ms=501)
        assert monitor2.is_skew_acceptable() is False

    def test_stats_is_acceptable_field_consistent_with_method(self) -> None:
        monitor = ClockSkewMonitor()
        self._record_with_skew(monitor, skew_ms=300)
        stats = monitor.get_skew_stats(threshold_ms=500)
        assert stats.isAcceptable == monitor.is_skew_acceptable(threshold_ms=500)

    def test_custom_threshold_respected(self) -> None:
        monitor = ClockSkewMonitor()
        self._record_with_skew(monitor, skew_ms=200)
        # Tight threshold: 100 ms → not acceptable
        assert monitor.is_skew_acceptable(threshold_ms=100) is False
        # Loose threshold: 1000 ms → acceptable
        assert monitor.is_skew_acceptable(threshold_ms=1000) is True


class TestClockSkewMonitorWindowExpiry:
    """Old samples are pruned outside the sliding window."""

    def test_samples_outside_window_are_pruned(self) -> None:
        monitor = ClockSkewMonitor(window_size_sec=10)
        now_ms = _now_ms()

        # Record a sample that is 15 seconds old (outside the 10s window).
        old_local_ms = now_ms - 15_000
        with patch(
            "src.monitors.clock_skew_monitor.time.time",
            return_value=old_local_ms / 1000,
        ):
            monitor.record(old_local_ms - 900)  # large skew

        # Record a fresh sample with small skew.
        with patch(
            "src.monitors.clock_skew_monitor.time.time",
            return_value=now_ms / 1000,
        ):
            monitor.record(now_ms - 50)

        # get_skew_stats prunes on the current time; patch to now_ms.
        with patch(
            "src.monitors.clock_skew_monitor.time.time",
            return_value=now_ms / 1000,
        ):
            stats = monitor.get_skew_stats()

        # Only the fresh sample should remain.
        assert stats.sampleCount == 1
        assert stats.medianSkewMs == pytest.approx(50.0, abs=1.0)

    def test_all_samples_expire_returns_empty_stats(self) -> None:
        monitor = ClockSkewMonitor(window_size_sec=5)
        old_ms = _now_ms() - 10_000  # 10 seconds ago

        with patch(
            "src.monitors.clock_skew_monitor.time.time",
            return_value=old_ms / 1000,
        ):
            monitor.record(old_ms - 200)

        # Advance to now → all samples are pruned.
        now_ms = _now_ms()
        with patch(
            "src.monitors.clock_skew_monitor.time.time",
            return_value=now_ms / 1000,
        ):
            stats = monitor.get_skew_stats()

        assert stats.sampleCount == 0
        assert stats.isAcceptable is True

    def test_window_size_ms_field_matches_constructor(self) -> None:
        monitor = ClockSkewMonitor(window_size_sec=120)
        stats = monitor.get_skew_stats()
        assert stats.windowSizeMs == 120_000


class TestClockSkewMonitorConstructorValidation:
    """Invalid constructor and method arguments are rejected early."""

    def test_zero_window_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="window_size_sec"):
            ClockSkewMonitor(window_size_sec=0)

    def test_negative_window_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="window_size_sec"):
            ClockSkewMonitor(window_size_sec=-10)

    def test_zero_threshold_raises_value_error(self) -> None:
        monitor = ClockSkewMonitor()
        with pytest.raises(ValueError, match="threshold_ms"):
            monitor.get_skew_stats(threshold_ms=0)

    def test_negative_threshold_raises_value_error(self) -> None:
        monitor = ClockSkewMonitor()
        with pytest.raises(ValueError, match="threshold_ms"):
            monitor.get_skew_stats(threshold_ms=-1)


class TestClockSkewMonitorMockedTime:
    """record() uses time.time() for local timestamp."""

    def test_record_uses_time_time(self) -> None:
        monitor = ClockSkewMonitor()
        fake_local_ms = 1_700_000_000_000  # arbitrary epoch ms
        event_ms = fake_local_ms - 250

        with patch(
            "src.monitors.clock_skew_monitor.time.time",
            return_value=fake_local_ms / 1000,
        ):
            monitor.record(event_ms)

        # The sample should reflect the mocked time, so skew = 250 ms.
        with patch(
            "src.monitors.clock_skew_monitor.time.time",
            return_value=fake_local_ms / 1000,
        ):
            stats = monitor.get_skew_stats()

        assert stats.sampleCount == 1
        assert stats.medianSkewMs == pytest.approx(250.0, abs=1.0)


class TestClockSkewMonitorHighLoad:
    """Stability under many consecutive samples."""

    def test_many_samples_correct_stats(self) -> None:
        """Record 1000 samples with a known uniform skew and verify stats."""
        monitor = ClockSkewMonitor(window_size_sec=3600)
        now_ms = _now_ms()
        skew_ms = 42

        for i in range(1000):
            # Spread samples across the window but all within it.
            fake_local = now_ms - (i * 100)  # 100ms apart
            with patch(
                "src.monitors.clock_skew_monitor.time.time",
                return_value=fake_local / 1000,
            ):
                monitor.record(fake_local - skew_ms)

        with patch(
            "src.monitors.clock_skew_monitor.time.time",
            return_value=now_ms / 1000,
        ):
            stats = monitor.get_skew_stats()

        assert stats.sampleCount == 1000
        assert stats.medianSkewMs == pytest.approx(skew_ms, abs=1.0)
        assert stats.maxSkewMs == pytest.approx(skew_ms, abs=1.0)
        assert stats.minSkewMs == pytest.approx(skew_ms, abs=1.0)
        assert stats.isAcceptable is True
