"""
load_fo_bhavcopy_5y.py
======================
Download NSE F&O daily bhavcopy and load OHLCV + OI data into
``futures_candle`` and ``options_candle``.

Two-path architecture
---------------------
The NSE changed their bhavcopy URL format on ~01-Jan-2024:

  Pre-2024  (legacy)  — fetched via the ``pybhav`` library, which handles
                         NSE session cookies, redirects, and the old URL scheme
                         automatically.  Returns a DataFrame with columns:
                           INSTRUMENT, SYMBOL, EXPIRY_DT, STRIKE_PR,
                           OPTION_TYP, OPEN, HIGH, LOW, CLOSE, SETTLE_PR,
                           CONTRACTS, VAL_INLAKH, OPEN_INT, CHG_IN_OI

  2024-present (new)  — fetched directly from:
                         nsearchives.nseindia.com/content/fo/
                         BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip
                         Returns a DataFrame with columns (UDiFF format):
                           TradDt, FinInstrmTp, TckrSymb, XpryDt,
                           StrkPric, OptnTp, OpnPric, HghPric, LwPric,
                           ClsPric, SttlmPric, TtlTradgVol, TtlTrfVal,
                           OpnIntrst, ChngInOpnIntrst

Usage
-----
    # Full 5-year load (default: 2021-09-18 → today):
    APP_ENV=local python3 scripts/load_fo_bhavcopy_5y.py

    # Pre-2024 gap only:
    APP_ENV=local python3 scripts/load_fo_bhavcopy_5y.py \
        --from-date 2021-09-18 --to-date 2023-12-31

    # 2024-present only:
    APP_ENV=local python3 scripts/load_fo_bhavcopy_5y.py \
        --from-date 2024-01-01

    # Futures only (faster):
    APP_ENV=local python3 scripts/load_fo_bhavcopy_5y.py --futures-only

    # Options only:
    APP_ENV=local python3 scripts/load_fo_bhavcopy_5y.py --options-only

    # Single underlying:
    APP_ENV=local python3 scripts/load_fo_bhavcopy_5y.py --symbol NIFTY

    # Dry-run:
    APP_ENV=local python3 scripts/load_fo_bhavcopy_5y.py --dry-run

    # Via Makefile:
    make load-bhavcopy-5y
    make load-bhavcopy-futures
    make load-bhavcopy-options

Idempotency
-----------
All inserts use ON CONFLICT (instrument_id, exchange, interval_str, time) DO UPDATE.
Safe to re-run for any date range.

Rate limiting
-------------
Pre-2024 (pybhav): built-in NSE session management, ~0.5s between requests.
2024+ (direct):    configurable --delay (default 0.4s).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import structlog

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_LOOKBACK_YEARS: int = 5

# Date boundary — NSE switched format on 2024-01-01
_NEW_FORMAT_START = date(2024, 1, 1)

# New format (2024+): direct URL
_NEW_URL = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{date}_F_0000.csv.zip"
)
_NEW_DATE_FMT = "%Y%m%d"

_INTERVAL    = "1d"
_PROVIDER    = "nse_bhavcopy"
_SOURCE_TYPE = "OPEN_SOURCE_NSE_DERIVED"
_NORM_VER    = "2.0.0"
_BATCH_SIZE  = 500

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

# New-format column map
_N = {
    "type":    "FinInstrmTp",
    "symbol":  "TckrSymb",
    "expiry":  "XpryDt",
    "strike":  "StrkPric",
    "opttype": "OptnTp",
    "open":    "OpnPric",
    "high":    "HghPric",
    "low":     "LwPric",
    "close":   "ClsPric",
    "settle":  "SttlmPric",
    "volume":  "TtlTradgVol",
    "oi":      "OpnIntrst",
    "oi_chg":  "ChngInOpnIntrst",
    "turnover":"TtlTrfVal",
}

# New-format instrument type codes
_NEW_FUT = frozenset({"STF", "IDF"})
_NEW_OPT = frozenset({"STO", "IDO"})
_NEW_ALL = _NEW_FUT | _NEW_OPT

# Legacy-format instrument codes
_LEG_FUT = frozenset({"FUTSTK", "FUTIDX"})
_LEG_OPT = frozenset({"OPTSTK", "OPTIDX"})
_LEG_ALL = _LEG_FUT | _LEG_OPT

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _trading_dates(from_d: date, to_d: date) -> list[date]:
    """Weekday dates in [from_d, to_d]. NSE holidays return 404/empty — handled gracefully."""
    out, cur = [], from_d
    while cur <= to_d:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _parse_expiry(s: str) -> Optional[date]:
    """Parse expiry from either 'YYYY-MM-DD' (new) or 'DD-Mon-YYYY' (legacy)."""
    s = s.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    try:
        return datetime.strptime(s.upper(), "%d-%b-%Y").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_new_format(
    client: httpx.AsyncClient,
    trading_date: date,
    retries: int = 3,
) -> Optional[list[dict]]:
    """Fetch 2024+ bhavcopy via direct URL. Returns None on 404/holiday."""
    url = _NEW_URL.format(date=trading_date.strftime(_NEW_DATE_FMT))
    for attempt in range(1, retries + 1):
        try:
            resp = await client.get(url, headers=_HTTP_HEADERS, follow_redirects=True)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                raw = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
            return list(csv.DictReader(io.StringIO(raw)))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500 and attempt < retries:
                await asyncio.sleep(2 ** attempt)
                continue
            logger.warning("bhavcopy_new_download_failed",
                           date=str(trading_date), status=exc.response.status_code)
            return None
        except Exception as exc:  # noqa: BLE001
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
                continue
            logger.warning("bhavcopy_new_download_error",
                           date=str(trading_date), error=str(exc))
            return None
    return None


def _fetch_legacy_format(
    nse_client,  # pybhav NSEBhavcopy instance
    trading_date: date,
) -> Optional[list[dict]]:
    """Fetch pre-2024 bhavcopy via pybhav. Returns None on holiday/error."""
    try:
        df = nse_client.get(trading_date.isoformat(), segment="FO")
        if df is None or len(df) == 0:
            return None
        return df.to_dict("records")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "holiday" in msg or "weekend" in msg or "future" in msg or "available" in msg:
            return None  # expected skip — not an error
        logger.warning("bhavcopy_legacy_error",
                        date=str(trading_date), error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Row parsers (new and legacy schemas → canonical candle dict)
# ---------------------------------------------------------------------------

def _parse_new_rows(
    rows: list[dict],
    trading_date: date,
    load_futures: bool,
    load_options: bool,
    symbol_filter: Optional[str],
) -> tuple[list[dict], list[dict]]:
    """Parse 2024+ UDiFF BhavCopy rows."""
    futures, options = [], []
    for row in rows:
        itype = row.get(_N["type"], "").strip().upper()
        if itype not in _NEW_ALL:
            continue
        is_fut = itype in _NEW_FUT
        if is_fut and not load_futures:
            continue
        if not is_fut and not load_options:
            continue

        symbol = row.get(_N["symbol"], "").strip().upper()
        if not symbol or (symbol_filter and symbol != symbol_filter.upper()):
            continue

        expiry = _parse_expiry(row.get(_N["expiry"], ""))
        if expiry is None:
            continue

        try:
            o = float(row.get(_N["open"],    0) or 0)
            h = float(row.get(_N["high"],    0) or 0)
            lo = float(row.get(_N["low"],    0) or 0)
            c = float(row.get(_N["close"],   0) or 0)
            se = float(row.get(_N["settle"], 0) or 0)
            vol = int(float(row.get(_N["volume"], 0) or 0))
            oi  = int(float(row.get(_N["oi"],     0) or 0))
            oi_chg = int(float(row.get(_N["oi_chg"], 0) or 0))
            turnover = float(row.get(_N["turnover"], 0) or 0)
        except (ValueError, TypeError):
            continue

        if c == 0 and se > 0:
            c = se
        if o == 0: o = c
        if h == 0: h = c
        if lo == 0: lo = c
        if c == 0:
            continue
        h  = max(o, h, lo, c)
        lo = min(o, h, lo, c)

        base = _make_base(symbol, trading_date, o, h, lo, c, vol, oi, oi_chg, turnover, expiry)
        if is_fut:
            futures.append(base)
        else:
            try:
                strike = float(row.get(_N["strike"], 0) or 0)
            except (ValueError, TypeError):
                continue
            if strike <= 0:
                continue
            opt_type = row.get(_N["opttype"], "").strip().upper()
            if opt_type not in ("CE", "PE"):
                continue
            base["strike"] = strike
            base["option_type"] = opt_type
            options.append(base)
    return futures, options


def _parse_legacy_rows(
    rows: list[dict],
    trading_date: date,
    load_futures: bool,
    load_options: bool,
    symbol_filter: Optional[str],
) -> tuple[list[dict], list[dict]]:
    """Parse pre-2024 legacy bhavcopy rows."""
    futures, options = [], []
    for row in rows:
        itype = row.get("INSTRUMENT", "").strip().upper()
        if itype not in _LEG_ALL:
            continue
        is_fut = itype in _LEG_FUT
        if is_fut and not load_futures:
            continue
        if not is_fut and not load_options:
            continue

        symbol = row.get("SYMBOL", "").strip().upper()
        if not symbol or (symbol_filter and symbol != symbol_filter.upper()):
            continue

        expiry = _parse_expiry(row.get("EXPIRY_DT", ""))
        if expiry is None:
            continue

        try:
            o = float(row.get("OPEN",    0) or 0)
            h = float(row.get("HIGH",    0) or 0)
            lo = float(row.get("LOW",    0) or 0)
            c = float(row.get("CLOSE",   0) or 0)
            se = float(row.get("SETTLE_PR", 0) or 0)
            vol = int(float(row.get("CONTRACTS", 0) or 0))
            oi  = int(float(row.get("OPEN_INT",  0) or 0))
            oi_chg = int(float(row.get("CHG_IN_OI", 0) or 0))
            # VAL_INLAKH = value in lakhs → convert to INR
            turnover = float(row.get("VAL_INLAKH", 0) or 0) * 100_000
        except (ValueError, TypeError):
            continue

        if c == 0 and se > 0:
            c = se
        if o == 0: o = c
        if h == 0: h = c
        if lo == 0: lo = c
        if c == 0:
            continue
        h  = max(o, h, lo, c)
        lo = min(o, h, lo, c)

        base = _make_base(symbol, trading_date, o, h, lo, c, vol, oi, oi_chg, turnover, expiry)
        if is_fut:
            futures.append(base)
        else:
            try:
                strike = float(row.get("STRIKE_PR", 0) or 0)
            except (ValueError, TypeError):
                continue
            if strike <= 0:
                continue
            opt_type = row.get("OPTION_TYP", "").strip().upper()
            if opt_type not in ("CE", "PE"):
                continue
            base["strike"] = strike
            base["option_type"] = opt_type
            options.append(base)
    return futures, options


def _make_base(
    symbol: str,
    trading_date: date,
    o: float, h: float, lo: float, c: float,
    vol: int, oi: int, oi_chg: int, turnover: float,
    expiry: date,
) -> dict:
    """Build the canonical candle dict shared by both parsers."""
    # 15:30 IST = 10:00 UTC
    candle_utc = datetime(
        trading_date.year, trading_date.month, trading_date.day,
        10, 0, 0, tzinfo=timezone.utc,
    )
    return {
        "instrument_id": f"NFO:{symbol}",
        "exchange":       "NFO",
        "interval_str":   _INTERVAL,
        "time":           candle_utc,
        "session_date":   trading_date,
        "open":           o,
        "high":           h,
        "low":            lo,
        "close":          c,
        "volume":         vol,
        "oi":             oi,
        "oi_change":      oi_chg,
        "turnover":       turnover,
        "expiry":         expiry,
        "underlying_id":  f"NSE:{symbol}",
        "provider":       _PROVIDER,
        "source_type":    _SOURCE_TYPE,
        "normalisation_version": _NORM_VER,
        "dataset_version": 1,
        "poor_quality":   False,
    }


# ---------------------------------------------------------------------------
# DB upsert SQL
# ---------------------------------------------------------------------------

_FUT_SQL = """
INSERT INTO futures_candle (
    instrument_id, exchange, interval_str, time,
    open, high, low, close, volume, open_interest, oi_change,
    provider, source_type, dataset_version, session_date,
    normalisation_version, poor_quality, data_origin, quality_status,
    expiry, underlying_id, turnover
) VALUES (
    :instrument_id, :exchange, :interval_str, :time,
    :open, :high, :low, :close, :volume, :oi, :oi_change,
    :provider, :source_type, :dataset_version, :session_date,
    :normalisation_version, :poor_quality, 'PROVIDER', 'TRUSTED',
    :expiry, :underlying_id, :turnover
)
ON CONFLICT (instrument_id, exchange, interval_str, time)
DO UPDATE SET
    open                  = EXCLUDED.open,
    high                  = EXCLUDED.high,
    low                   = EXCLUDED.low,
    close                 = EXCLUDED.close,
    volume                = EXCLUDED.volume,
    open_interest         = EXCLUDED.open_interest,
    oi_change             = EXCLUDED.oi_change,
    turnover              = EXCLUDED.turnover,
    provider              = EXCLUDED.provider,
    dataset_version       = EXCLUDED.dataset_version,
    normalisation_version = EXCLUDED.normalisation_version
"""

_OPT_SQL = """
INSERT INTO options_candle (
    instrument_id, exchange, interval_str, time,
    open, high, low, close, volume, open_interest, oi_change,
    provider, source_type, dataset_version, session_date,
    normalisation_version, poor_quality, data_origin, quality_status,
    expiry, strike, option_type, underlying_id, turnover
) VALUES (
    :instrument_id, :exchange, :interval_str, :time,
    :open, :high, :low, :close, :volume, :oi, :oi_change,
    :provider, :source_type, :dataset_version, :session_date,
    :normalisation_version, :poor_quality, 'PROVIDER', 'TRUSTED',
    :expiry, :strike, :option_type, :underlying_id, :turnover
)
ON CONFLICT (instrument_id, exchange, interval_str, time)
DO UPDATE SET
    open                  = EXCLUDED.open,
    high                  = EXCLUDED.high,
    low                   = EXCLUDED.low,
    close                 = EXCLUDED.close,
    volume                = EXCLUDED.volume,
    open_interest         = EXCLUDED.open_interest,
    oi_change             = EXCLUDED.oi_change,
    turnover              = EXCLUDED.turnover,
    provider              = EXCLUDED.provider,
    dataset_version       = EXCLUDED.dataset_version,
    normalisation_version = EXCLUDED.normalisation_version
"""


async def _upsert(conn, sql: str, batch: list[dict]) -> int:
    from sqlalchemy import text  # noqa: PLC0415
    for row in batch:
        await conn.execute(text(sql), row)
    return len(batch)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run(
    from_date: date,
    to_date: date,
    dry_run: bool,
    load_futures: bool,
    load_options: bool,
    symbol_filter: Optional[str],
    inter_file_delay: float,
) -> dict:
    from src.core.settings import get_settings  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    # Split date range at the format boundary
    legacy_end = min(to_date, date(2023, 12, 31))
    new_start  = max(from_date, _NEW_FORMAT_START)

    legacy_dates = _trading_dates(from_date, legacy_end) if from_date <= legacy_end else []
    new_dates    = _trading_dates(new_start, to_date)    if new_start <= to_date    else []
    total        = len(legacy_dates) + len(new_dates)

    print(f"Bhavcopy loader — {from_date} → {to_date}")
    print(f"  Pre-2024 (pybhav)     : {len(legacy_dates)} candidate days")
    print(f"  2024+ (direct URL)    : {len(new_dates)} candidate days")
    print(f"  Total candidate days  : {total}")
    print(f"  Load futures          : {load_futures}")
    print(f"  Load options          : {load_options}")
    print(f"  Symbol filter         : {symbol_filter or 'ALL'}")
    print(f"  Dry-run               : {dry_run}")
    print()

    if dry_run:
        print(f"DRY RUN: would process up to {total} files → no DB writes.")
        await engine.dispose()
        return {"dry_run": True, "total": total}

    # Initialise pybhav client for legacy dates
    nse_legacy = None
    if legacy_dates:
        try:
            from pybhav import NSEBhavcopy  # noqa: PLC0415
            nse_legacy = NSEBhavcopy()
            print("  pybhav NSEBhavcopy ready for pre-2024 dates")
        except ImportError:
            print("  [WARN] pybhav not installed — skipping pre-2024 dates")
            print("         Run: pip install pybhav")
            legacy_dates = []

    stats = {
        "files_attempted": 0, "files_downloaded": 0,
        "files_holiday": 0, "fut_rows": 0, "opt_rows": 0, "errors": 0,
    }
    t0 = time.monotonic()

    # ── Pre-2024 legacy path (pybhav, synchronous client) ────────────────
    if legacy_dates and nse_legacy:
        print(f"Loading pre-2024 ({len(legacy_dates)} days) via pybhav…")
        for i, d in enumerate(legacy_dates, 1):
            stats["files_attempted"] += 1
            rows = _fetch_legacy_format(nse_legacy, d)
            if rows is None:
                stats["files_holiday"] += 1
                await asyncio.sleep(inter_file_delay * 0.1)
                continue

            stats["files_downloaded"] += 1
            fut, opt = _parse_legacy_rows(rows, d, load_futures, load_options, symbol_filter)
            try:
                async with engine.begin() as conn:
                    for j in range(0, len(fut), _BATCH_SIZE):
                        stats["fut_rows"] += await _upsert(conn, _FUT_SQL, fut[j:j+_BATCH_SIZE])
                    for j in range(0, len(opt), _BATCH_SIZE):
                        stats["opt_rows"] += await _upsert(conn, _OPT_SQL, opt[j:j+_BATCH_SIZE])
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                logger.error("bhavcopy_upsert_error", date=str(d), error=str(exc))

            if stats["files_downloaded"] % 50 == 0 or i == len(legacy_dates):
                elapsed = time.monotonic() - t0
                print(
                    f"  legacy [{stats['files_downloaded']:>4}/{stats['files_attempted']:>4}]"
                    f"  fut={stats['fut_rows']:,}  opt={stats['opt_rows']:,}"
                    f"  err={stats['errors']}  date={d}  ({elapsed:.0f}s)"
                )

            await asyncio.sleep(inter_file_delay)

    # ── 2024+ new format (direct URL, async httpx) ────────────────────────
    if new_dates:
        print(f"\nLoading 2024+ ({len(new_dates)} days) via direct URL…")
        transport = httpx.AsyncHTTPTransport(retries=2)
        async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
            for i, d in enumerate(new_dates, 1):
                stats["files_attempted"] += 1
                rows = await _fetch_new_format(client, d)
                if rows is None:
                    stats["files_holiday"] += 1
                    await asyncio.sleep(inter_file_delay * 0.1)
                    continue

                stats["files_downloaded"] += 1
                fut, opt = _parse_new_rows(rows, d, load_futures, load_options, symbol_filter)
                try:
                    async with engine.begin() as conn:
                        for j in range(0, len(fut), _BATCH_SIZE):
                            stats["fut_rows"] += await _upsert(conn, _FUT_SQL, fut[j:j+_BATCH_SIZE])
                        for j in range(0, len(opt), _BATCH_SIZE):
                            stats["opt_rows"] += await _upsert(conn, _OPT_SQL, opt[j:j+_BATCH_SIZE])
                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    logger.error("bhavcopy_upsert_error", date=str(d), error=str(exc))

                if stats["files_downloaded"] % 50 == 0 or i == len(new_dates):
                    elapsed = time.monotonic() - t0
                    print(
                        f"  new   [{stats['files_downloaded']:>4}/{stats['files_attempted']:>4}]"
                        f"  fut={stats['fut_rows']:,}  opt={stats['opt_rows']:,}"
                        f"  err={stats['errors']}  date={d}  ({elapsed:.0f}s)"
                    )

                await asyncio.sleep(inter_file_delay)

    await engine.dispose()
    elapsed = time.monotonic() - t0

    print()
    print("=" * 64)
    print("  BHAVCOPY LOAD COMPLETE")
    print("=" * 64)
    print(f"  Files attempted   : {stats['files_attempted']}")
    print(f"  Files downloaded  : {stats['files_downloaded']}")
    print(f"  Holidays/skipped  : {stats['files_holiday']}")
    print(f"  Futures rows      : {stats['fut_rows']:,}")
    print(f"  Options rows      : {stats['opt_rows']:,}")
    print(f"  Errors            : {stats['errors']}")
    print(f"  Elapsed           : {elapsed:.0f}s")
    print("=" * 64)

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    today = date.today()
    default_from = today.replace(year=today.year - _LOOKBACK_YEARS)

    p = argparse.ArgumentParser(
        description="Load NSE F&O bhavcopy (5y) → futures_candle + options_candle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--from-date", default=str(default_from), metavar="YYYY-MM-DD",
                   help=f"Start date (default: {default_from})")
    p.add_argument("--to-date",   default=str(today),        metavar="YYYY-MM-DD",
                   help=f"End date inclusive (default: today {today})")
    p.add_argument("--dry-run",      action="store_true")
    p.add_argument("--futures-only", action="store_true", help="Skip options")
    p.add_argument("--options-only", action="store_true", help="Skip futures")
    p.add_argument("--symbol", default=None, metavar="SYMBOL",
                   help="Only load this underlying (e.g. NIFTY)")
    p.add_argument("--delay", type=float, default=0.4, metavar="SECS",
                   help="Sleep between files (default 0.4s)")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.futures_only and args.options_only:
        print("[ERROR] --futures-only and --options-only are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(
        from_date=date.fromisoformat(args.from_date),
        to_date=date.fromisoformat(args.to_date),
        dry_run=args.dry_run,
        load_futures=not args.options_only,
        load_options=not args.futures_only,
        symbol_filter=args.symbol,
        inter_file_delay=args.delay,
    ))


if __name__ == "__main__":
    main()
