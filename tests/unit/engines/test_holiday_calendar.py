"""
tests/unit/engines/test_holiday_calendar.py

Unit tests for src/engines/holiday_calendar.py — HolidayCalendar.

Covers:
- Weekend detection (Saturday/Sunday → not trading day)
- Known NSE holiday → not trading day
- Regular weekday → trading day
- Refresh with mocked HTTP response populates holidays
- Refresh failure retains previous calendar
- get_holidays_for_month returns correct dates
- calendar_status updates on successful refresh
- next_trading_day skips weekends and holidays
- Singleton pattern via get_instance()
- load_holidays() directly populates in-memory store

Requirements: 12.3, 12.4
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.engines.holiday_calendar import (
    HolidayCalendar,
    _deserialise_holiday_set,
    _parse_nse_date,
    _serialise_holiday_set,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_calendar() -> HolidayCalendar:
    """Return a clean HolidayCalendar instance (singleton reset each test)."""
    HolidayCalendar.reset_instance()
    return HolidayCalendar.get_instance()


def _nse_api_response(holidays: list[date]) -> dict:
    """Build a mock NSE holiday API response dict for the given dates."""
    # Format: {"Jan": [{"tradingDate": "01-Jan-2025", ...}], ...}
    by_month: dict[str, list[dict]] = {}
    for d in holidays:
        key = d.strftime("%b")  # "Jan", "Feb", …
        entry = {
            "tradingDate": d.strftime("%d-%b-%Y"),
            "weekDay": d.strftime("%A"),
            "description": "Test Holiday",
        }
        by_month.setdefault(key, []).append(entry)
    return by_month


# ---------------------------------------------------------------------------
# Weekend detection
# ---------------------------------------------------------------------------


class TestWeekendDetection:
    def test_saturday_is_not_trading_day(self):
        cal = _fresh_calendar()
        # 2025-01-04 is a Saturday
        assert cal.is_trading_day(date(2025, 1, 4)) is False

    def test_sunday_is_not_trading_day(self):
        cal = _fresh_calendar()
        # 2025-01-05 is a Sunday
        assert cal.is_trading_day(date(2025, 1, 5)) is False

    def test_monday_is_trading_day_when_no_calendar(self):
        """Without a loaded calendar, weekdays default to trading days."""
        cal = _fresh_calendar()
        # 2025-01-06 is a Monday; no holidays loaded
        assert cal.is_trading_day(date(2025, 1, 6)) is True

    def test_friday_is_trading_day_when_no_calendar(self):
        cal = _fresh_calendar()
        # 2025-01-03 is a Friday
        assert cal.is_trading_day(date(2025, 1, 3)) is True

    def test_all_weekdays_in_week_no_calendar(self):
        """All five weekdays are trading days when no holiday calendar is loaded."""
        cal = _fresh_calendar()
        # Week of 2025-01-06
        for offset in range(5):
            d = date(2025, 1, 6) + timedelta(days=offset)
            assert cal.is_trading_day(d) is True, f"{d} should be a trading day"


# ---------------------------------------------------------------------------
# Holiday detection
# ---------------------------------------------------------------------------


class TestHolidayDetection:
    def test_known_holiday_is_not_trading_day(self):
        """A date in the loaded holiday set returns False."""
        cal = _fresh_calendar()
        holiday = date(2025, 1, 14)  # Makar Sankranti (sample)
        cal.load_holidays(2025, [holiday])
        assert cal.is_trading_day(holiday) is False

    def test_non_holiday_weekday_is_trading_day(self):
        cal = _fresh_calendar()
        holiday = date(2025, 1, 14)
        cal.load_holidays(2025, [holiday])
        # Adjacent weekday is a trading day
        regular = date(2025, 1, 13)  # Monday
        assert cal.is_trading_day(regular) is True

    def test_holiday_on_saturday_still_not_trading_day(self):
        """A holiday listed on a Saturday doesn't change the result (already False)."""
        cal = _fresh_calendar()
        sat = date(2025, 1, 4)  # Saturday
        cal.load_holidays(2025, [sat])
        assert cal.is_trading_day(sat) is False

    def test_multiple_holidays_in_month(self):
        cal = _fresh_calendar()
        holidays = [date(2025, 1, 14), date(2025, 1, 26)]  # 26-Jan is Republic Day
        cal.load_holidays(2025, holidays)
        for h in holidays:
            assert cal.is_trading_day(h) is False
        # Non-holiday Monday
        assert cal.is_trading_day(date(2025, 1, 6)) is True

    def test_holiday_different_year_not_applied_to_current_year(self):
        """Holidays loaded for year X do not affect year Y lookups."""
        cal = _fresh_calendar()
        cal.load_holidays(2024, [date(2024, 11, 1)])  # Diwali 2024
        # 2025-11-01 is a Saturday anyway; use a weekday in same month for 2025
        cal.load_holidays(2025, [])  # explicitly empty for 2025
        # 2025-11-03 is Monday — no holiday
        assert cal.is_trading_day(date(2025, 11, 3)) is True


# ---------------------------------------------------------------------------
# Refresh with mocked HTTP response
# ---------------------------------------------------------------------------


class TestRefresh:
    @pytest.mark.asyncio
    async def test_refresh_populates_holidays_from_api(self):
        """refresh() fetches from NSE API and populates the in-memory dict."""
        cal = _fresh_calendar()
        test_holidays = [date(2025, 1, 14), date(2025, 8, 15), date(2025, 10, 2)]
        api_response = _nse_api_response(test_holidays)

        async def _mock_fetch(year: int) -> list[date]:
            return test_holidays

        with patch(
            "src.engines.holiday_calendar._fetch_holidays_from_nse",
            new=AsyncMock(side_effect=_mock_fetch),
        ):
            await cal.refresh(calendar_year=2025)

        for h in test_holidays:
            assert cal.is_trading_day(h) is False

    @pytest.mark.asyncio
    async def test_refresh_updates_calendar_status(self):
        """calendar_status is set to today's ISO string after a successful refresh."""
        cal = _fresh_calendar()
        assert cal.calendar_status is None

        with patch(
            "src.engines.holiday_calendar._fetch_holidays_from_nse",
            new=AsyncMock(return_value=[date(2025, 1, 14)]),
        ):
            await cal.refresh(calendar_year=2025)

        from datetime import date as _date
        today = _date.today().isoformat()
        assert cal.calendar_status == today

    @pytest.mark.asyncio
    async def test_refresh_failure_retains_previous_calendar(self):
        """When API fails, previously loaded holidays are preserved."""
        cal = _fresh_calendar()
        original_holiday = date(2025, 1, 14)
        cal.load_holidays(2025, [original_holiday])

        with patch(
            "src.engines.holiday_calendar._fetch_holidays_from_nse",
            new=AsyncMock(side_effect=RuntimeError("network error")),
        ):
            await cal.refresh(calendar_year=2025)

        # Original holiday should still be in effect
        assert cal.is_trading_day(original_holiday) is False

    @pytest.mark.asyncio
    async def test_refresh_failure_does_not_raise(self):
        """refresh() must never raise even on total API failure."""
        cal = _fresh_calendar()
        with patch(
            "src.engines.holiday_calendar._fetch_holidays_from_nse",
            new=AsyncMock(side_effect=Exception("timeout")),
        ):
            # Must not raise
            await cal.refresh(calendar_year=2025)

    @pytest.mark.asyncio
    async def test_refresh_failure_emits_warning_log(self, caplog):
        """A structured WARNING is emitted on refresh failure."""
        cal = _fresh_calendar()

        with patch(
            "src.engines.holiday_calendar._fetch_holidays_from_nse",
            new=AsyncMock(side_effect=RuntimeError("conn timeout")),
        ):
            with caplog.at_level(logging.WARNING, logger="src.engines.holiday_calendar"):
                await cal.refresh(calendar_year=2025)

        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_refresh_with_redis_writes_to_redis(self):
        """When Redis is available, refresh writes to Redis after fetching."""
        cal = _fresh_calendar()

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set_with_ttl = AsyncMock()
        cal.set_redis_client(mock_redis)

        test_holidays = [date(2025, 1, 14)]

        with patch(
            "src.engines.holiday_calendar._fetch_holidays_from_nse",
            new=AsyncMock(return_value=test_holidays),
        ):
            await cal.refresh(calendar_year=2025)

        mock_redis.set_with_ttl.assert_awaited_once()
        call_args = mock_redis.set_with_ttl.call_args
        assert "mds:holiday_calendar:2025" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_refresh_loads_from_redis_cache_if_available(self):
        """When Redis has cached holiday data, no API fetch is performed."""
        cal = _fresh_calendar()

        cached_holidays = [date(2025, 1, 14), date(2025, 8, 15)]
        cached_str = _serialise_holiday_set(cached_holidays)

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=cached_str)
        mock_redis.set_with_ttl = AsyncMock()
        cal.set_redis_client(mock_redis)

        with patch(
            "src.engines.holiday_calendar._fetch_holidays_from_nse",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            await cal.refresh(calendar_year=2025)

        assert cal.is_trading_day(date(2025, 1, 14)) is False
        assert cal.is_trading_day(date(2025, 8, 15)) is False


# ---------------------------------------------------------------------------
# get_holidays_for_month
# ---------------------------------------------------------------------------


class TestGetHolidaysForMonth:
    def test_returns_holidays_in_specified_month(self):
        cal = _fresh_calendar()
        holidays = [date(2025, 1, 14), date(2025, 1, 26), date(2025, 2, 19)]
        cal.load_holidays(2025, holidays)
        result = cal.get_holidays_for_month(2025, 1)
        assert result == [date(2025, 1, 14), date(2025, 1, 26)]

    def test_returns_empty_for_month_with_no_holidays(self):
        cal = _fresh_calendar()
        cal.load_holidays(2025, [date(2025, 1, 14)])
        result = cal.get_holidays_for_month(2025, 3)
        assert result == []

    def test_returns_empty_when_no_calendar_loaded(self):
        cal = _fresh_calendar()
        result = cal.get_holidays_for_month(2025, 1)
        assert result == []

    def test_returns_sorted_list(self):
        cal = _fresh_calendar()
        # Load in reverse order to verify sorting
        holidays = [date(2025, 1, 26), date(2025, 1, 14), date(2025, 1, 2)]
        cal.load_holidays(2025, holidays)
        result = cal.get_holidays_for_month(2025, 1)
        assert result == sorted(result)

    def test_only_returns_dates_for_exact_year_and_month(self):
        cal = _fresh_calendar()
        cal.load_holidays(2025, [date(2025, 1, 14)])
        cal.load_holidays(2024, [date(2024, 1, 14)])
        # Should only return 2025 dates
        result = cal.get_holidays_for_month(2025, 1)
        assert all(d.year == 2025 for d in result)

    def test_multiple_holidays_in_month(self):
        cal = _fresh_calendar()
        holidays = [
            date(2025, 8, 15),  # Independence Day
            date(2025, 8, 27),  # Ganesh Chaturthi
        ]
        cal.load_holidays(2025, holidays)
        result = cal.get_holidays_for_month(2025, 8)
        assert len(result) == 2
        assert date(2025, 8, 15) in result
        assert date(2025, 8, 27) in result


# ---------------------------------------------------------------------------
# next_trading_day
# ---------------------------------------------------------------------------


class TestNextTradingDay:
    def test_next_day_when_no_holiday(self):
        """next_trading_day returns the next weekday when no holidays are loaded."""
        cal = _fresh_calendar()
        # Monday 2025-01-06 → next trading day is Tuesday 2025-01-07
        result = cal.next_trading_day(date(2025, 1, 6))
        assert result == date(2025, 1, 7)

    def test_skips_weekend(self):
        """next_trading_day skips Saturday and Sunday."""
        cal = _fresh_calendar()
        # Friday 2025-01-03 → next trading day should be Monday 2025-01-06
        result = cal.next_trading_day(date(2025, 1, 3))
        assert result == date(2025, 1, 6)

    def test_skips_weekend_from_saturday(self):
        cal = _fresh_calendar()
        # Saturday → next trading day is Monday
        result = cal.next_trading_day(date(2025, 1, 4))
        assert result == date(2025, 1, 6)

    def test_skips_holiday(self):
        """next_trading_day skips NSE holidays."""
        cal = _fresh_calendar()
        cal.load_holidays(2025, [date(2025, 1, 7)])  # Tuesday is holiday
        # Monday 2025-01-06 → skip Tuesday (holiday) → Wednesday 2025-01-08
        result = cal.next_trading_day(date(2025, 1, 6))
        assert result == date(2025, 1, 8)

    def test_skips_consecutive_holidays(self):
        """next_trading_day skips multiple consecutive holidays."""
        cal = _fresh_calendar()
        cal.load_holidays(2025, [date(2025, 1, 7), date(2025, 1, 8)])
        # Monday → skip Tue and Wed → Thursday
        result = cal.next_trading_day(date(2025, 1, 6))
        assert result == date(2025, 1, 9)


# ---------------------------------------------------------------------------
# calendar_status property
# ---------------------------------------------------------------------------


class TestCalendarStatus:
    def test_none_when_no_refresh(self):
        cal = _fresh_calendar()
        assert cal.calendar_status is None

    def test_iso_string_after_load_holidays(self):
        from datetime import date as _date
        cal = _fresh_calendar()
        cal.load_holidays(2025, [])
        assert cal.calendar_status == _date.today().isoformat()

    @pytest.mark.asyncio
    async def test_updates_on_successful_refresh(self):
        from datetime import date as _date
        cal = _fresh_calendar()

        with patch(
            "src.engines.holiday_calendar._fetch_holidays_from_nse",
            new=AsyncMock(return_value=[]),
        ):
            await cal.refresh(calendar_year=2025)

        assert cal.calendar_status == _date.today().isoformat()

    @pytest.mark.asyncio
    async def test_not_updated_on_failed_refresh(self):
        cal = _fresh_calendar()

        with patch(
            "src.engines.holiday_calendar._fetch_holidays_from_nse",
            new=AsyncMock(side_effect=RuntimeError("fail")),
        ):
            await cal.refresh(calendar_year=2025)

        assert cal.calendar_status is None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_instance_returns_same_object(self):
        HolidayCalendar.reset_instance()
        a = HolidayCalendar.get_instance()
        b = HolidayCalendar.get_instance()
        assert a is b

    def test_reset_instance_creates_new_object(self):
        HolidayCalendar.reset_instance()
        a = HolidayCalendar.get_instance()
        HolidayCalendar.reset_instance()
        b = HolidayCalendar.get_instance()
        assert a is not b

    def test_get_instance_creates_fresh_calendar(self):
        HolidayCalendar.reset_instance()
        cal = HolidayCalendar.get_instance()
        assert cal.calendar_status is None
        assert cal.loaded_years() == []


# ---------------------------------------------------------------------------
# load_holidays (direct bootstrap)
# ---------------------------------------------------------------------------


class TestLoadHolidays:
    def test_load_holidays_makes_dates_non_trading(self):
        cal = _fresh_calendar()
        cal.load_holidays(2025, [date(2025, 3, 31), date(2025, 10, 2)])
        assert cal.is_trading_day(date(2025, 3, 31)) is False
        assert cal.is_trading_day(date(2025, 10, 2)) is False

    def test_load_holidays_returns_correct_count(self):
        cal = _fresh_calendar()
        holidays = [date(2025, 1, 14), date(2025, 8, 15)]
        cal.load_holidays(2025, holidays)
        assert cal.holiday_count(2025) == 2

    def test_load_holidays_overwrites_previous(self):
        cal = _fresh_calendar()
        cal.load_holidays(2025, [date(2025, 1, 14)])
        cal.load_holidays(2025, [date(2025, 8, 15)])
        # Old holiday should no longer be blocked
        assert cal.is_trading_day(date(2025, 1, 14)) is True
        assert cal.is_trading_day(date(2025, 8, 15)) is False

    def test_loaded_years_reports_correctly(self):
        cal = _fresh_calendar()
        cal.load_holidays(2024, [])
        cal.load_holidays(2025, [])
        assert 2024 in cal.loaded_years()
        assert 2025 in cal.loaded_years()


# ---------------------------------------------------------------------------
# Internal utility tests
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_round_trip(self):
        holidays = [date(2025, 1, 14), date(2025, 8, 15), date(2025, 10, 2)]
        raw = _serialise_holiday_set(holidays)
        recovered = _deserialise_holiday_set(raw)
        assert sorted(recovered) == sorted(holidays)

    def test_empty_list_round_trip(self):
        raw = _serialise_holiday_set([])
        recovered = _deserialise_holiday_set(raw)
        assert recovered == []

    def test_malformed_lines_are_skipped(self):
        raw = "2025-01-14\nNOT-A-DATE\n2025-08-15"
        recovered = _deserialise_holiday_set(raw)
        assert date(2025, 1, 14) in recovered
        assert date(2025, 8, 15) in recovered
        assert len(recovered) == 2


class TestParsNseDate:
    def test_dd_mon_yyyy_format(self):
        assert _parse_nse_date("01-Jan-2025") == date(2025, 1, 1)
        assert _parse_nse_date("14-Jan-2025") == date(2025, 1, 14)
        assert _parse_nse_date("31-Dec-2025") == date(2025, 12, 31)

    def test_iso_format(self):
        assert _parse_nse_date("2025-01-14") == date(2025, 1, 14)

    def test_leading_zero_day(self):
        assert _parse_nse_date("01-Aug-2025") == date(2025, 8, 1)

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse_nse_date("not-a-date")
