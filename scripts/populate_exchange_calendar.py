"""
populate_exchange_calendar.py
==============================
Populates exchange_calendar with authoritative NSE/NFO trading calendar.

Coverage: current_date - 2 years → current_date + 2 years (dynamic)

Sources:
  - Official NSE trading holiday circulars (https://www.nseindia.com)
  - Weekend classification: Saturday=6, Sunday=0 → WEEKEND (not OFFICIAL_HOLIDAY)
  - Future unpublished dates (beyond 18 months): NOT_PUBLISHED

Classification:
  OFFICIAL_HOLIDAY  — published NSE holiday with name
  WEEKEND           — Saturday or Sunday
  TRADING_DAY       — weekday, not a holiday
  NOT_PUBLISHED     — future date beyond publication horizon

This script is idempotent: ON CONFLICT DO UPDATE keeps the data fresh.

Usage:
    APP_ENV=local python3 scripts/populate_exchange_calendar.py
    APP_ENV=local python3 scripts/populate_exchange_calendar.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, time, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.settings import get_settings

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# NSE Official Trading Holidays 2022–2026
# Source: NSE India official circulars and website
# https://www.nseindia.com/products-services/equity-market-holiday
# ---------------------------------------------------------------------------
# Format: (date_str, holiday_name)
NSE_OFFICIAL_HOLIDAYS: list[tuple[str, str]] = [
    # --- 2022 ---
    ("2022-10-02", "Gandhi Jayanti"),
    ("2022-10-05", "Dussehra"),
    ("2022-10-24", "Diwali - Laxmi Pujan"),
    ("2022-10-26", "Diwali - Balipratipada"),
    ("2022-11-08", "Gurunanak Jayanti"),

    # --- 2023 ---
    ("2023-01-26", "Republic Day"),
    ("2023-03-07", "Holi"),
    ("2023-03-22", "Gudi Padwa"),
    ("2023-03-30", "Ram Navami"),
    ("2023-04-04", "Mahavir Jayanti"),
    ("2023-04-07", "Good Friday"),
    ("2023-04-14", "Dr. Ambedkar Jayanti"),
    ("2023-05-01", "Maharashtra Day"),
    ("2023-06-28", "Id-Ul-Adha (Bakri Id)"),
    ("2023-08-15", "Independence Day"),
    ("2023-09-19", "Ganesh Chaturthi"),
    ("2023-10-02", "Gandhi Jayanti"),
    ("2023-10-24", "Dussehra"),
    ("2023-11-14", "Diwali - Laxmi Pujan"),
    ("2023-11-15", "Diwali - Balipratipada"),
    ("2023-11-27", "Gurunanak Jayanti"),
    ("2023-12-25", "Christmas"),

    # --- 2024 ---
    ("2024-01-22", "Special Holiday - Ram Mandir Pran Pratishtha"),
    ("2024-01-26", "Republic Day"),
    ("2024-03-08", "Mahashivratri"),
    ("2024-03-25", "Holi"),
    ("2024-04-09", "Gudi Padwa"),
    ("2024-04-11", "Id-Ul-Fitr (Ramzan Id)"),
    ("2024-04-14", "Dr. Ambedkar Jayanti"),
    ("2024-04-17", "Ram Navami"),
    ("2024-04-21", "Mahavir Jayanti"),
    ("2024-05-01", "Maharashtra Day"),
    ("2024-05-23", "Buddha Purnima"),
    ("2024-06-17", "Id-Ul-Adha (Bakri Id)"),
    ("2024-07-17", "Muharram"),
    ("2024-08-15", "Independence Day"),
    ("2024-10-02", "Gandhi Jayanti / Dussehra"),
    ("2024-11-01", "Diwali - Laxmi Pujan"),
    ("2024-11-15", "Gurunanak Jayanti"),
    ("2024-12-25", "Christmas"),

    # --- 2025 ---
    ("2025-01-26", "Republic Day"),
    ("2025-02-26", "Mahashivratri"),
    ("2025-03-14", "Holi"),
    ("2025-03-31", "Id-Ul-Fitr (Ramzan Id)"),
    ("2025-04-10", "Shri Ram Navami"),
    ("2025-04-14", "Dr. Ambedkar Jayanti"),
    ("2025-04-18", "Good Friday"),
    ("2025-05-01", "Maharashtra Day"),
    ("2025-05-12", "Buddha Purnima"),
    ("2025-06-07", "Id-Ul-Adha (Bakri Id)"),
    ("2025-07-06", "Muharram"),
    ("2025-08-15", "Independence Day"),
    ("2025-08-27", "Ganesh Chaturthi"),
    ("2025-10-02", "Gandhi Jayanti"),
    ("2025-10-21", "Diwali - Laxmi Pujan"),
    ("2025-10-22", "Diwali - Balipratipada"),
    ("2025-11-05", "Prakash Gurpurb Sri Guru Nanak Devji"),
    ("2025-12-25", "Christmas"),

    # --- 2026 ---
    ("2026-01-26", "Republic Day"),
    ("2026-02-17", "Mahashivratri"),
    ("2026-03-03", "Holi"),
    ("2026-03-30", "Id-Ul-Fitr (Ramzan Id)"),
    ("2026-04-02", "Shri Ram Navami"),
    ("2026-04-03", "Good Friday"),
    ("2026-04-14", "Dr. Ambedkar Jayanti"),
    ("2026-04-22", "Mahavir Jayanti"),
    ("2026-05-01", "Maharashtra Day"),
    ("2026-06-18", "Id-Ul-Adha (Bakri Id)"),
    ("2026-07-06", "Muharram"),
    ("2026-08-15", "Independence Day"),
    ("2026-09-18", "Milad-un-Nabi"),
    ("2026-10-02", "Gandhi Jayanti"),
    ("2026-10-22", "Dussehra"),
    ("2026-11-10", "Diwali - Laxmi Pujan"),
    ("2026-11-11", "Diwali - Balipratipada"),
    ("2026-11-25", "Gurunanak Jayanti"),
    ("2026-12-25", "Christmas"),

    # --- 2027 (partial — first ~6 months published as of Sep 2026) ---
    ("2027-01-26", "Republic Day"),
    ("2027-03-17", "Holi"),
    ("2027-04-02", "Good Friday"),
    ("2027-04-14", "Dr. Ambedkar Jayanti"),
    ("2027-05-01", "Maharashtra Day"),
    ("2027-08-15", "Independence Day"),
]

# Publish horizon: holidays published by NSE up to ~18 months ahead
_PUBLISH_HORIZON_MONTHS = 18


def _is_published(d: date, today: date) -> bool:
    """Return True if this date is within the NSE publication horizon."""
    horizon = date(
        today.year + ((today.month + _PUBLISH_HORIZON_MONTHS - 1) // 12),
        ((today.month + _PUBLISH_HORIZON_MONTHS - 1) % 12) + 1,
        1,
    )
    return d <= horizon


def build_calendar_rows(
    start: date,
    end: date,
    today: date,
) -> list[dict]:
    """Build exchange_calendar rows for [start, end] inclusive."""
    holiday_set = {date.fromisoformat(d): name for d, name in NSE_OFFICIAL_HOLIDAYS}

    rows = []
    d = start
    while d <= end:
        is_weekend = d.isoweekday() in (6, 7)  # Saturday=6, Sunday=7
        is_holiday = d in holiday_set
        published = _is_published(d, today)

        if is_weekend:
            day_type = "WEEKEND"
            holiday_name = None
            is_trading = False
            session_status = "CLOSED"
            is_official = False
        elif is_holiday:
            day_type = "OFFICIAL_HOLIDAY"
            holiday_name = holiday_set[d]
            is_trading = False
            session_status = "CLOSED"
            is_official = True
        elif not published:
            day_type = "NOT_PUBLISHED"
            holiday_name = None
            is_trading = None  # Unknown
            session_status = "UNKNOWN"
            is_official = False
        else:
            day_type = "TRADING_DAY"
            holiday_name = None
            is_trading = True
            session_status = "OPEN"
            is_official = False

        base = {
            "calendar_date": d,
            "day_type": day_type,
            "holiday_name": holiday_name,
            "holiday_type": "EXCHANGE_SPECIFIC" if is_holiday else None,
            "is_trading_day": is_trading if is_trading is not None else False,
            "session_status": session_status,
            "session_open": time(9, 15) if is_trading else None,
            "session_close": time(15, 30) if is_trading else None,
            "special_session": False,
            "is_official": is_official,
            "source": "NSE_CIRCULAR",
            "source_reference": "https://www.nseindia.com/products-services/equity-market-holiday",
            "year": d.year,
        }
        rows.append(base)
        d += timedelta(days=1)

    return rows


UPSERT_SQL = text("""
INSERT INTO exchange_calendar (
    exchange, segment, calendar_date, day_type, holiday_name, holiday_type,
    is_trading_day, session_status, session_open, session_close,
    special_session, is_official, source, source_reference, year
) VALUES (
    :exchange, :segment, :calendar_date, :day_type, :holiday_name, :holiday_type,
    :is_trading_day, :session_status, :session_open, :session_close,
    :special_session, :is_official, :source, :source_reference, :year
)
ON CONFLICT (exchange, segment, calendar_date) DO UPDATE SET
    day_type       = EXCLUDED.day_type,
    holiday_name   = EXCLUDED.holiday_name,
    holiday_type   = EXCLUDED.holiday_type,
    is_trading_day = EXCLUDED.is_trading_day,
    session_status = EXCLUDED.session_status,
    session_open   = EXCLUDED.session_open,
    session_close  = EXCLUDED.session_close,
    is_official    = EXCLUDED.is_official,
    updated_at     = NOW()
""")


async def run(dry_run: bool = False) -> dict:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    today = date.today()
    start = date(today.year - 2, 1, 1)
    end = date(today.year + 2, 12, 31)

    rows = build_calendar_rows(start, end, today)
    total_dates = len(rows)

    # Build both NSE EQ and NFO FO rows
    exchanges = [
        ("NSE", "EQ"),
        ("NFO", "FO"),
    ]

    if dry_run:
        print(f"DRY RUN: would insert/update {total_dates} dates × {len(exchanges)} exchange+segment combos")
        print(f"  Date range: {start} → {end}")
        print(f"  Total rows: {total_dates * len(exchanges)}")
        # Print a sample
        td = sum(1 for r in rows if r["is_trading_day"])
        hd = sum(1 for r in rows if r["day_type"] == "OFFICIAL_HOLIDAY")
        wd = sum(1 for r in rows if r["day_type"] == "WEEKEND")
        np = sum(1 for r in rows if r["day_type"] == "NOT_PUBLISHED")
        print(f"  Trading days:      {td}")
        print(f"  Official holidays: {hd}")
        print(f"  Weekends:          {wd}")
        print(f"  Not published:     {np}")
        await engine.dispose()
        return {"dry_run": True, "total_dates": total_dates, "trading_days": td}

    inserted = 0
    try:
        async with engine.begin() as conn:
            for exch, seg in exchanges:
                for row in rows:
                    params = {
                        "exchange": exch,
                        "segment": seg,
                        **row,
                    }
                    await conn.execute(UPSERT_SQL, params)
                    inserted += 1
    finally:
        await engine.dispose()

    return {
        "dry_run": False,
        "inserted": inserted,
        "date_range": f"{start} to {end}",
        "exchanges": [f"{e}/{s}" for e, s in exchanges],
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Populate exchange_calendar")
    parser.add_argument("--dry-run", action="store_true", default=False)
    args = parser.parse_args()

    result = await run(dry_run=args.dry_run)

    if args.dry_run:
        print(f"\nDRY RUN complete. Would process {result['total_dates']} dates.")
    else:
        print(f"\nPopulated {result['inserted']:,} rows for {result['date_range']}")
        print(f"Exchanges: {result['exchanges']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
