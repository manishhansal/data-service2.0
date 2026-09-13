"""
src/engines/holiday_calendar.py

NSE Holiday Calendar Management — fetches, caches, and serves the NSE
official trading holiday list.

Responsibilities:
- Fetch the NSE holiday list from the NSE public API once per calendar year
  (before the first trading session of each new year).
- Cache the result in Redis (L2) and in an in-process dict keyed by year.
- When the API is unavailable, retain the most recently fetched calendar and
  emit a structured warning.
- Expose helpers: ``is_trading_day``, ``next_trading_day``,
  ``get_holidays_for_month``, ``calendar_status``.
- Singleton pattern via ``get_instance()``.

NSE Holiday API endpoint:
    GET https://www.nseindia.com/api/holiday-master?type=trading

The API returns a JSON object whose keys are month abbreviations (e.g. ``"Jan"``)
and whose values are lists of holiday objects.  Each object includes at minimum:

    {
        "tradingDate": "01-Jan-2025",
        ...
    }

Requirements: 12.3, 12.4
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Official NSE trading holidays API endpoint.
_NSE_HOLIDAY_API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"

#: Redis key prefix for persisted holiday lists (per year).
_REDIS_KEY_PREFIX = "mds:holiday_calendar"

#: Number of seconds to cache holiday data in Redis (48 hours).
_REDIS_TTL_SEC = 172_800

#: HTTP timeout for NSE API requests (seconds).
_HTTP_TIMEOUT_SEC = 10.0

#: Headers that simulate a browser visit to avoid NSE WAF rejections.
_NSE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class HolidayCalendarUnavailableError(Exception):
    """Raised when the NSE holiday API is unreachable and no cached data exists."""


# ---------------------------------------------------------------------------
# HolidayCalendar
# ---------------------------------------------------------------------------


class HolidayCalendar:
    """Manages NSE trading holiday data for one or more calendar years.

    Provides:
    - ``is_trading_day(d)`` — True if *d* is not a weekend and not an NSE holiday.
    - ``next_trading_day(d)`` — next calendar date after *d* that is a trading day.
    - ``get_holidays_for_month(year, month)`` — sorted list of holiday dates in the month.
    - ``calendar_status`` — ISO date string of last successful refresh, or ``None``.
    - ``async refresh(year)`` — fetch / update the holiday list for *year* from NSE.
    - ``get_instance()`` — static singleton accessor.

    Thread / asyncio safety:
        ``_holidays`` is updated atomically via dict replacement.  Concurrent
        readers never see a partially populated dict for any given year.
    """

    _instance: Optional["HolidayCalendar"] = None

    def __init__(self) -> None:
        # year → frozenset[date]
        self._holidays: dict[int, frozenset[date]] = {}
        # Date of last successful refresh (any year)
        self._last_refresh_date: Optional[date] = None
        # Optional Redis client (injected by the application lifespan handler)
        self._redis: Optional[object] = None  # RedisClient | None

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @staticmethod
    def get_instance() -> "HolidayCalendar":
        """Return the process-level singleton instance (create if necessary)."""
        if HolidayCalendar._instance is None:
            HolidayCalendar._instance = HolidayCalendar()
        return HolidayCalendar._instance

    @staticmethod
    def reset_instance() -> None:
        """Reset the singleton (used in tests only)."""
        HolidayCalendar._instance = None

    # ------------------------------------------------------------------
    # Dependency injection
    # ------------------------------------------------------------------

    def set_redis_client(self, redis_client: object) -> None:
        """Inject a ``RedisClient`` for L2 persistence of holiday data.

        Args:
            redis_client: ``src.cache.redis_client.RedisClient`` instance.
        """
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------

    def is_trading_day(self, d: date) -> bool:
        """Return True if *d* is a valid NSE trading day.

        A date is a trading day when:
        1. It is a weekday (Monday–Friday, ``weekday() in 0..4``).
        2. It is NOT listed as an NSE holiday for its calendar year.

        If no holiday calendar has been loaded for the year of *d*, the method
        treats the day as a trading day (conservative default — avoids treating
        a valid trading day as a holiday due to a missing calendar).

        Args:
            d: The date to check (IST calendar date).

        Returns:
            ``True`` when *d* is a trading day; ``False`` otherwise.
        """
        # Weekend check: Monday=0 … Sunday=6
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            return False

        # Holiday check (only if calendar for this year has been loaded)
        year_holidays = self._holidays.get(d.year)
        if year_holidays is not None and d in year_holidays:
            return False

        return True

    def next_trading_day(self, d: date) -> date:
        """Return the earliest date *after* *d* that is a trading day.

        Iterates forward one day at a time.  In practice, the longest streak
        of consecutive non-trading days on the Indian calendar is short enough
        that this terminates quickly.

        Args:
            d: Reference date (IST calendar date).  The returned date is
               strictly *after* this date.

        Returns:
            The next trading day as a ``date`` object.
        """
        candidate = d + timedelta(days=1)
        # Safeguard: limit to 30 days (handles any realistic holiday cluster)
        for _ in range(30):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        # Fallback: should never happen in practice
        return candidate

    def get_holidays_for_month(self, year: int, month: int) -> list[date]:
        """Return NSE holiday dates within the given calendar month.

        Useful for the ``/v1/india/market/status`` endpoint which exposes
        remaining NSE holidays in the current IST calendar month.

        Args:
            year: Calendar year (e.g. 2025).
            month: Calendar month (1–12).

        Returns:
            Sorted list of ``date`` objects that are NSE holidays in the
            given month.  Returns an empty list when no calendar is loaded
            for *year*.
        """
        year_holidays = self._holidays.get(year, frozenset())
        return sorted(
            h for h in year_holidays if h.year == year and h.month == month
        )

    @property
    def calendar_status(self) -> Optional[str]:
        """ISO-8601 date string of the last successful refresh, or ``None``.

        Exposed via ``GET /v1/india/market/status`` as ``calendarStatus``
        (Requirement 12.4, 12.6).
        """
        if self._last_refresh_date is None:
            return None
        return self._last_refresh_date.isoformat()

    # ------------------------------------------------------------------
    # Refresh (async — called by scheduler and lifespan handler)
    # ------------------------------------------------------------------

    async def refresh(self, calendar_year: Optional[int] = None) -> None:
        """Fetch and cache the NSE holiday list for *calendar_year*.

        Strategy:
        1. Check in-process dict — if already populated and year matches, skip.
        2. Try Redis L2 cache — if hit, populate in-process dict and return.
        3. Fetch from NSE API — parse, populate dict, write to Redis.
        4. On failure: retain existing data, emit structured warning; do NOT raise.

        Args:
            calendar_year: Year to refresh.  Defaults to the current IST year
                (which is ``date.today().year`` — close enough for scheduling
                purposes since the scheduler calls this before the first session
                of the new year).
        """
        if calendar_year is None:
            calendar_year = date.today().year

        log = logger.getChild("refresh")
        log.info(
            "holiday_calendar_refresh_started",
            extra={
                "event": "holiday_calendar_refresh_started",
                "component": "holiday_calendar",
                "year": calendar_year,
            },
        )

        # ── 1. Try Redis L2 ───────────────────────────────────────────────
        redis_key = f"{_REDIS_KEY_PREFIX}:{calendar_year}"

        if self._redis is not None:
            try:
                cached_raw = await self._redis.get(redis_key)  # type: ignore[attr-defined]
                if cached_raw is not None:
                    holidays = _deserialise_holiday_set(cached_raw)
                    self._holidays[calendar_year] = frozenset(holidays)
                    self._last_refresh_date = date.today()
                    log.info(
                        "holiday_calendar_loaded_from_redis",
                        extra={
                            "event": "holiday_calendar_loaded_from_redis",
                            "component": "holiday_calendar",
                            "year": calendar_year,
                            "count": len(holidays),
                        },
                    )
                    return
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "holiday_calendar_redis_read_failed",
                    extra={
                        "event": "holiday_calendar_redis_read_failed",
                        "component": "holiday_calendar",
                        "year": calendar_year,
                        "error": str(exc),
                    },
                )

        # ── 2. Fetch from NSE API ─────────────────────────────────────────
        try:
            holidays = await _fetch_holidays_from_nse(calendar_year)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "holiday_calendar_fetch_failed",
                extra={
                    "event": "holiday_calendar_fetch_failed",
                    "component": "holiday_calendar",
                    "year": calendar_year,
                    "error": str(exc),
                    "action": "retaining_previous_calendar",
                    "previous_count": len(self._holidays.get(calendar_year, frozenset())),
                },
            )
            # Retain last successful calendar — do not overwrite with empty
            return

        # ── 3. Populate in-process dict ───────────────────────────────────
        self._holidays[calendar_year] = frozenset(holidays)
        self._last_refresh_date = date.today()

        # ── 4. Persist to Redis L2 ────────────────────────────────────────
        if self._redis is not None:
            try:
                serialised = _serialise_holiday_set(holidays)
                await self._redis.set_with_ttl(redis_key, serialised, _REDIS_TTL_SEC)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "holiday_calendar_redis_write_failed",
                    extra={
                        "event": "holiday_calendar_redis_write_failed",
                        "component": "holiday_calendar",
                        "year": calendar_year,
                        "error": str(exc),
                    },
                )

        log.info(
            "holiday_calendar_refreshed",
            extra={
                "event": "holiday_calendar_refreshed",
                "component": "holiday_calendar",
                "year": calendar_year,
                "holiday_count": len(holidays),
                "last_refresh_date": self._last_refresh_date.isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # Direct load (for testing / bootstrap without network)
    # ------------------------------------------------------------------

    def load_holidays(self, year: int, holidays: list[date]) -> None:
        """Directly populate the in-memory holiday set for *year*.

        Intended for:
        - Unit tests (inject known holidays without network calls).
        - Application bootstrap when serialised calendar data is restored
          from Redis by the lifespan handler.

        Args:
            year: Calendar year.
            holidays: List of NSE holiday dates for that year.
        """
        self._holidays[year] = frozenset(holidays)
        self._last_refresh_date = date.today()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def loaded_years(self) -> list[int]:
        """Return the list of calendar years for which holiday data is loaded."""
        return sorted(self._holidays.keys())

    def holiday_count(self, year: int) -> int:
        """Return the number of holidays loaded for *year* (0 if not loaded)."""
        return len(self._holidays.get(year, frozenset()))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch_holidays_from_nse(calendar_year: int) -> list[date]:
    """Fetch NSE trading holidays from the official NSE API.

    The API returns holidays for the current calendar year.  We request the
    page, parse all ``tradingDate`` fields, filter to *calendar_year*, and
    return a deduplicated sorted list.

    Args:
        calendar_year: Target year.  Dates from other years are discarded.

    Returns:
        Sorted list of holiday ``date`` objects for *calendar_year*.

    Raises:
        Exception: Any HTTP, JSON parse, or network error.
    """
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_SEC,
        headers=_NSE_HEADERS,
        follow_redirects=True,
    ) as client:
        response = await client.get(_NSE_HOLIDAY_API_URL)
        response.raise_for_status()
        data = response.json()

    holidays: list[date] = []

    # The API response is a dict keyed by month abbreviation.
    # Each value is a list of holiday objects with a "tradingDate" field.
    # Format example: "01-Jan-2025"
    if isinstance(data, dict):
        for _month_key, entries in data.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                raw_date = entry.get("tradingDate", "")
                try:
                    parsed = _parse_nse_date(raw_date)
                    if parsed.year == calendar_year:
                        holidays.append(parsed)
                except (ValueError, KeyError, AttributeError):
                    logger.debug(
                        "holiday_calendar_date_parse_skip",
                        extra={
                            "raw_date": raw_date,
                            "component": "holiday_calendar",
                        },
                    )
                    continue
    elif isinstance(data, list):
        # Some API variants return a flat list
        for entry in data:
            raw_date = entry.get("tradingDate", "")
            try:
                parsed = _parse_nse_date(raw_date)
                if parsed.year == calendar_year:
                    holidays.append(parsed)
            except (ValueError, KeyError, AttributeError):
                continue

    return sorted(set(holidays))


def _parse_nse_date(raw: str) -> date:
    """Parse an NSE date string to a ``date`` object.

    Handles the format ``DD-Mon-YYYY`` (e.g. ``"01-Jan-2025"``).

    Args:
        raw: Date string from the NSE API.

    Returns:
        Parsed ``date``.

    Raises:
        ValueError: When parsing fails.
    """
    from datetime import datetime  # local import to avoid circular issues

    raw = raw.strip()
    # Try DD-Mon-YYYY first (most common NSE format)
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse NSE date: {raw!r}")


def _serialise_holiday_set(holidays: list[date]) -> str:
    """Serialise a list of holiday dates to a newline-separated ISO string.

    Used for Redis persistence.

    Args:
        holidays: List of date objects.

    Returns:
        Newline-separated string of ISO-8601 date strings.
    """
    return "\n".join(d.isoformat() for d in sorted(holidays))


def _deserialise_holiday_set(raw: str) -> list[date]:
    """Deserialise a newline-separated ISO date string to a list of dates.

    Args:
        raw: Redis-stored string produced by ``_serialise_holiday_set``.

    Returns:
        List of ``date`` objects.  Malformed entries are silently skipped.
    """
    result: list[date] = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            result.append(date.fromisoformat(line))
        except ValueError:
            logger.debug(
                "holiday_calendar_deserialise_skip",
                extra={"raw_line": line, "component": "holiday_calendar"},
            )
    return result
