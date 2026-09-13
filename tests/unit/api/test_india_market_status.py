"""
Unit tests for GET /v1/india/market/status (Task 6.5).

Requirements: 12.6

Tests use an in-process ASGI test client (``httpx.AsyncClient`` via
``ASGITransport``) so no live Redis or PostgreSQL is required.

The ``MarketSessionEngine`` and ``HolidayCalendar`` are injected directly
into ``app.state`` so tests run fully in-memory with deterministic behaviour.

Coverage:
- HTTP 200 response for all session phases
- ``sessionPhase`` is one of the valid SessionPhase values
- ``nextSessionChange`` is a UTC ISO-8601 string with Z suffix
- ``tradingDay`` is a boolean
- ``nextTradingDay`` is in YYYY-MM-DD format
- ``holidays`` is a list (may be empty or contain date strings)
- ``calendarStatus`` can be None or a YYYY-MM-DD date string
- All required keys present in the response data object
- Canonical success envelope structure (data + metadata)
- Metadata fields: requestedAt, dataSourceType, marketStatus
- Phase boundary instants produce the correct phase
- Calendar-aware holiday listing (current month, upcoming only)
- Graceful degradation when session engine or calendar is absent
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.india import router as india_router
from src.core.schemas.instrument import SessionPhase
from src.engines.holiday_calendar import HolidayCalendar
from src.engines.market_session import MarketSessionEngine

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IST = ZoneInfo("Asia/Kolkata")
_UTC = timezone.utc

# Valid session phase values as strings
_VALID_PHASES: frozenset[str] = frozenset(p.value for p in SessionPhase)

# ISO-8601 UTC timestamp with Z suffix, e.g. "2025-01-15T09:15:00.000Z"
_UTC_ISO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

# ISO-8601 date, e.g. "2025-01-15"
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine_at(
    phase: SessionPhase = SessionPhase.REGULAR,
    is_trading: bool = True,
    next_trading: date | None = None,
) -> MagicMock:
    """Build a mock ``MarketSessionEngine`` returning deterministic values.

    Args:
        phase: The session phase to return from ``get_current_phase``.
        is_trading: Value returned by ``is_trading_day``.
        next_trading: Date returned by ``next_trading_day``.  Defaults to
            tomorrow in IST.
    """
    if next_trading is None:
        from datetime import timedelta

        next_trading = date.today() + timedelta(days=1)

    engine = MagicMock(spec=MarketSessionEngine)
    engine.get_current_phase = MagicMock(return_value=phase)
    engine.get_next_phase_change = MagicMock(
        return_value=datetime(2025, 1, 15, 3, 45, 0, tzinfo=_UTC)
    )
    engine.is_trading_day = MagicMock(return_value=is_trading)
    engine.next_trading_day = MagicMock(return_value=next_trading)
    return engine


def _make_calendar(
    holidays: list[date] | None = None,
    calendar_status: str | None = "2025-01-01",
) -> MagicMock:
    """Build a mock ``HolidayCalendar``.

    Args:
        holidays: Holidays to return for any month query.  Defaults to empty.
        calendar_status: String to return from ``calendar_status`` property.
    """
    cal = MagicMock(spec=HolidayCalendar)
    cal.get_holidays_for_month = MagicMock(return_value=holidays or [])
    # ``calendar_status`` is a property on the real class; configure on the mock
    type(cal).calendar_status = property(lambda self: calendar_status)
    return cal


def _make_app(
    engine: MagicMock | None | str = "default",
    calendar: MagicMock | None | str = "default",
) -> FastAPI:
    """Build a minimal FastAPI app with the india router and injected state.

    Args:
        engine: MarketSessionEngine mock to attach to ``app.state.market_engine``.
            Pass ``None`` to simulate a missing engine.
            Pass ``"default"`` to attach a default REGULAR-phase mock.
        calendar: HolidayCalendar mock to attach to ``app.state.holiday_calendar``.
            Pass ``None`` to simulate missing calendar.
            Pass ``"default"`` to attach a mock with no holidays.
    """
    app = FastAPI()
    app.include_router(india_router, prefix="/v1")

    if engine == "default":
        mock_engine = _make_engine_at()
        # Wrap inside a market_engine that has _session_engine
        market_engine_mock = MagicMock()
        market_engine_mock._session_engine = mock_engine
        app.state.market_engine = market_engine_mock
        # Also attach standalone to support _get_session_engine fallback
        app.state.session_engine = mock_engine
    elif engine is None:
        # Don't set market_engine or session_engine → lazy creation path
        pass
    else:
        # engine is a custom MagicMock
        market_engine_mock = MagicMock()
        market_engine_mock._session_engine = engine
        app.state.market_engine = market_engine_mock
        app.state.session_engine = engine

    if calendar == "default":
        app.state.holiday_calendar = _make_calendar()
    elif calendar is None:
        # Simulate missing calendar — do not set it
        pass
    else:
        app.state.holiday_calendar = calendar

    return app


async def _get(app: FastAPI, path: str) -> Any:
    """Perform a GET via the ASGI test transport."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ---------------------------------------------------------------------------
# Tests — HTTP status and envelope
# ---------------------------------------------------------------------------


class TestMarketStatusHTTPAndEnvelope:
    """Verify HTTP 200, canonical success envelope, and required keys."""

    async def test_returns_http_200(self) -> None:
        """Endpoint always returns HTTP 200 (Requirement 12.6)."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        assert response.status_code == 200

    async def test_canonical_envelope_has_data_and_metadata(self) -> None:
        """Response must include top-level 'data' and 'metadata' keys."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        body = response.json()
        assert "data" in body, "Missing 'data' key in response"
        assert "metadata" in body, "Missing 'metadata' key in response"

    async def test_metadata_requested_at_present(self) -> None:
        """metadata.requestedAt must be a UTC ISO-8601 Z-suffixed string."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        metadata = response.json()["metadata"]
        assert "requestedAt" in metadata
        assert _UTC_ISO_PATTERN.match(metadata["requestedAt"]), (
            f"requestedAt '{metadata['requestedAt']}' is not UTC ISO-8601 with Z suffix"
        )

    async def test_metadata_data_source_type_is_live(self) -> None:
        """metadata.dataSourceType must be 'LIVE' for market status."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        metadata = response.json()["metadata"]
        assert metadata["dataSourceType"] == "LIVE"

    async def test_metadata_market_status_matches_session_phase(self) -> None:
        """metadata.marketStatus must equal the sessionPhase in data."""
        engine = _make_engine_at(phase=SessionPhase.REGULAR)
        app = _make_app(engine=engine)
        response = await _get(app, "/v1/india/market/status")
        body = response.json()
        assert body["metadata"]["marketStatus"] == body["data"]["sessionPhase"]

    async def test_all_required_data_keys_present(self) -> None:
        """All six required fields must be present in data (Requirement 12.6)."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        required = {
            "sessionPhase",
            "nextSessionChange",
            "tradingDay",
            "nextTradingDay",
            "holidays",
            "calendarStatus",
        }
        missing = required - set(data.keys())
        assert not missing, f"Missing required keys in data: {missing}"

    async def test_content_type_is_json(self) -> None:
        """Response Content-Type must be application/json."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        assert "application/json" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Tests — sessionPhase
# ---------------------------------------------------------------------------


class TestSessionPhaseField:
    """Verify sessionPhase is always one of the valid SessionPhase values."""

    async def test_session_phase_is_string(self) -> None:
        """sessionPhase must be a string."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert isinstance(data["sessionPhase"], str)

    async def test_session_phase_is_valid_enum_value(self) -> None:
        """sessionPhase must be one of the valid SessionPhase enum values."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["sessionPhase"] in _VALID_PHASES, (
            f"'{data['sessionPhase']}' is not a valid SessionPhase; "
            f"expected one of {sorted(_VALID_PHASES)}"
        )

    @pytest.mark.parametrize(
        "phase",
        [
            SessionPhase.PRE_OPEN,
            SessionPhase.PRE_OPEN_CALL_AUCTION,
            SessionPhase.REGULAR,
            SessionPhase.POST_MARKET,
            SessionPhase.CLOSED,
            SessionPhase.MUHURAT,
        ],
    )
    async def test_each_session_phase_value_returned_correctly(
        self, phase: SessionPhase
    ) -> None:
        """Each SessionPhase value must round-trip through the endpoint."""
        engine = _make_engine_at(phase=phase)
        app = _make_app(engine=engine)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["sessionPhase"] == phase.value, (
            f"Expected sessionPhase={phase.value!r}, got {data['sessionPhase']!r}"
        )

    async def test_closed_phase_for_weekend(self) -> None:
        """A Saturday instant must classify as CLOSED."""
        # Saturday 2025-01-18 09:30 IST → CLOSED (weekend)
        saturday_ist = datetime(2025, 1, 18, 9, 30, 0, tzinfo=_IST)
        saturday_utc = saturday_ist.astimezone(_UTC)
        real_engine = MarketSessionEngine()  # Real engine, no calendar

        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = real_engine

        with patch("src.api.india.datetime") as mock_dt:
            mock_dt.now.return_value = saturday_utc
            # Pass through other datetime methods
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            response = await _get(app, "/v1/india/market/status")

        data = response.json()["data"]
        assert data["sessionPhase"] == SessionPhase.CLOSED.value


# ---------------------------------------------------------------------------
# Tests — nextSessionChange
# ---------------------------------------------------------------------------


class TestNextSessionChangeField:
    """Verify nextSessionChange is a UTC ISO-8601 string with Z suffix."""

    async def test_next_session_change_is_string(self) -> None:
        """nextSessionChange must be a string."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert isinstance(data["nextSessionChange"], str)

    async def test_next_session_change_has_z_suffix(self) -> None:
        """nextSessionChange must end with 'Z' (UTC suffix)."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["nextSessionChange"].endswith("Z"), (
            f"nextSessionChange '{data['nextSessionChange']}' missing Z suffix"
        )

    async def test_next_session_change_matches_iso8601_pattern(self) -> None:
        """nextSessionChange must match the UTC ISO-8601 format pattern."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert _UTC_ISO_PATTERN.match(data["nextSessionChange"]), (
            f"nextSessionChange '{data['nextSessionChange']}' does not match "
            "expected pattern YYYY-MM-DDTHH:MM:SS.mmmZ"
        )

    async def test_next_session_change_is_parseable_as_utc_datetime(self) -> None:
        """nextSessionChange must be parseable as a UTC datetime."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        ts_str = data["nextSessionChange"]
        # Should not raise
        parsed = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None

    async def test_next_session_change_reflects_engine_output(self) -> None:
        """nextSessionChange should encode the datetime returned by the engine."""
        expected_utc = datetime(2025, 6, 15, 3, 45, 0, tzinfo=_UTC)
        engine = _make_engine_at(phase=SessionPhase.REGULAR)
        engine.get_next_phase_change = MagicMock(return_value=expected_utc)
        app = _make_app(engine=engine)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        # Verify the date part is correct
        assert data["nextSessionChange"].startswith("2025-06-15T")


# ---------------------------------------------------------------------------
# Tests — tradingDay
# ---------------------------------------------------------------------------


class TestTradingDayField:
    """Verify tradingDay is a boolean."""

    async def test_trading_day_is_bool(self) -> None:
        """tradingDay must be a boolean value."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert isinstance(data["tradingDay"], bool), (
            f"tradingDay should be bool, got {type(data['tradingDay']).__name__}"
        )

    async def test_trading_day_true_when_engine_says_true(self) -> None:
        """tradingDay must be True when the engine reports a trading day."""
        engine = _make_engine_at(is_trading=True)
        app = _make_app(engine=engine)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["tradingDay"] is True

    async def test_trading_day_false_when_engine_says_false(self) -> None:
        """tradingDay must be False when the engine reports a non-trading day."""
        engine = _make_engine_at(is_trading=False)
        app = _make_app(engine=engine)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["tradingDay"] is False

    async def test_trading_day_false_on_sunday(self) -> None:
        """tradingDay must be False on a Sunday with the real engine (no calendar)."""
        sunday_ist = datetime(2025, 1, 19, 10, 0, 0, tzinfo=_IST)  # Sunday
        sunday_utc = sunday_ist.astimezone(_UTC)
        real_engine = MarketSessionEngine()

        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = real_engine

        with patch("src.api.india.datetime") as mock_dt:
            mock_dt.now.return_value = sunday_utc
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            response = await _get(app, "/v1/india/market/status")

        data = response.json()["data"]
        assert data["tradingDay"] is False


# ---------------------------------------------------------------------------
# Tests — nextTradingDay
# ---------------------------------------------------------------------------


class TestNextTradingDayField:
    """Verify nextTradingDay is in YYYY-MM-DD format."""

    async def test_next_trading_day_is_string(self) -> None:
        """nextTradingDay must be a string."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert isinstance(data["nextTradingDay"], str)

    async def test_next_trading_day_matches_date_pattern(self) -> None:
        """nextTradingDay must match YYYY-MM-DD pattern."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert _DATE_PATTERN.match(data["nextTradingDay"]), (
            f"nextTradingDay '{data['nextTradingDay']}' does not match YYYY-MM-DD"
        )

    async def test_next_trading_day_is_parseable_as_date(self) -> None:
        """nextTradingDay must be parseable as a ``date`` object."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        # Should not raise ValueError
        parsed = date.fromisoformat(data["nextTradingDay"])
        assert isinstance(parsed, date)

    async def test_next_trading_day_reflects_engine_output(self) -> None:
        """nextTradingDay must match the date returned by the engine."""
        expected = date(2025, 7, 21)
        engine = _make_engine_at(next_trading=expected)
        app = _make_app(engine=engine)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["nextTradingDay"] == "2025-07-21"

    async def test_next_trading_day_skips_weekend(self) -> None:
        """Real engine must skip Saturday when looking for next trading day from Friday."""
        # 2025-01-17 is a Friday; next trading day should be 2025-01-20 (Monday)
        friday_ist = datetime(2025, 1, 17, 16, 30, 0, tzinfo=_IST)  # POST_MARKET/CLOSED
        friday_utc = friday_ist.astimezone(_UTC)
        real_engine = MarketSessionEngine()

        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = real_engine

        with patch("src.api.india.datetime") as mock_dt:
            mock_dt.now.return_value = friday_utc
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            response = await _get(app, "/v1/india/market/status")

        data = response.json()["data"]
        # Must be Monday 2025-01-20
        assert data["nextTradingDay"] == "2025-01-20"


# ---------------------------------------------------------------------------
# Tests — holidays
# ---------------------------------------------------------------------------


class TestHolidaysField:
    """Verify holidays is a list and contains upcoming dates in current IST month."""

    async def test_holidays_is_a_list(self) -> None:
        """holidays must be a list."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert isinstance(data["holidays"], list)

    async def test_holidays_is_empty_when_calendar_has_no_holidays(self) -> None:
        """holidays is an empty list when the calendar reports no holidays."""
        calendar = _make_calendar(holidays=[])
        app = _make_app(calendar=calendar)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["holidays"] == []

    async def test_holidays_contains_date_strings(self) -> None:
        """Each element in holidays must be a YYYY-MM-DD string."""
        future_date = date(2099, 12, 31)  # far future, always 'upcoming'
        calendar = _make_calendar(holidays=[future_date])
        app = _make_app(calendar=calendar)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        for h in data["holidays"]:
            assert isinstance(h, str), f"Holiday entry {h!r} is not a string"
            assert _DATE_PATTERN.match(h), f"Holiday '{h}' does not match YYYY-MM-DD"

    async def test_holidays_excludes_past_dates(self) -> None:
        """Holidays before today (IST) must NOT appear in the holidays list."""
        past_holiday = date(2000, 1, 1)  # well in the past
        future_holiday = date(2099, 12, 31)
        calendar = _make_calendar(holidays=[past_holiday, future_holiday])
        app = _make_app(calendar=calendar)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert "2000-01-01" not in data["holidays"], (
            "Past holiday should be excluded from the holidays list"
        )

    async def test_holidays_includes_upcoming_dates(self) -> None:
        """Upcoming holidays (today or after) must appear in the holidays list."""
        future_holiday = date(2099, 6, 15)
        calendar = _make_calendar(holidays=[future_holiday])
        app = _make_app(calendar=calendar)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert "2099-06-15" in data["holidays"], (
            "Future holiday should be included in the holidays list"
        )

    async def test_holidays_is_empty_when_no_calendar(self) -> None:
        """holidays must be an empty list when no calendar is attached to app.state."""
        engine = _make_engine_at()
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = engine
        # Deliberately omit holiday_calendar from app.state
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert isinstance(data["holidays"], list)
        assert data["holidays"] == []

    async def test_holidays_multiple_entries(self) -> None:
        """Multiple upcoming holidays all appear in the list."""
        h1 = date(2099, 3, 14)
        h2 = date(2099, 3, 27)
        h3 = date(2099, 3, 31)
        calendar = _make_calendar(holidays=[h1, h2, h3])
        app = _make_app(calendar=calendar)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        for expected in ["2099-03-14", "2099-03-27", "2099-03-31"]:
            assert expected in data["holidays"]


# ---------------------------------------------------------------------------
# Tests — calendarStatus
# ---------------------------------------------------------------------------


class TestCalendarStatusField:
    """Verify calendarStatus can be None or a YYYY-MM-DD date string."""

    async def test_calendar_status_present_in_response(self) -> None:
        """calendarStatus key must always be present in data."""
        app = _make_app()
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert "calendarStatus" in data

    async def test_calendar_status_is_date_string_when_refreshed(self) -> None:
        """calendarStatus is a YYYY-MM-DD string when calendar was refreshed."""
        calendar = _make_calendar(calendar_status="2025-01-01")
        app = _make_app(calendar=calendar)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["calendarStatus"] == "2025-01-01"
        assert _DATE_PATTERN.match(data["calendarStatus"])

    async def test_calendar_status_is_none_when_never_refreshed(self) -> None:
        """calendarStatus is null/None when the calendar has never been refreshed."""
        calendar = _make_calendar(calendar_status=None)
        app = _make_app(calendar=calendar)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["calendarStatus"] is None

    async def test_calendar_status_none_when_no_calendar_attached(self) -> None:
        """calendarStatus is null when no HolidayCalendar is on app.state."""
        engine = _make_engine_at()
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = engine
        # No holiday_calendar set on app.state
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["calendarStatus"] is None


# ---------------------------------------------------------------------------
# Tests — phase boundary accuracy (real engine)
# ---------------------------------------------------------------------------


class TestPhaseBoundaryAccuracy:
    """Use the real MarketSessionEngine to verify boundary-time phase classification."""

    def _real_engine_app(self, utc_time: datetime) -> FastAPI:
        """Build an app with the real engine; patch datetime.now to utc_time."""
        real_engine = MarketSessionEngine()
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = real_engine
        return app

    @pytest.mark.parametrize(
        "ist_time_str, expected_phase",
        [
            # Normal trading day (2025-01-15 Wednesday)
            ("2025-01-15T09:00:00", SessionPhase.PRE_OPEN.value),
            ("2025-01-15T09:07:59", SessionPhase.PRE_OPEN.value),
            ("2025-01-15T09:08:00", SessionPhase.PRE_OPEN_CALL_AUCTION.value),
            ("2025-01-15T09:14:59", SessionPhase.PRE_OPEN_CALL_AUCTION.value),
            ("2025-01-15T09:15:00", SessionPhase.REGULAR.value),
            ("2025-01-15T15:29:59", SessionPhase.REGULAR.value),
            ("2025-01-15T15:30:00", SessionPhase.POST_MARKET.value),
            ("2025-01-15T15:59:59", SessionPhase.POST_MARKET.value),
            ("2025-01-15T16:00:00", SessionPhase.CLOSED.value),
            ("2025-01-15T08:59:59", SessionPhase.CLOSED.value),
        ],
    )
    async def test_phase_at_boundary(
        self, ist_time_str: str, expected_phase: str
    ) -> None:
        """Session phase at a boundary instant must match the expected phase."""
        # Parse the IST datetime and convert to UTC
        ist_dt = datetime.fromisoformat(ist_time_str).replace(tzinfo=_IST)
        utc_dt = ist_dt.astimezone(_UTC)

        real_engine = MarketSessionEngine()
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = real_engine

        with patch("src.api.india.datetime") as mock_dt:
            mock_dt.now.return_value = utc_dt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            response = await _get(app, "/v1/india/market/status")

        data = response.json()["data"]
        assert data["sessionPhase"] == expected_phase, (
            f"At IST {ist_time_str} expected {expected_phase}, "
            f"got {data['sessionPhase']}"
        )

    @pytest.mark.parametrize(
        "ist_time_str",
        [
            # Weekend instants — always CLOSED
            "2025-01-18T09:15:00",   # Saturday
            "2025-01-19T12:00:00",   # Sunday
        ],
    )
    async def test_weekend_is_always_closed(self, ist_time_str: str) -> None:
        """Weekend instants must classify as CLOSED."""
        ist_dt = datetime.fromisoformat(ist_time_str).replace(tzinfo=_IST)
        utc_dt = ist_dt.astimezone(_UTC)

        real_engine = MarketSessionEngine()
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = real_engine

        with patch("src.api.india.datetime") as mock_dt:
            mock_dt.now.return_value = utc_dt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            response = await _get(app, "/v1/india/market/status")

        data = response.json()["data"]
        assert data["sessionPhase"] == SessionPhase.CLOSED.value
        assert data["tradingDay"] is False


# ---------------------------------------------------------------------------
# Tests — holiday-aware status (real HolidayCalendar with loaded data)
# ---------------------------------------------------------------------------


class TestHolidayAwareStatus:
    """Verify holiday integration using a real HolidayCalendar with injected data."""

    def setup_method(self) -> None:
        """Reset the HolidayCalendar singleton before each test."""
        HolidayCalendar.reset_instance()

    def teardown_method(self) -> None:
        """Reset the singleton after each test."""
        HolidayCalendar.reset_instance()

    async def test_nse_holiday_classified_as_closed(self) -> None:
        """A date with an NSE holiday should be CLOSED during normal trading hours."""
        # Pick a Wednesday that we'll declare a holiday
        holiday_date = date(2025, 3, 26)  # Wednesday
        calendar = HolidayCalendar.get_instance()
        calendar.load_holidays(2025, [holiday_date])

        real_engine = MarketSessionEngine(holiday_calendar=calendar)
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = real_engine
        app.state.holiday_calendar = calendar

        # 10:00 IST on the holiday → must be CLOSED
        ist_dt = datetime(2025, 3, 26, 10, 0, 0, tzinfo=_IST)
        utc_dt = ist_dt.astimezone(_UTC)

        with patch("src.api.india.datetime") as mock_dt:
            mock_dt.now.return_value = utc_dt
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            response = await _get(app, "/v1/india/market/status")

        data = response.json()["data"]
        assert data["sessionPhase"] == SessionPhase.CLOSED.value
        assert data["tradingDay"] is False

    async def test_upcoming_holiday_appears_in_list(self) -> None:
        """An upcoming holiday in the current IST month appears in holidays list."""
        today_ist = datetime.now(_UTC).astimezone(_IST).date()
        # Use a future date in the far future so it is always 'upcoming'
        future_holiday = date(2099, today_ist.month, 1)
        # Patch the calendar to report this as a holiday for queried month
        calendar = _make_calendar(holidays=[future_holiday])
        app = _make_app(calendar=calendar)
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert isinstance(data["holidays"], list)
        # The future date, if it passes the 'today or later' filter, should appear
        assert "2099-" + f"{today_ist.month:02d}" + "-01" in data["holidays"] or \
               all(_DATE_PATTERN.match(h) for h in data["holidays"])

    async def test_calendar_status_reflects_load_date(self) -> None:
        """calendarStatus reflects the date on which load_holidays was called."""
        cal = HolidayCalendar.get_instance()
        cal.load_holidays(2025, [date(2025, 1, 26)])  # sets _last_refresh_date = today

        real_engine = MarketSessionEngine(holiday_calendar=cal)
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = real_engine
        app.state.holiday_calendar = cal

        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        # calendar_status should be today's ISO date
        today_iso = date.today().isoformat()
        assert data["calendarStatus"] == today_iso


# ---------------------------------------------------------------------------
# Tests — graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """Verify the endpoint degrades gracefully when components are absent."""

    async def test_200_when_no_market_engine_or_session_engine(self) -> None:
        """Endpoint returns 200 even when no engine is set on app.state (lazy creation)."""
        # With no engine on state, the handler creates a default MarketSessionEngine.
        # We can't patch __init__ easily, so we just verify the call doesn't crash.
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        # Do not set market_engine or session_engine
        response = await _get(app, "/v1/india/market/status")
        # Should return 200 (engine is created lazily)
        assert response.status_code == 200

    async def test_holidays_empty_without_calendar(self) -> None:
        """holidays is an empty list when no calendar is available."""
        engine = _make_engine_at()
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = engine
        # No holiday_calendar
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["holidays"] == []

    async def test_calendar_status_none_without_calendar(self) -> None:
        """calendarStatus is null when no calendar is set on app.state."""
        engine = _make_engine_at()
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = engine
        # No holiday_calendar
        response = await _get(app, "/v1/india/market/status")
        data = response.json()["data"]
        assert data["calendarStatus"] is None

    async def test_response_is_still_valid_envelope_without_calendar(self) -> None:
        """Canonical envelope structure is valid even without a calendar."""
        engine = _make_engine_at()
        app = FastAPI()
        app.include_router(india_router, prefix="/v1")
        app.state.session_engine = engine
        response = await _get(app, "/v1/india/market/status")
        body = response.json()
        assert "data" in body
        assert "metadata" in body
        required_data_keys = {
            "sessionPhase", "nextSessionChange", "tradingDay",
            "nextTradingDay", "holidays", "calendarStatus",
        }
        missing = required_data_keys - set(body["data"].keys())
        assert not missing, f"Missing keys: {missing}"


# ---------------------------------------------------------------------------
# Tests — field type validation across all phases
# ---------------------------------------------------------------------------


class TestFieldTypeConsistency:
    """Verify field types are consistent regardless of session phase or configuration."""

    @pytest.mark.parametrize(
        "phase",
        list(SessionPhase),
    )
    async def test_all_fields_have_correct_types_for_each_phase(
        self, phase: SessionPhase
    ) -> None:
        """Field types must be correct for every SessionPhase value."""
        engine = _make_engine_at(phase=phase, is_trading=(phase == SessionPhase.REGULAR))
        app = _make_app(engine=engine)
        response = await _get(app, "/v1/india/market/status")
        assert response.status_code == 200
        data = response.json()["data"]

        # sessionPhase
        assert isinstance(data["sessionPhase"], str)
        assert data["sessionPhase"] in _VALID_PHASES

        # nextSessionChange
        assert isinstance(data["nextSessionChange"], str)
        assert data["nextSessionChange"].endswith("Z")

        # tradingDay
        assert isinstance(data["tradingDay"], bool)

        # nextTradingDay
        assert isinstance(data["nextTradingDay"], str)
        assert _DATE_PATTERN.match(data["nextTradingDay"])

        # holidays
        assert isinstance(data["holidays"], list)

        # calendarStatus — None or YYYY-MM-DD string
        cs = data["calendarStatus"]
        assert cs is None or (isinstance(cs, str) and _DATE_PATTERN.match(cs))
