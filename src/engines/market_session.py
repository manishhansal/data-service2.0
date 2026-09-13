"""
src/engines/market_session.py

NSE Market Session Engine — classifies any UTC datetime into an NSE session
phase and provides trading-day utilities.

Design invariants:
  - ``zoneinfo.ZoneInfo("Asia/Kolkata")`` is the SOLE reference for IST
    conversion.  No other IST construction (e.g. ``timedelta(hours=5,
    minutes=30)``) is used.
  - All inputs and outputs (except where explicitly documented) are UTC.
  - IST interpretation occurs ONLY inside this module.

Session phase boundaries (IST, inclusive start, exclusive end):
  PRE_OPEN               09:00 – 09:08
  PRE_OPEN_CALL_AUCTION  09:08 – 09:15
  REGULAR                09:15 – 15:30  (13:00 on NSE half-days)
  POST_MARKET            15:30 – 16:00  (13:00 – 13:30 on NSE half-days)
  CLOSED                 all other instants, NSE holidays, weekends
  MUHURAT                Diwali Muhurat session (special case, future extension)

On full NSE holidays: always CLOSED.
On NSE-designated half-trading days:
  REGULAR  ends at 13:00 IST.
  POST_MARKET spans 13:00 – 13:30 IST.
  CLOSED otherwise.

Requirements: 12.1, 12.2
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from src.core.schemas.instrument import SessionPhase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IST timezone — the SOLE reference (Requirement 12.1)
# ---------------------------------------------------------------------------

_IST = ZoneInfo("Asia/Kolkata")
_UTC = timezone.utc

# ---------------------------------------------------------------------------
# Session boundary times (IST) — all boundaries are (inclusive_start, exclusive_end)
# ---------------------------------------------------------------------------

# Normal trading day boundaries
_T_PRE_OPEN_START = time(9, 0)          # 09:00 IST  PRE_OPEN starts
_T_PRE_OPEN_END = time(9, 8)            # 09:08 IST  PRE_OPEN ends / PRE_OPEN_CALL_AUCTION starts
_T_CALL_AUCTION_END = time(9, 15)       # 09:15 IST  PRE_OPEN_CALL_AUCTION ends / REGULAR starts
_T_REGULAR_END = time(15, 30)           # 15:30 IST  REGULAR ends on normal day
_T_POST_MARKET_END = time(16, 0)        # 16:00 IST  POST_MARKET ends on normal day

# Half-day trading day boundaries
_T_HALF_REGULAR_END = time(13, 0)       # 13:00 IST  REGULAR ends on half-day
_T_HALF_POST_MARKET_END = time(13, 30)  # 13:30 IST  POST_MARKET ends on half-day

# ---------------------------------------------------------------------------
# Half-day dates — a static set of known NSE half-trading days.
# The scheduler fetches the updated list; this stub covers known patterns.
# In production, the HolidayCalendar should expose a ``is_half_day`` method.
# For now, this set is intentionally empty; actual half-days will be added
# when the NSE calendar API integration is complete.
# ---------------------------------------------------------------------------

#: Known half-trading days (IST calendar dates).
#: Populated externally by the HolidayCalendar once the full API integration
#: is complete.  Tests inject dates directly into ``HALF_TRADING_DAYS``.
HALF_TRADING_DAYS: set[date] = set()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class MarketSessionEngine:
    """Classifies UTC datetimes into NSE session phases.

    Usage::

        engine = MarketSessionEngine()
        phase = engine.get_current_phase()          # uses datetime.now(UTC)
        next_change = engine.get_next_phase_change() # UTC datetime

    The engine holds a reference to a ``HolidayCalendar`` to determine
    whether a given IST date is a trading day or a holiday.  If the calendar
    is ``None`` (unavailable), the engine assumes every weekday is a trading
    day (conservative — may treat a holiday as a trading day, but never
    crashes).

    Thread / asyncio safety: all methods are pure functions over immutable
    state; no shared mutable data exists.
    """

    def __init__(
        self,
        holiday_calendar: Optional[object] = None,
    ) -> None:
        """Initialise the engine.

        Args:
            holiday_calendar: Optional ``HolidayCalendar`` instance used for
                holiday and half-day lookups.  When ``None``, only weekend
                checks are applied.
        """
        self._calendar: Optional[object] = holiday_calendar

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current_phase(
        self, dt_utc: Optional[datetime] = None
    ) -> SessionPhase:
        """Classify *dt_utc* into exactly one NSE session phase.

        Every UTC instant maps to exactly one ``SessionPhase``.  No instant
        maps to two phases, and no instant maps to zero phases.

        Args:
            dt_utc: UTC datetime to classify.  When ``None``, uses
                ``datetime.now(UTC)`` (current wall clock time).

        Returns:
            The ``SessionPhase`` for the given instant.
        """
        if dt_utc is None:
            dt_utc = datetime.now(_UTC)

        # Ensure we have a timezone-aware UTC datetime
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=_UTC)

        ist_dt = dt_utc.astimezone(_IST)
        ist_date = ist_dt.date()
        ist_time = ist_dt.time()

        # ── 1. Non-trading day → CLOSED ───────────────────────────────────
        if not self._is_trading_day_internal(ist_date):
            return SessionPhase.CLOSED

        # ── 2. Classify by session boundaries ────────────────────────────
        is_half_day = ist_date in HALF_TRADING_DAYS

        if is_half_day:
            return _classify_half_day(ist_time)
        else:
            return _classify_normal_day(ist_time)

    def get_next_phase_change(
        self, dt_utc: Optional[datetime] = None
    ) -> datetime:
        """Return the UTC datetime of the next NSE session phase boundary.

        Scans forward from *dt_utc* to find the next IST time that crosses a
        session phase boundary.

        Args:
            dt_utc: UTC datetime.  Defaults to ``datetime.now(UTC)``.

        Returns:
            UTC-aware ``datetime`` of the next phase boundary.  The returned
            datetime is the first future instant whose phase differs from the
            phase at *dt_utc*.
        """
        if dt_utc is None:
            dt_utc = datetime.now(_UTC)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=_UTC)

        current_phase = self.get_current_phase(dt_utc)

        # Scan forward in 1-minute increments (maximum 8 days)
        # This handles weekends and holiday clusters correctly.
        candidate = dt_utc + timedelta(minutes=1)
        max_steps = 8 * 24 * 60  # 8 days in 1-minute steps

        for _ in range(max_steps):
            if self.get_current_phase(candidate) != current_phase:
                # Found boundary — round back to the canonical boundary second
                return _snap_to_boundary(candidate, current_phase)
            candidate += timedelta(minutes=1)

        # Fallback: return candidate as-is (should never be reached)
        return candidate

    def is_trading_day(self, dt_utc: Optional[datetime] = None) -> bool:
        """Return ``True`` if the IST date of *dt_utc* has (or will have) a REGULAR session.

        Args:
            dt_utc: UTC datetime.  Defaults to ``datetime.now(UTC)``.

        Returns:
            ``True`` when the IST date is a trading day (weekday + not a
            public holiday); ``False`` for weekends and NSE holidays.
        """
        if dt_utc is None:
            dt_utc = datetime.now(_UTC)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=_UTC)
        ist_date = dt_utc.astimezone(_IST).date()
        return self._is_trading_day_internal(ist_date)

    def next_trading_day(self, dt_utc: Optional[datetime] = None) -> date:
        """Return the next IST date that has a REGULAR session.

        Returns the date immediately following the IST date of *dt_utc* that
        is a trading day.

        Args:
            dt_utc: UTC datetime.  Defaults to ``datetime.now(UTC)``.

        Returns:
            Next IST trading date as a ``date`` object.
        """
        if dt_utc is None:
            dt_utc = datetime.now(_UTC)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=_UTC)
        ist_date = dt_utc.astimezone(_IST).date()

        if self._calendar is not None and hasattr(self._calendar, "next_trading_day"):
            return self._calendar.next_trading_day(ist_date)  # type: ignore[union-attr]

        # Fallback: skip weekends only
        candidate = ist_date + timedelta(days=1)
        for _ in range(30):
            if candidate.weekday() < 5:  # 0–4 = Mon–Fri
                return candidate
            candidate += timedelta(days=1)
        return candidate

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_trading_day_internal(self, ist_date: date) -> bool:
        """Return True when *ist_date* is a trading day.

        Delegates to ``HolidayCalendar`` when available; otherwise applies
        only a weekend check (conservative default per requirement 12.1).
        """
        if self._calendar is not None and hasattr(self._calendar, "is_trading_day"):
            return self._calendar.is_trading_day(ist_date)  # type: ignore[union-attr]

        # Calendar unavailable — fall back to weekend check only
        return ist_date.weekday() < 5  # 0–4 = Mon–Fri


# ---------------------------------------------------------------------------
# Phase classification functions
# ---------------------------------------------------------------------------


def _classify_normal_day(ist_time: time) -> SessionPhase:
    """Classify an IST time on a normal (full) trading day.

    Boundaries are inclusive at start, exclusive at end:
      [09:00, 09:08) → PRE_OPEN
      [09:08, 09:15) → PRE_OPEN_CALL_AUCTION
      [09:15, 15:30) → REGULAR
      [15:30, 16:00) → POST_MARKET
      otherwise       → CLOSED

    Args:
        ist_time: Time component of the IST datetime (no timezone).

    Returns:
        Appropriate ``SessionPhase``.
    """
    if _T_PRE_OPEN_START <= ist_time < _T_PRE_OPEN_END:
        return SessionPhase.PRE_OPEN
    if _T_PRE_OPEN_END <= ist_time < _T_CALL_AUCTION_END:
        return SessionPhase.PRE_OPEN_CALL_AUCTION
    if _T_CALL_AUCTION_END <= ist_time < _T_REGULAR_END:
        return SessionPhase.REGULAR
    if _T_REGULAR_END <= ist_time < _T_POST_MARKET_END:
        return SessionPhase.POST_MARKET
    return SessionPhase.CLOSED


def _classify_half_day(ist_time: time) -> SessionPhase:
    """Classify an IST time on an NSE half-trading day.

    Boundaries:
      [09:00, 09:08) → PRE_OPEN
      [09:08, 09:15) → PRE_OPEN_CALL_AUCTION
      [09:15, 13:00) → REGULAR
      [13:00, 13:30) → POST_MARKET
      otherwise       → CLOSED

    Args:
        ist_time: Time component of the IST datetime.

    Returns:
        Appropriate ``SessionPhase``.
    """
    if _T_PRE_OPEN_START <= ist_time < _T_PRE_OPEN_END:
        return SessionPhase.PRE_OPEN
    if _T_PRE_OPEN_END <= ist_time < _T_CALL_AUCTION_END:
        return SessionPhase.PRE_OPEN_CALL_AUCTION
    if _T_CALL_AUCTION_END <= ist_time < _T_HALF_REGULAR_END:
        return SessionPhase.REGULAR
    if _T_HALF_REGULAR_END <= ist_time < _T_HALF_POST_MARKET_END:
        return SessionPhase.POST_MARKET
    return SessionPhase.CLOSED


def _snap_to_boundary(
    candidate_utc: datetime, previous_phase: SessionPhase
) -> datetime:
    """Snap a candidate UTC datetime back to the precise phase boundary second.

    When scanning forward by 1-minute increments we may overshoot a boundary.
    This function converts the candidate IST time back to the exact boundary
    that was crossed and returns that as a UTC-aware datetime.

    If the exact boundary cannot be determined (e.g. MUHURAT or unexpected
    CLOSED transitions), the candidate itself is returned unchanged.

    Args:
        candidate_utc: UTC datetime that crossed a boundary (may be ≤ 1 min
            past the actual boundary).
        previous_phase: The phase that was active before the crossing.

    Returns:
        UTC-aware datetime of the precise boundary.
    """
    candidate_ist = candidate_utc.astimezone(_IST)
    ist_date = candidate_ist.date()

    is_half_day = ist_date in HALF_TRADING_DAYS

    # Map phase → its end boundary time on a normal or half day
    if previous_phase == SessionPhase.CLOSED:
        # CLOSED can end at PRE_OPEN start (09:00) on a trading day
        boundary_ist_time = _T_PRE_OPEN_START
    elif previous_phase == SessionPhase.PRE_OPEN:
        boundary_ist_time = _T_PRE_OPEN_END
    elif previous_phase == SessionPhase.PRE_OPEN_CALL_AUCTION:
        boundary_ist_time = _T_CALL_AUCTION_END
    elif previous_phase == SessionPhase.REGULAR:
        boundary_ist_time = _T_HALF_REGULAR_END if is_half_day else _T_REGULAR_END
    elif previous_phase == SessionPhase.POST_MARKET:
        boundary_ist_time = _T_HALF_POST_MARKET_END if is_half_day else _T_POST_MARKET_END
    else:
        # MUHURAT or unknown — return candidate as-is
        return candidate_utc

    boundary_ist = datetime.combine(ist_date, boundary_ist_time, tzinfo=_IST)
    # If boundary is in the past of candidate (we overshot), shift to next day boundary
    if boundary_ist <= candidate_ist - timedelta(minutes=2):
        # Find the next appropriate boundary date
        next_date = ist_date + timedelta(days=1)
        boundary_ist = datetime.combine(next_date, boundary_ist_time, tzinfo=_IST)

    return boundary_ist.astimezone(_UTC)
