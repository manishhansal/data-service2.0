"""
backfill_india_1y.py
====================
One-shot script to fetch the last 1 year of Indian market OHLCV data
(NSE indices + Nifty 50 equities) and persist it to the canonical tables
(equity_candle for EQ/IDX, futures_candle for FO) via HistoricalEngine.

Uses the project's existing HistoricalEngine, provider adapters, and DB
engine — exactly the same code path that the API uses at runtime.

Usage
-----
# Run from the repo root with local settings:
    APP_ENV=local python scripts/backfill_india_1y.py

# Backfill only indices:
    APP_ENV=local python scripts/backfill_india_1y.py --class IDX

# Backfill a single symbol:
    APP_ENV=local python scripts/backfill_india_1y.py --symbol RELIANCE --class EQ

# Add intraday intervals on top of 1d (requires Angel One / Upstox creds):
    APP_ENV=local python scripts/backfill_india_1y.py --intervals 1d 1h 15m

# Dry-run — resolve providers and print the plan without touching the DB:
    APP_ENV=local python scripts/backfill_india_1y.py --dry-run

# Inside the Docker api container:
    docker compose --env-file .env.local exec api \
        python scripts/backfill_india_1y.py

Environment variables (loaded from .env.local / APP_ENV selection)
-------------------------------------------------------------------
DATABASE_URL           — PostgreSQL async connection string  (REQUIRED)
REDIS_URL              — Redis connection string             (REQUIRED)
ANGEL_ONE_API_KEY      — Angel One credentials              (optional, enables intraday)
ANGEL_ONE_CLIENT_ID    —   "
ANGEL_ONE_TOTP_SECRET  —   "
ANGEL_ONE_MPIN         —   "
UPSTOX_API_KEY         — Upstox credentials                 (optional, enables IDX + 1d)
UPSTOX_ACCESS_TOKEN    —   "

Notes
-----
* 3m interval is permanently blocked for Indian market data (Req 4.2).
* The HistoricalEngine is resumable — re-running the script continues from
  the last Redis checkpoint.  Safe to run repeatedly.
* Concurrency is intentionally limited (--concurrency, default 3) to avoid
  hammering the broker APIs within their rate limits.
* Failed symbols are collected and printed in a summary table at the end.
  They do NOT abort the entire run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Make sure the repo root is on sys.path so "src.*" imports work whether
# the script is executed from the repo root or from scripts/.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Instrument list
# ---------------------------------------------------------------------------
from scripts.india_instruments import (  # noqa: E402
    ALL_EQ_IDX as ALL_INSTRUMENTS,   # 1y script only covers IDX + EQ (no FO)
    NSE_INDICES,
    NIFTY50_EQUITIES,
    InstrumentEntry,
)

# ---------------------------------------------------------------------------
# Supported daily intervals only — intraday requires broker credentials and
# much larger API quota.  The CLI --intervals flag can override this.
# ---------------------------------------------------------------------------
_DEFAULT_INTERVALS: list[str] = ["1d"]

# One full trading year on NSE ≈ 252 sessions.  We fetch 370 calendar days
# to guarantee a clean 1-year window even accounting for weekends + holidays.
_LOOKBACK_DAYS: int = 370


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _from_ts() -> datetime:
    """Return start-of-range: today minus _LOOKBACK_DAYS at midnight UTC."""
    return (_now_utc() - timedelta(days=_LOOKBACK_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _print_banner(intervals: list[str], instrument_count: int, dry_run: bool) -> None:
    bar = "=" * 62
    print(bar)
    print("  DATA-SERVICE 2.0 — Indian Market 1-Year Backfill")
    print(bar)
    print(f"  From   : {_from_ts().strftime('%Y-%m-%d')} UTC")
    print(f"  To     : {_now_utc().strftime('%Y-%m-%d')} UTC")
    print(f"  Intervals : {', '.join(intervals)}")
    print(f"  Instruments: {instrument_count}")
    print(f"  Dry-run: {dry_run}")
    print(bar)
    print()


def _print_result_row(
    symbol: str,
    instrument_class: str,
    interval: str,
    candles: int,
    chunks_ok: int,
    chunks_total: int,
    elapsed_s: float,
    status: str,
) -> None:
    mark = "✓" if status == "OK" else "✗"
    print(
        f"  {mark}  {symbol:<16} [{instrument_class:<3}] {interval:<4} "
        f"candles={candles:>6}  chunks={chunks_ok}/{chunks_total}  "
        f"{elapsed_s:5.1f}s  {status}"
    )


def _print_summary(
    total: int,
    ok: int,
    failed: list[tuple[str, str, str]],
    wall_s: float,
) -> None:
    print()
    print("=" * 62)
    print(f"  Summary: {ok}/{total} completed  ({wall_s:.0f}s total)")
    if failed:
        print(f"\n  Failed ({len(failed)}):")
        for sym, cls, ivl in failed:
            print(f"    • {sym} [{cls}] {ivl}")
    else:
        print("  All instruments backfilled successfully.")
    print("=" * 62)


# ---------------------------------------------------------------------------
# Core backfill runner
# ---------------------------------------------------------------------------


async def _backfill_one(
    *,
    instrument: InstrumentEntry,
    interval: str,
    from_ts: datetime,
    to_ts: datetime,
    hist_engine: "HistoricalEngine",  # type: ignore[name-defined]  # noqa: F821
    db_engine: "AsyncEngine",        # type: ignore[name-defined]  # noqa: F821
    redis_client: "AsyncRedis",      # type: ignore[name-defined]  # noqa: F821
    dry_run: bool,
) -> dict:
    """Run backfill for a single (symbol, interval) pair.

    Returns a result dict with keys:
        ok, candles_persisted, chunks_attempted, chunks_succeeded,
        elapsed_s, error
    """
    symbol = instrument["symbol"]
    exchange = instrument["exchange"]
    instrument_class = instrument["instrument_class"]
    t0 = time.monotonic()

    if dry_run:
        # Resolve provider without touching the network
        provider = hist_engine._resolve_provider(
            instrument_class=instrument_class,
            interval=interval,
            is_indian_market=True,
        )
        print(
            f"  [DRY-RUN] {symbol:<16} [{instrument_class}] {interval:<4} "
            f"→ provider={provider.value}"
        )
        return {
            "ok": True,
            "candles_persisted": 0,
            "chunks_attempted": 0,
            "chunks_succeeded": 0,
            "elapsed_s": time.monotonic() - t0,
            "error": None,
        }

    try:
        result = await hist_engine.run_backfill(
            symbol=symbol,
            exchange=exchange,
            instrument_class=instrument_class,
            interval=interval,
            from_ts=from_ts,
            to_ts=to_ts,
            db_engine=db_engine,
            redis_client=redis_client,
            is_indian_market=True,
        )
        return {
            "ok": True,
            "candles_persisted": result.get("candles_persisted", 0),
            "chunks_attempted": result.get("chunks_attempted", 0),
            "chunks_succeeded": result.get("chunks_succeeded", 0),
            "elapsed_s": time.monotonic() - t0,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "candles_persisted": 0,
            "chunks_attempted": 0,
            "chunks_succeeded": 0,
            "elapsed_s": time.monotonic() - t0,
            "error": str(exc),
        }


async def _backfill_all(
    instruments: list[InstrumentEntry],
    intervals: list[str],
    dry_run: bool,
    concurrency: int,
) -> None:
    """Bootstrap infrastructure, then backfill all (instrument × interval) pairs."""

    # ── Settings ─────────────────────────────────────────────────────────
    from src.core.settings import get_settings  # noqa: PLC0415

    settings = get_settings()

    # ── Database engine ───────────────────────────────────────────────────
    from src.db.engine import create_async_engine_from_settings  # noqa: PLC0415

    print("Connecting to PostgreSQL …")
    db_engine = await create_async_engine_from_settings(settings)
    print(f"  ✓ PostgreSQL: {settings.database_url.split('@')[-1]}")

    # ── Redis client ──────────────────────────────────────────────────────
    from src.cache.redis_client import create_redis_pool  # noqa: PLC0415

    print("Connecting to Redis …")
    redis_client = await create_redis_pool(settings.redis_url)
    await redis_client.ping()
    print(f"  ✓ Redis: {settings.redis_url}")

    # ── Historical engine ─────────────────────────────────────────────────
    from src.engines.historical_engine import HistoricalEngine  # noqa: PLC0415

    hist_engine = HistoricalEngine()

    # ── Pre-authenticate provider adapters and inject into the engine ────
    # Without this, _fetch_candles creates a new adapter + TOTP login per
    # chunk, which hammers Angel One's rate limiter and causes HTTP 403.
    if (settings.angel_one_api_key and settings.angel_one_client_id
            and settings.angel_one_totp_secret):
        try:
            from src.providers.adapters.angel_one import AngelOneAdapter  # noqa: PLC0415
            angel_adapter = AngelOneAdapter(
                api_key=settings.angel_one_api_key,
                client_id=settings.angel_one_client_id,
                totp_secret=settings.angel_one_totp_secret,
                mpin=settings.angel_one_mpin,
            )
            await angel_adapter.ensure_authenticated()
            hist_engine._angel_one_adapter = angel_adapter
            print("  ✓ Angel One adapter authenticated (shared session)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ Angel One auth failed: {exc} — intraday data will be skipped")

    if settings.upstox_access_token and settings.upstox_api_key:
        try:
            from src.providers.adapters.upstox import UpstoxAdapter  # noqa: PLC0415
            upstox_adapter = UpstoxAdapter(
                api_key=settings.upstox_api_key,
                api_secret=settings.upstox_api_secret or "",
                redirect_uri=settings.upstox_redirect_uri or "http://localhost:8200/v1/auth/upstox/callback",
            )
            await upstox_adapter.set_access_token(settings.upstox_access_token)
            hist_engine._upstox_adapter = upstox_adapter
            print("  ✓ Upstox adapter ready (shared session)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ Upstox init failed: {exc} — daily/weekly data may fall back to Yahoo")

    print("  ✓ HistoricalEngine ready")
    print()

    # ── Date range ────────────────────────────────────────────────────────
    from_ts = _from_ts()
    to_ts   = _now_utc()

    _print_banner(intervals, len(instruments), dry_run)

    # ── Build work list: (instrument, interval) pairs ─────────────────────
    work: list[tuple[InstrumentEntry, str]] = [
        (instr, ivl)
        for instr in instruments
        for ivl in intervals
    ]

    total   = len(work)
    ok_count = 0
    failed: list[tuple[str, str, str]] = []   # (symbol, class, interval)

    # ── Bounded concurrency via semaphore ─────────────────────────────────
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(instrument: InstrumentEntry, interval: str) -> None:
        nonlocal ok_count
        async with sem:
            result = await _backfill_one(
                instrument=instrument,
                interval=interval,
                from_ts=from_ts,
                to_ts=to_ts,
                hist_engine=hist_engine,
                db_engine=db_engine,
                redis_client=redis_client,
                dry_run=dry_run,
            )
            sym   = instrument["symbol"]
            cls   = instrument["instrument_class"]
            status = "OK" if result["ok"] else f"ERROR: {result['error']}"
            if not dry_run:
                _print_result_row(
                    symbol=sym,
                    instrument_class=cls,
                    interval=interval,
                    candles=result["candles_persisted"],
                    chunks_ok=result["chunks_succeeded"],
                    chunks_total=result["chunks_attempted"],
                    elapsed_s=result["elapsed_s"],
                    status="OK" if result["ok"] else "FAIL",
                )
            if result["ok"]:
                ok_count += 1
            else:
                failed.append((sym, cls, interval))

    wall_start = time.monotonic()
    await asyncio.gather(*[_bounded(instr, ivl) for instr, ivl in work])
    wall_s = time.monotonic() - wall_start

    # ── Tear down ─────────────────────────────────────────────────────────
    if hist_engine._angel_one_adapter is not None:
        await hist_engine._angel_one_adapter.close()
    if hist_engine._upstox_adapter is not None:
        await hist_engine._upstox_adapter.aclose()
    await redis_client.aclose()
    await db_engine.dispose()

    _print_summary(total, ok_count, failed, wall_s)

    if failed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill 1 year of Indian market OHLCV data into the canonical tables "
            "(equity_candle for EQ/IDX, futures_candle for FO) via HistoricalEngine."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--intervals",
        nargs="+",
        default=_DEFAULT_INTERVALS,
        metavar="INTERVAL",
        help=(
            "Candle intervals to backfill. "
            "Supported: 1m 5m 10m 15m 30m 1h 1d 1w 1M. "
            "3m is permanently blocked. "
            f"Default: {' '.join(_DEFAULT_INTERVALS)}"
        ),
    )
    parser.add_argument(
        "--class",
        dest="instrument_class",
        choices=["ALL", "EQ", "IDX"],
        default="ALL",
        help=(
            "Filter instruments by class. "
            "ALL = indices + equities (default). "
            "IDX = NSE indices only. "
            "EQ = Nifty 50 equities only."
        ),
    )
    parser.add_argument(
        "--symbol",
        default=None,
        metavar="SYMBOL",
        help=(
            "Backfill a single instrument only "
            "(e.g. --symbol RELIANCE). "
            "Must also specify --class when using this flag."
        ),
    )
    parser.add_argument(
        "--exchange",
        default="NSE",
        help="Exchange for a single-symbol backfill (default: NSE).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        metavar="N",
        help=(
            "Maximum number of concurrent backfill tasks. "
            "Keep ≤3 to stay within Angel One's 3 req/s rate limit. "
            "Default: 3."
        ),
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=_LOOKBACK_DAYS,
        metavar="DAYS",
        help=f"Calendar days of history to fetch (default: {_LOOKBACK_DAYS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve providers and print the plan without fetching or persisting data.",
    )
    return parser.parse_args()


def _resolve_instruments(
    instrument_class: str,
    symbol: Optional[str],
    exchange: str,
) -> list[InstrumentEntry]:
    """Return the filtered instrument list based on CLI args."""
    if symbol is not None:
        # Single-symbol mode
        if instrument_class == "ALL":
            print(
                "[ERROR] --symbol requires --class EQ or --class IDX. "
                "Cannot auto-detect instrument class.",
                file=sys.stderr,
            )
            sys.exit(1)
        return [
            InstrumentEntry(
                symbol=symbol.upper(),
                exchange=exchange.upper(),
                instrument_class=instrument_class,
                description=f"{symbol.upper()} (user-supplied)",
            )
        ]

    if instrument_class == "IDX":
        return NSE_INDICES
    if instrument_class == "EQ":
        return NIFTY50_EQUITIES
    return ALL_INSTRUMENTS   # "ALL"


def _validate_intervals(intervals: list[str]) -> list[str]:
    """Block 3m and unknown intervals; return the validated list."""
    from src.core.schemas.provider import CANONICAL_INDIAN_TIMEFRAMES  # noqa: PLC0415

    clean: list[str] = []
    for ivl in intervals:
        if ivl == "3m":
            print(
                f"[WARN] Skipping interval '3m' — permanently blocked for Indian market data.",
                file=sys.stderr,
            )
            continue
        if ivl not in CANONICAL_INDIAN_TIMEFRAMES:
            print(
                f"[WARN] Skipping unknown interval '{ivl}'. "
                f"Supported: {', '.join(CANONICAL_INDIAN_TIMEFRAMES)}",
                file=sys.stderr,
            )
            continue
        clean.append(ivl)

    if not clean:
        print("[ERROR] No valid intervals remaining after filtering.", file=sys.stderr)
        sys.exit(1)

    return clean


def main() -> None:
    args = _parse_args()

    # Override lookback if provided
    global _LOOKBACK_DAYS
    _LOOKBACK_DAYS = args.lookback_days

    intervals   = _validate_intervals(args.intervals)
    instruments = _resolve_instruments(
        instrument_class=args.instrument_class,
        symbol=args.symbol,
        exchange=args.exchange,
    )

    asyncio.run(
        _backfill_all(
            instruments=instruments,
            intervals=intervals,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
        )
    )


if __name__ == "__main__":
    main()
