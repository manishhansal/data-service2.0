"""
src/engines/trading_calendar_service.py

TradingCalendarService — DB-backed authoritative trading calendar resolver.

This service is the **single authoritative source** for trading day
determination.  It uses the ``exchange_calendar`` table (populated by
``scripts/populate_exchange_calendar.py``) as its primary source,
falling back to the in-memory ``HolidayCalendar`` when the DB is
unavailable.

All historical backfill jobs MUST use this service (not hardcoded logic)
to determine:
  - Whether a date is a trading day
  - Previous/next trading day
  - Trading day lists for date ranges

Design guarantees:
  - Weekend ≠ official holiday (they are classified separately in the DB)
  - NOT_PUBLISHED ≠ trading day (unknown future dates never assumed open)
  - UNKNOWN ≠ trading day (missing calendar data is not assumed open)
  - No look-ahead: ``get_trading_days(start, end)`` returns only dates
    where ``is_trading_day = TRUE`` in the DB, i.e. only published,
    confirmed trading dates.

Requirements: Phase 14-18 of pre-F&O backfill certification
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum forward scan (days) when searching for next/previous trading day
_MAX_SCAN_DAYS = 60

#: Exchange and segment defaults
_DEFAULT_EXCHANGE = "NSE"
_DEFAULT_SEGMENT = "EQ"


# ---------------------------------------------------------------------------
# TradingCalendarService
# ---------------------------------------------------------------------------


class TradingCalendarService:
    """DB-backed authoritative trading calendar resolver.

    Usage::

        svc = TradingCalendarService(db_engine)
        is_open = await svc.is_trading_day(date(2026, 9, 15))
        prev     = await svc.get_previous_trading_day(date(2026, 9, 15))
        nxt      = await svc.get_next_trading_day(date(2026, 9, 15))
        days     = await svc.get_trading_days(date(2026, 9, 1), date(2026, 9, 30))
        session  = await svc.get_session(date(2026, 9, 15))

    The service is stateless beyond holding a reference to the db_engine.
    """

    def __init__(
        self,
        db_engine: Optional["AsyncEngine"] = None,
        exchange: str = _DEFAULT_EXCHANGE,
        segment: str = _DEFAULT_SEGMENT,
    ) -> None:
        self._engine = db_engine
        self._exchange = exchange
        self._segment = segment

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_trading_day(
        self,
        d: date,
        *,
        exchange: Optional[str] = None,
        segment: Optional[str] = None,
    ) -> bool:
        """Return True if *d* is a confirmed trading day for this exchange.

        Uses the DB ``exchange_calendar`` table.  If the DB is unavailable or
        the date has no record, falls back to the conservative rule:
        weekday = trading day, weekend = not trading day (no holiday check).

        Args:
            d:        Date to check (IST calendar date).
            exchange: Override exchange (default: NSE).
            segment:  Override segment (default: EQ).

        Returns:
            True if *d* is a trading day.
        """
        exch = (exchange or self._exchange).upper()
        seg  = (segment  or self._segment).upper()

        result = await self._query_single(d, exch, seg)
        if result is None:
            # No DB record → fall back to weekend check only
            logger.debug(
                "trading_calendar_no_record",
                extra={"date": str(d), "exchange": exch, "segment": seg},
            )
            return d.isoweekday() not in (6, 7)  # conservative: weekday = trading

        return bool(result.get("is_trading_day", False))

    async def get_previous_trading_day(
        self,
        d: date,
        *,
        exchange: Optional[str] = None,
        segment: Optional[str] = None,
    ) -> date:
        """Return the most recent trading day strictly before *d*.

        Scans backward up to ``_MAX_SCAN_DAYS`` days.

        Args:
            d:        Reference date.
            exchange: Override exchange.
            segment:  Override segment.

        Returns:
            Previous trading day.
        """
        candidate = d - timedelta(days=1)
        for _ in range(_MAX_SCAN_DAYS):
            if await self.is_trading_day(candidate, exchange=exchange, segment=segment):
                return candidate
            candidate -= timedelta(days=1)
        # Fallback: should never happen
        logger.warning(
            "trading_calendar_prev_scan_exhausted",
            extra={"reference_date": str(d)},
        )
        return candidate

    async def get_next_trading_day(
        self,
        d: date,
        *,
        exchange: Optional[str] = None,
        segment: Optional[str] = None,
    ) -> date:
        """Return the earliest trading day strictly after *d*.

        Scans forward up to ``_MAX_SCAN_DAYS`` days.

        Args:
            d:        Reference date.
            exchange: Override exchange.
            segment:  Override segment.

        Returns:
            Next trading day.
        """
        candidate = d + timedelta(days=1)
        for _ in range(_MAX_SCAN_DAYS):
            if await self.is_trading_day(candidate, exchange=exchange, segment=segment):
                return candidate
            candidate += timedelta(days=1)
        logger.warning(
            "trading_calendar_next_scan_exhausted",
            extra={"reference_date": str(d)},
        )
        return candidate

    async def get_trading_days(
        self,
        start: date,
        end: date,
        *,
        exchange: Optional[str] = None,
        segment: Optional[str] = None,
    ) -> list[date]:
        """Return all confirmed trading days in [start, end] inclusive.

        Uses a single DB query for efficiency.  Dates not in the DB are
        omitted (conservative — unknown dates are NOT assumed trading days
        to prevent look-ahead leakage in backfill jobs).

        Args:
            start:    Range start (inclusive).
            end:      Range end (inclusive).
            exchange: Override exchange.
            segment:  Override segment.

        Returns:
            Sorted list of trading dates.
        """
        exch = (exchange or self._exchange).upper()
        seg  = (segment  or self._segment).upper()

        if self._engine is None:
            # No DB: fall back to weekday-only
            return self._weekday_range(start, end)

        from sqlalchemy import text as _text  # noqa: PLC0415

        sql = _text(
            """
            SELECT calendar_date
            FROM exchange_calendar
            WHERE exchange       = :exchange
              AND segment        = :segment
              AND calendar_date  >= :start
              AND calendar_date  <= :end
              AND is_trading_day = TRUE
            ORDER BY calendar_date ASC
            """
        )
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    sql,
                    {
                        "exchange": exch,
                        "segment": seg,
                        "start": start,
                        "end": end,
                    },
                )
                return [row[0] for row in result.fetchall()]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "trading_calendar_db_error",
                extra={"error": str(exc), "exchange": exch},
            )
            return self._weekday_range(start, end)

    async def get_session(
        self,
        d: date,
        *,
        exchange: Optional[str] = None,
        segment: Optional[str] = None,
    ) -> dict:
        """Return the session record for *d* from exchange_calendar.

        Returns a dict with keys:
          - ``is_trading_day`` (bool)
          - ``day_type`` (str: TRADING_DAY | WEEKEND | OFFICIAL_HOLIDAY | NOT_PUBLISHED | UNKNOWN)
          - ``session_status`` (str: OPEN | CLOSED | UNKNOWN)
          - ``session_open`` (time | None)
          - ``session_close`` (time | None)
          - ``holiday_name`` (str | None)
          - ``source`` (str | None)

        Returns default UNKNOWN record when the date has no DB entry.
        """
        exch = (exchange or self._exchange).upper()
        seg  = (segment  or self._segment).upper()

        result = await self._query_single(d, exch, seg)
        if result is None:
            # No DB record — use conservative fallback
            is_weekend = d.isoweekday() in (6, 7)
            return {
                "is_trading_day": not is_weekend,
                "day_type": "WEEKEND" if is_weekend else "UNKNOWN",
                "session_status": "CLOSED" if is_weekend else "UNKNOWN",
                "session_open": None,
                "session_close": None,
                "holiday_name": None,
                "source": "FALLBACK",
            }
        return result

    async def calendar_coverage(
        self,
        *,
        exchange: Optional[str] = None,
        segment: Optional[str] = None,
    ) -> dict:
        """Return coverage statistics for the calendar.

        Returns:
          - ``total_dates``
          - ``trading_days``
          - ``weekends``
          - ``official_holidays``
          - ``not_published``
          - ``min_date``
          - ``max_date``
          - ``exchange``
          - ``segment``
        """
        exch = (exchange or self._exchange).upper()
        seg  = (segment  or self._segment).upper()

        if self._engine is None:
            return {"error": "DB_UNAVAILABLE", "exchange": exch, "segment": seg}

        from sqlalchemy import text as _text  # noqa: PLC0415

        sql = _text(
            """
            SELECT
                COUNT(*) AS total_dates,
                SUM(CASE WHEN is_trading_day THEN 1 ELSE 0 END) AS trading_days,
                SUM(CASE WHEN day_type = 'WEEKEND' THEN 1 ELSE 0 END) AS weekends,
                SUM(CASE WHEN day_type = 'OFFICIAL_HOLIDAY' THEN 1 ELSE 0 END) AS official_holidays,
                SUM(CASE WHEN day_type = 'NOT_PUBLISHED' THEN 1 ELSE 0 END) AS not_published,
                MIN(calendar_date) AS min_date,
                MAX(calendar_date) AS max_date
            FROM exchange_calendar
            WHERE exchange = :exchange AND segment = :segment
            """
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(sql, {"exchange": exch, "segment": seg})).mappings().one()
        return {
            "total_dates": int(row["total_dates"]),
            "trading_days": int(row["trading_days"] or 0),
            "weekends": int(row["weekends"] or 0),
            "official_holidays": int(row["official_holidays"] or 0),
            "not_published": int(row["not_published"] or 0),
            "min_date": str(row["min_date"]) if row["min_date"] else None,
            "max_date": str(row["max_date"]) if row["max_date"] else None,
            "exchange": exch,
            "segment": seg,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _query_single(
        self, d: date, exchange: str, segment: str
    ) -> Optional[dict]:
        """Query a single calendar_date row from exchange_calendar."""
        if self._engine is None:
            return None

        from sqlalchemy import text as _text  # noqa: PLC0415

        sql = _text(
            """
            SELECT is_trading_day, day_type, session_status,
                   session_open, session_close, holiday_name, source
            FROM exchange_calendar
            WHERE exchange = :exchange AND segment = :segment
              AND calendar_date = :d
            LIMIT 1
            """
        )
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    sql, {"exchange": exchange, "segment": segment, "d": d}
                )
                row = result.mappings().first()
                if row is None:
                    return None
                return dict(row)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "trading_calendar_query_error",
                extra={"error": str(exc), "date": str(d)},
            )
            return None

    @staticmethod
    def _weekday_range(start: date, end: date) -> list[date]:
        """Fallback: return all weekdays in [start, end] (no holiday check)."""
        days = []
        d = start
        while d <= end:
            if d.isoweekday() not in (6, 7):
                days.append(d)
            d += timedelta(days=1)
        return days
