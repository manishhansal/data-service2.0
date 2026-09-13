"""
tests/unit/engines/test_market_session.py

Unit tests for src/engines/market_session.py — MarketSessionEngine.

Covers:
- Phase boundaries (at exactly 09:00, 09:08, 09:15, 15:30, 16:00 IST)
- Inclusive start, exclusive end boundary conditions
- Weekend → CLOSED
- Holiday → CLOSED (using mock HolidayCalendar)
- get_next_phase_change returns correct upcoming boundary
- Half-day support (REGULAR ends 13:00, POST_MARKET ends 13:30)
- is_trading_day reflects calendar state
- next_trading_day delegates to calendar when available
- No-calendar fallback: weekends → CLOSED, weekdays → classified normally

Requirements: 12.1, 12.2
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.core.schemas.instrument import SessionPhase
from src.engines.market_session import (
    HALF_TRADING_DAYS,
    MarketSessionEngine,
    _classify_half_day,
    _classify_normal_day,
)

# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------

_IST = ZoneInfo("Asia/Kolkata")
_UTC = timezone.utc


def _ist(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """Create a timezone-aware IST datetime and convert it to UTC for testing."""
    ist_dt = datetime(year, month, day, hour, minute, second, tzinfo=_IST)
    return ist_dt.astimezone(_UTC)


def _ist_time(hour: int, minute: int) -> time:
    """Convenience: return a naive time object (no tzinfo)."""
    return time(hour, minute)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_engine_with_mock_calendar(
    holidays: list[date] | None = None,
    half_days: list[date] | None = None,
) -> tuple[MarketSessionEngine, MagicMock]:
    """Return an engine + mock calendar with given holidays and half-days."""
    mock_cal = MagicMock()

    holiday_set = set(holidays or [])

    def _is_trading_day(d: date) -> bool:
        if d.weekday() >= 5:
            return False
        return d not in holiday_set

    def _next_trading_day(d: date) -> date:
        candidate = d + timedelta(days=1)
        while not _is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    mock_cal.is_trading_day.side_effect = _is_trading_day
    mock_cal.next_trading_day.side_effect = _next_trading_day

    return MarketSessionEngine(holiday_calendar=mock_cal), mock_cal


def _engine_no_calendar() -> MarketSessionEngine:
    """Return an engine with no holiday calendar attached."""
    return MarketSessionEngine(holiday_calendar=None)


# ---------------------------------------------------------------------------
# Phase boundary tests (normal trading day — 2025-01-06, Monday)
# ---------------------------------------------------------------------------

class TestNormalDayPhaseBoundaries:
    """Test exact phase boundaries on a normal trading day."""

    # Reference trading day: Monday, 2025-01-06

    def test_before_pre_open_is_closed(self):
        # 08:59:59 IST → CLOSED
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 8, 59, 59)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED

    def test_at_0900_is_pre_open(self):
        """09:00:00 IST → PRE_OPEN (inclusive start)."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 9, 0, 0)
        assert engine.get_current_phase(dt) == SessionPhase.PRE_OPEN

    def test_at_0907_is_pre_open(self):
        """09:07:59 IST is still PRE_OPEN."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 9, 7, 59)
        assert engine.get_current_phase(dt) == SessionPhase.PRE_OPEN

    def test_at_0908_is_pre_open_call_auction(self):
        """09:08:00 IST → PRE_OPEN_CALL_AUCTION (exclusive PRE_OPEN end)."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 9, 8, 0)
        assert engine.get_current_phase(dt) == SessionPhase.PRE_OPEN_CALL_AUCTION

    def test_at_0914_is_pre_open_call_auction(self):
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 9, 14, 59)
        assert engine.get_current_phase(dt) == SessionPhase.PRE_OPEN_CALL_AUCTION

    def test_at_0915_is_regular(self):
        """09:15:00 IST → REGULAR (exclusive PRE_OPEN_CALL_AUCTION end)."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 9, 15, 0)
        assert engine.get_current_phase(dt) == SessionPhase.REGULAR

    def test_during_regular_session(self):
        """Mid-session times are REGULAR."""
        engine, _ = _make_engine_with_mock_calendar()
        for hour in [10, 11, 12, 13, 14]:
            dt = _ist(2025, 1, 6, hour, 30, 0)
            assert engine.get_current_phase(dt) == SessionPhase.REGULAR, f"{hour}:30 should be REGULAR"

    def test_at_1529_is_regular(self):
        """15:29:59 IST is still REGULAR."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 15, 29, 59)
        assert engine.get_current_phase(dt) == SessionPhase.REGULAR

    def test_at_1530_is_post_market(self):
        """15:30:00 IST → POST_MARKET (exclusive REGULAR end)."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 15, 30, 0)
        assert engine.get_current_phase(dt) == SessionPhase.POST_MARKET

    def test_at_1559_is_post_market(self):
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 15, 59, 59)
        assert engine.get_current_phase(dt) == SessionPhase.POST_MARKET

    def test_at_1600_is_closed(self):
        """16:00:00 IST → CLOSED (exclusive POST_MARKET end)."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 16, 0, 0)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED

    def test_after_1600_is_closed(self):
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 20, 0, 0)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED


# ---------------------------------------------------------------------------
# Inclusive-start / exclusive-end boundary precision
# ---------------------------------------------------------------------------

class TestBoundaryPrecision:
    """Verify inclusive-start / exclusive-end at each boundary second."""

    def _trading_day(self) -> tuple[MarketSessionEngine, None]:
        engine, _ = _make_engine_with_mock_calendar()
        return engine, None

    def test_0900_inclusive(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 9, 0, 0)) == SessionPhase.PRE_OPEN

    def test_0859_exclusive(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 8, 59, 59)) == SessionPhase.CLOSED

    def test_0908_inclusive_starts_call_auction(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 9, 8, 0)) == SessionPhase.PRE_OPEN_CALL_AUCTION

    def test_0907_59_still_pre_open(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 9, 7, 59)) == SessionPhase.PRE_OPEN

    def test_0915_inclusive_starts_regular(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 9, 15, 0)) == SessionPhase.REGULAR

    def test_0914_59_still_call_auction(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 9, 14, 59)) == SessionPhase.PRE_OPEN_CALL_AUCTION

    def test_1530_inclusive_starts_post_market(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 15, 30, 0)) == SessionPhase.POST_MARKET

    def test_1529_59_still_regular(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 15, 29, 59)) == SessionPhase.REGULAR

    def test_1600_exclusive_ends_post_market(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 16, 0, 0)) == SessionPhase.CLOSED

    def test_1559_59_still_post_market(self):
        engine, _ = self._trading_day()
        assert engine.get_current_phase(_ist(2025, 1, 6, 15, 59, 59)) == SessionPhase.POST_MARKET


# ---------------------------------------------------------------------------
# Weekend → CLOSED
# ---------------------------------------------------------------------------

class TestWeekendIsClosed:
    def test_saturday_pre_open_time_is_closed(self):
        """Even at 09:15 IST on Saturday → CLOSED."""
        engine, _ = _make_engine_with_mock_calendar()
        # 2025-01-04 is a Saturday
        dt = _ist(2025, 1, 4, 9, 15, 0)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED

    def test_sunday_regular_time_is_closed(self):
        engine, _ = _make_engine_with_mock_calendar()
        # 2025-01-05 is a Sunday
        dt = _ist(2025, 1, 5, 12, 0, 0)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED

    def test_saturday_midnight_is_closed(self):
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 4, 0, 0, 0)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED

    def test_sunday_late_is_closed(self):
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 5, 23, 59, 59)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED


# ---------------------------------------------------------------------------
# Holiday → CLOSED (via mock HolidayCalendar)
# ---------------------------------------------------------------------------

class TestHolidayIsClosed:
    def test_holiday_at_regular_session_time_is_closed(self):
        """A registered NSE holiday returns CLOSED at any time of day."""
        holiday = date(2025, 1, 14)  # Tuesday (weekday)
        engine, _ = _make_engine_with_mock_calendar(holidays=[holiday])
        # 10:00 IST would normally be REGULAR
        dt = _ist(2025, 1, 14, 10, 0, 0)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED

    def test_holiday_at_pre_open_time_is_closed(self):
        holiday = date(2025, 1, 14)
        engine, _ = _make_engine_with_mock_calendar(holidays=[holiday])
        dt = _ist(2025, 1, 14, 9, 5, 0)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED

    def test_non_holiday_weekday_is_regular_at_0930(self):
        holiday = date(2025, 1, 14)
        engine, _ = _make_engine_with_mock_calendar(holidays=[holiday])
        # Day before holiday (Monday 2025-01-13) at 09:30 → REGULAR
        dt = _ist(2025, 1, 13, 9, 30, 0)
        assert engine.get_current_phase(dt) == SessionPhase.REGULAR

    def test_multiple_holidays_all_closed(self):
        holidays = [date(2025, 1, 14), date(2025, 3, 14)]
        engine, _ = _make_engine_with_mock_calendar(holidays=holidays)
        for h in holidays:
            dt = _ist(h.year, h.month, h.day, 10, 0, 0)
            assert engine.get_current_phase(dt) == SessionPhase.CLOSED, f"{h} should be CLOSED"

    def test_is_trading_day_false_for_holiday(self):
        holiday = date(2025, 1, 14)
        engine, _ = _make_engine_with_mock_calendar(holidays=[holiday])
        dt = _ist(2025, 1, 14, 9, 0, 0)
        assert engine.is_trading_day(dt) is False

    def test_is_trading_day_true_for_weekday_non_holiday(self):
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 12, 0, 0)
        assert engine.is_trading_day(dt) is True


# ---------------------------------------------------------------------------
# Half-day support
# ---------------------------------------------------------------------------

class TestHalfDayPhases:
    """Verify that half-trading-day schedule is applied correctly.

    Half-day reference: Monday, 2025-03-31 (weekday, non-holiday for testing).
    """

    # Use Monday 2025-03-31 — a weekday guaranteed to be a trading day
    # in the mock calendar (no holidays loaded by default).
    _HALF_DAY = date(2025, 3, 31)

    def setup_method(self, method):
        """Register the half-day before each test and clean up after."""
        HALF_TRADING_DAYS.add(self._HALF_DAY)

    def teardown_method(self, method):
        HALF_TRADING_DAYS.discard(self._HALF_DAY)

    def _engine(self) -> MarketSessionEngine:
        engine, _ = _make_engine_with_mock_calendar()
        return engine

    def test_half_day_0915_is_regular(self):
        engine = self._engine()
        dt = _ist(2025, 3, 31, 9, 15, 0)
        assert engine.get_current_phase(dt) == SessionPhase.REGULAR

    def test_half_day_regular_runs_until_1259(self):
        engine = self._engine()
        dt = _ist(2025, 3, 31, 12, 59, 59)
        assert engine.get_current_phase(dt) == SessionPhase.REGULAR

    def test_half_day_at_1300_is_post_market(self):
        """13:00:00 IST on half-day → POST_MARKET (exclusive REGULAR end)."""
        engine = self._engine()
        dt = _ist(2025, 3, 31, 13, 0, 0)
        assert engine.get_current_phase(dt) == SessionPhase.POST_MARKET

    def test_half_day_post_market_ends_at_1330(self):
        engine = self._engine()
        dt = _ist(2025, 3, 31, 13, 29, 59)
        assert engine.get_current_phase(dt) == SessionPhase.POST_MARKET

    def test_half_day_at_1330_is_closed(self):
        """13:30:00 IST on half-day → CLOSED."""
        engine = self._engine()
        dt = _ist(2025, 3, 31, 13, 30, 0)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED

    def test_half_day_at_1530_is_closed(self):
        """15:30 IST on half-day → still CLOSED (regular schedule does NOT apply)."""
        engine = self._engine()
        dt = _ist(2025, 3, 31, 15, 30, 0)
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED

    def test_half_day_pre_open_boundaries_unchanged(self):
        """PRE_OPEN and PRE_OPEN_CALL_AUCTION boundaries are the same on half-days."""
        engine = self._engine()
        assert engine.get_current_phase(_ist(2025, 3, 31, 9, 0, 0)) == SessionPhase.PRE_OPEN
        assert engine.get_current_phase(_ist(2025, 3, 31, 9, 8, 0)) == SessionPhase.PRE_OPEN_CALL_AUCTION
        assert engine.get_current_phase(_ist(2025, 3, 31, 9, 15, 0)) == SessionPhase.REGULAR


# ---------------------------------------------------------------------------
# get_next_phase_change
# ---------------------------------------------------------------------------

class TestGetNextPhaseChange:
    def test_returns_0900_when_before_pre_open(self):
        """Just before 09:00 IST on a trading day → next change is 09:00."""
        engine, _ = _make_engine_with_mock_calendar()
        # 08:30 IST Monday 2025-01-06
        dt = _ist(2025, 1, 6, 8, 30, 0)
        next_change = engine.get_next_phase_change(dt)
        # Convert to IST to verify
        next_ist = next_change.astimezone(_IST)
        assert next_ist.date() == date(2025, 1, 6)
        assert next_ist.hour == 9
        assert next_ist.minute == 0

    def test_returns_0908_during_pre_open(self):
        """During PRE_OPEN → next change is 09:08."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 9, 4, 0)  # 09:04 IST
        next_change = engine.get_next_phase_change(dt)
        next_ist = next_change.astimezone(_IST)
        assert next_ist.hour == 9
        assert next_ist.minute == 8

    def test_returns_0915_during_call_auction(self):
        """During PRE_OPEN_CALL_AUCTION → next change is 09:15."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 9, 10, 0)  # 09:10 IST
        next_change = engine.get_next_phase_change(dt)
        next_ist = next_change.astimezone(_IST)
        assert next_ist.hour == 9
        assert next_ist.minute == 15

    def test_returns_1530_during_regular(self):
        """During REGULAR → next change is 15:30."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 12, 0, 0)  # 12:00 IST
        next_change = engine.get_next_phase_change(dt)
        next_ist = next_change.astimezone(_IST)
        assert next_ist.hour == 15
        assert next_ist.minute == 30

    def test_returns_1600_during_post_market(self):
        """During POST_MARKET → next change is 16:00."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 15, 45, 0)  # 15:45 IST
        next_change = engine.get_next_phase_change(dt)
        next_ist = next_change.astimezone(_IST)
        assert next_ist.hour == 16
        assert next_ist.minute == 0

    def test_returns_utc_aware_datetime(self):
        """get_next_phase_change always returns a UTC-aware datetime."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 12, 0, 0)
        next_change = engine.get_next_phase_change(dt)
        assert next_change.tzinfo is not None

    def test_closed_evening_returns_next_morning_boundary(self):
        """After 16:00 on a trading day → next boundary is next trading day 09:00."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 17, 0, 0)  # Monday evening
        next_change = engine.get_next_phase_change(dt)
        next_ist = next_change.astimezone(_IST)
        # Should be Tuesday 2025-01-07 at 09:00
        assert next_ist.date() == date(2025, 1, 7)
        assert next_ist.hour == 9
        assert next_ist.minute == 0

    def test_friday_post_market_returns_monday_09_00(self):
        """After Friday close → next boundary is Monday 09:00."""
        engine, _ = _make_engine_with_mock_calendar()
        # Friday 2025-01-03 post-session
        dt = _ist(2025, 1, 3, 17, 0, 0)
        next_change = engine.get_next_phase_change(dt)
        next_ist = next_change.astimezone(_IST)
        # Next Monday is 2025-01-06
        assert next_ist.weekday() == 0  # Monday
        assert next_ist.hour == 9
        assert next_ist.minute == 0


# ---------------------------------------------------------------------------
# is_trading_day
# ---------------------------------------------------------------------------

class TestIsTradingDay:
    def test_weekday_with_no_calendar_is_trading_day(self):
        engine = _engine_no_calendar()
        dt = _ist(2025, 1, 6, 12, 0, 0)  # Monday
        assert engine.is_trading_day(dt) is True

    def test_weekend_with_no_calendar_is_not_trading_day(self):
        engine = _engine_no_calendar()
        dt = _ist(2025, 1, 4, 12, 0, 0)  # Saturday
        assert engine.is_trading_day(dt) is False

    def test_holiday_with_calendar_is_not_trading_day(self):
        holiday = date(2025, 1, 14)
        engine, _ = _make_engine_with_mock_calendar(holidays=[holiday])
        dt = _ist(2025, 1, 14, 10, 0, 0)
        assert engine.is_trading_day(dt) is False

    def test_defaults_to_now_when_no_arg(self):
        """is_trading_day() with no argument should not raise."""
        engine = _engine_no_calendar()
        result = engine.is_trading_day()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# next_trading_day
# ---------------------------------------------------------------------------

class TestNextTradingDay:
    def test_monday_next_is_tuesday(self):
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 12, 0, 0)  # Monday
        assert engine.next_trading_day(dt) == date(2025, 1, 7)

    def test_friday_next_is_monday(self):
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 3, 12, 0, 0)  # Friday
        assert engine.next_trading_day(dt) == date(2025, 1, 6)

    def test_skips_holiday_in_calendar(self):
        holiday = date(2025, 1, 7)  # Tuesday
        engine, _ = _make_engine_with_mock_calendar(holidays=[holiday])
        dt = _ist(2025, 1, 6, 12, 0, 0)  # Monday
        # Should skip Tuesday holiday → Wednesday
        assert engine.next_trading_day(dt) == date(2025, 1, 8)

    def test_fallback_no_calendar_skips_weekend(self):
        """Without calendar, next_trading_day skips weekends."""
        engine = _engine_no_calendar()
        dt = _ist(2025, 1, 3, 12, 0, 0)  # Friday
        assert engine.next_trading_day(dt) == date(2025, 1, 6)  # Monday


# ---------------------------------------------------------------------------
# No-calendar fallback behaviour
# ---------------------------------------------------------------------------

class TestNoCalondarFallback:
    def test_weekday_classified_normally_without_calendar(self):
        """Without a calendar, weekday sessions are classified by time."""
        engine = _engine_no_calendar()
        # Monday 2025-01-06 at 10:00 IST
        dt = _ist(2025, 1, 6, 10, 0, 0)
        assert engine.get_current_phase(dt) == SessionPhase.REGULAR

    def test_weekend_is_closed_without_calendar(self):
        engine = _engine_no_calendar()
        dt = _ist(2025, 1, 4, 10, 0, 0)  # Saturday
        assert engine.get_current_phase(dt) == SessionPhase.CLOSED


# ---------------------------------------------------------------------------
# _classify_normal_day and _classify_half_day unit tests
# ---------------------------------------------------------------------------

class TestClassifyNormalDay:
    def test_pre_open_start(self):
        assert _classify_normal_day(time(9, 0)) == SessionPhase.PRE_OPEN

    def test_pre_open_just_before_end(self):
        assert _classify_normal_day(time(9, 7, 59)) == SessionPhase.PRE_OPEN

    def test_call_auction_start(self):
        assert _classify_normal_day(time(9, 8)) == SessionPhase.PRE_OPEN_CALL_AUCTION

    def test_call_auction_just_before_end(self):
        assert _classify_normal_day(time(9, 14, 59)) == SessionPhase.PRE_OPEN_CALL_AUCTION

    def test_regular_start(self):
        assert _classify_normal_day(time(9, 15)) == SessionPhase.REGULAR

    def test_regular_just_before_end(self):
        assert _classify_normal_day(time(15, 29, 59)) == SessionPhase.REGULAR

    def test_post_market_start(self):
        assert _classify_normal_day(time(15, 30)) == SessionPhase.POST_MARKET

    def test_post_market_just_before_end(self):
        assert _classify_normal_day(time(15, 59, 59)) == SessionPhase.POST_MARKET

    def test_closed_at_1600(self):
        assert _classify_normal_day(time(16, 0)) == SessionPhase.CLOSED

    def test_closed_midnight(self):
        assert _classify_normal_day(time(0, 0)) == SessionPhase.CLOSED

    def test_closed_early_morning(self):
        assert _classify_normal_day(time(7, 0)) == SessionPhase.CLOSED


class TestClassifyHalfDay:
    def test_regular_starts_at_0915(self):
        assert _classify_half_day(time(9, 15)) == SessionPhase.REGULAR

    def test_regular_ends_just_before_1300(self):
        assert _classify_half_day(time(12, 59, 59)) == SessionPhase.REGULAR

    def test_post_market_starts_at_1300(self):
        assert _classify_half_day(time(13, 0)) == SessionPhase.POST_MARKET

    def test_post_market_ends_just_before_1330(self):
        assert _classify_half_day(time(13, 29, 59)) == SessionPhase.POST_MARKET

    def test_closed_at_1330(self):
        assert _classify_half_day(time(13, 30)) == SessionPhase.CLOSED

    def test_closed_at_1530(self):
        """Normal REGULAR hours do not apply on half-day."""
        assert _classify_half_day(time(15, 30)) == SessionPhase.CLOSED

    def test_pre_open_unchanged(self):
        assert _classify_half_day(time(9, 0)) == SessionPhase.PRE_OPEN
        assert _classify_half_day(time(9, 8)) == SessionPhase.PRE_OPEN_CALL_AUCTION


# ---------------------------------------------------------------------------
# Determinism property: every instant maps to exactly one phase
# ---------------------------------------------------------------------------

class TestPhaseDeterminism:
    """Every sampled UTC instant maps to exactly one SessionPhase."""

    def test_sample_of_times_all_return_one_phase(self):
        engine, _ = _make_engine_with_mock_calendar()
        all_phases = set(SessionPhase)

        # Sample one minute interval across a full trading day
        base = datetime(2025, 1, 6, 0, 0, 0, tzinfo=_UTC)
        for minute in range(0, 24 * 60, 5):
            dt = base + timedelta(minutes=minute)
            phase = engine.get_current_phase(dt)
            assert phase in all_phases, f"Unknown phase at {dt}: {phase}"

    def test_two_calls_same_input_same_output(self):
        """Calling get_current_phase twice with the same input yields the same result."""
        engine, _ = _make_engine_with_mock_calendar()
        dt = _ist(2025, 1, 6, 10, 30, 0)
        assert engine.get_current_phase(dt) == engine.get_current_phase(dt)

    def test_naive_utc_datetime_still_works(self):
        """A naive datetime is treated as UTC and does not raise."""
        engine, _ = _make_engine_with_mock_calendar()
        naive_dt = datetime(2025, 1, 6, 4, 30, 0)  # 04:30 UTC = 10:00 IST
        phase = engine.get_current_phase(naive_dt)
        assert phase == SessionPhase.REGULAR
