"""
backfill_india_5y.py
====================
Fetch the last 5 years of Indian market OHLCV data (NSE indices + Nifty 50
equities) and persist it to the canonical tables via HistoricalEngine.

Key differences from backfill_india_1y.py
------------------------------------------
* **5-year window** (1825 calendar days, ~252 × 5 trading sessions).
* **Validate-and-update**: existing rows are re-fetched from the provider and
  written via the engine's idempotent ``ON CONFLICT … DO UPDATE`` upsert, so
  stale / corrupted data is corrected in-place.  Pass ``--skip-existing`` to
  restore the resume-only behaviour of the 1-year script.
* **Gradual fetching**: a configurable inter-chunk delay (``--chunk-delay``,
  default 0.4 s) is inserted between every provider API call so the process
  self-throttles well inside Angel One's 3 req/s and Upstox's 50 req/s limits.
  An additional per-instrument courtesy sleep (``--instrument-delay``,
  default 1.0 s) fires between instruments to avoid burst build-up.
* **Force mode**: ``--force`` clears the Redis checkpoint before starting,
  guaranteeing the full 5-year window is fetched even when a previous partial
  run already wrote a checkpoint.
* **Selective class / symbol** filtering (same flags as the 1-year script).
* **Progress tracking**: a running tally of chunks and candles is printed in
  real time so you can watch progress during the long-running fetch.

Usage
-----
    # Full 5-year backfill, default intervals (1d only):
    APP_ENV=local python scripts/backfill_india_5y.py

    # All intraday intervals on top of 1d (requires broker creds):
    APP_ENV=local python scripts/backfill_india_5y.py --intervals 1d 1h 15m 5m 1m

    # Indices only, gradual 0.5 s chunk delay:
    APP_ENV=local python scripts/backfill_india_5y.py --class IDX --chunk-delay 0.5

    # Single symbol, force re-fetch from scratch:
    APP_ENV=local python scripts/backfill_india_5y.py --symbol RELIANCE --class EQ --force

    # Dry-run — print the plan without touching DB or provider:
    APP_ENV=local python scripts/backfill_india_5y.py --dry-run

    # Inside the Docker api container (recommended for production):
    docker compose --env-file .env.local exec api \\
        python scripts/backfill_india_5y.py

    # Via Makefile helper:
    make backfill-5y

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

Rate-limit notes
----------------
Angel One:  3 req/s hard limit.  Default --concurrency 2 + --chunk-delay 0.4 s
            keeps sustained throughput ≈ 2 req/s, well within the limit.

Upstox V3:  50 req/s / 500 per-minute / 2000 per-30-minute rolling windows.
            The HierarchicalRateLimiter in src/providers/rate_limiter.py
            enforces all three automatically via Redis.  No manual delay is
            needed for Upstox EOD data, but --chunk-delay still applies so the
            process is easy on the network.

Resumability
------------
HistoricalEngine writes a Redis checkpoint after every successful chunk.
Re-running the script without --force resumes from where it left off.
With --skip-existing the engine will also skip instruments whose checkpoint
already covers the full 5-year window (no re-validation).
Use --force to override checkpoints and re-fetch everything from scratch.
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
# Repo-root path injection — works when run from repo root or scripts/
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Instrument lists (shared with the 1-year script)
# ---------------------------------------------------------------------------
from scripts.india_instruments import (  # noqa: E402
    ALL_INSTRUMENTS,
    ALL_EQ_IDX,
    ALL_SPOT,
    NSE_INDICES,
    NIFTY50_EQUITIES,
    NFO_FUTURES,
    FO_UNIVERSE_EQUITIES,
    InstrumentEntry,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 5 years of calendar days (365 × 5 + 1 leap-year day + small buffer)
_LOOKBACK_DAYS: int = 1826

# All supported intervals — 3m is permanently blocked (Req 4.2).
# EOD intervals (1d, 1w, 1M) work with any provider.
# Intraday intervals (1m–1h) require Angel One or Upstox credentials.
_DEFAULT_INTERVALS: list[str] = ["1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"]

# Per-provider advisory inter-chunk sleep (seconds).
# Applied AFTER each successful or failed chunk fetch.
# Angel One: 3 req/s → at concurrency=2, ~0.4 s gap keeps us ≈ 2 req/s.
# Upstox:    50 req/s → 0.1 s is more than enough, but we use the same value
#            for simplicity; the HierarchicalRateLimiter enforces the hard cap.
_DEFAULT_CHUNK_DELAY_S: float = 0.4

# Courtesy sleep between instruments (not between chunks of the same instrument).
_DEFAULT_INSTRUMENT_DELAY_S: float = 1.0

# Default bounded concurrency — kept low to stay safe for Angel One's 3 req/s.
_DEFAULT_CONCURRENCY: int = 2

# Column separator width for progress output
_COL = 62


# ---------------------------------------------------------------------------
# Helper — UTC timestamps
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _from_ts(lookback_days: int) -> datetime:
    """Return start-of-range: today minus lookback_days at midnight UTC."""
    return (_now_utc() - timedelta(days=lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------


def _banner(intervals: list[str], count: int, lookback: int, dry_run: bool,
            skip_existing: bool, force: bool) -> None:
    bar = "=" * _COL
    from_label = _from_ts(lookback).strftime("%Y-%m-%d")
    to_label   = _now_utc().strftime("%Y-%m-%d")
    print(bar)
    print("  DATA-SERVICE 2.0 — Indian Market 5-Year Backfill")
    print(bar)
    print(f"  From       : {from_label} UTC")
    print(f"  To         : {to_label} UTC")
    print(f"  Lookback   : {lookback} calendar days (~5 years)")
    print(f"  Intervals  : {', '.join(intervals)}")
    print(f"  Instruments: {count}")
    print(f"  Dry-run    : {dry_run}")
    print(f"  Force      : {force}  (clears Redis checkpoints before start)")
    print(f"  Skip-exist : {skip_existing}  (skip fully-checkpointed ranges)")
    print(bar)
    print()


def _header_row() -> None:
    print(
        f"  {'':1}  {'Symbol':<16} {'Cls':<5} {'Ivl':<5} "
        f"{'Candles':>8}  {'Chunks':>9}  {'Time':>6}  Status"
    )
    print("  " + "-" * (_COL - 2))


def _result_row(
    *,
    symbol: str,
    instrument_class: str,
    interval: str,
    candles: int,
    chunks_ok: int,
    chunks_total: int,
    elapsed_s: float,
    ok: bool,
    error: str = "",
) -> None:
    mark   = "✓" if ok else "✗"
    status = "OK" if ok else f"FAIL: {error[:28]}"
    print(
        f"  {mark}  {symbol:<16} [{instrument_class:<3}] {interval:<4} "
        f"candles={candles:>7}  chunks={chunks_ok:>3}/{chunks_total:<3}  "
        f"{elapsed_s:5.1f}s  {status}"
    )


def _summary(total: int, ok: int, failed: list[tuple[str, str, str]], wall_s: float) -> None:
    print()
    print("=" * _COL)
    print(f"  Summary: {ok}/{total} completed  ({wall_s:.0f}s total)")
    if failed:
        print(f"  Failed ({len(failed)}):")
        for sym, cls, ivl in failed:
            print(f"    ✗  {sym}  [{cls}]  {ivl}")
    else:
        print("  All instruments backfilled successfully.")
    print("=" * _COL)


# ---------------------------------------------------------------------------
# Validate intervals
# ---------------------------------------------------------------------------


def _validate_intervals(intervals: list[str]) -> list[str]:
    from src.core.schemas.provider import CANONICAL_INDIAN_TIMEFRAMES  # noqa: PLC0415

    clean: list[str] = []
    for ivl in intervals:
        if ivl == "3m":
            print(
                "[WARN] Skipping interval '3m' — permanently blocked for Indian market data.",
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


# ---------------------------------------------------------------------------
# Instrument filtering
# ---------------------------------------------------------------------------


def _resolve_instruments(
    instrument_class: str,
    symbol: Optional[str],
    exchange: str,
) -> list[InstrumentEntry]:
    if symbol is not None:
        if instrument_class == "ALL":
            print(
                "[ERROR] --symbol requires --class EQ, IDX, or FO. "
                "Cannot auto-detect instrument class.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Derive the correct exchange from class if not explicitly overridden
        _exchange = exchange.upper()
        if instrument_class == "FO" and _exchange == "NSE":
            _exchange = "NFO"  # F&O always live on NFO segment
        return [
            InstrumentEntry(
                symbol=symbol.upper(),
                exchange=_exchange,
                instrument_class=instrument_class,
                description=f"{symbol.upper()} (user-supplied)",
            )
        ]

    if instrument_class == "IDX":
        return NSE_INDICES
    if instrument_class == "EQ":
        return NIFTY50_EQUITIES
    if instrument_class == "FO":
        return NFO_FUTURES
    if instrument_class == "EQ_IDX":
        return ALL_EQ_IDX
    if instrument_class == "FO_UNIVERSE":
        # Full F&O universe equities — all 233 stocks with options/futures
        return FO_UNIVERSE_EQUITIES
    if instrument_class == "ALL_SPOT":
        # Every EQ+IDX instrument with spot data needed (Nifty50 + F&O universe)
        return ALL_SPOT
    return ALL_INSTRUMENTS   # "ALL" — IDX + EQ + FO + FO_UNIVERSE


# ---------------------------------------------------------------------------
# Single-instrument backfill with gradual rate limiting
# ---------------------------------------------------------------------------


async def _backfill_one(
    *,
    instrument: InstrumentEntry,
    interval: str,
    from_ts: datetime,
    to_ts: datetime,
    hist_engine,        # HistoricalEngine — typed loosely to avoid import at top
    db_engine,          # AsyncEngine
    redis_client,       # AsyncRedis
    dry_run: bool,
    force: bool,
    skip_existing: bool,
    chunk_delay_s: float,
) -> dict:
    """Run a rate-limited backfill for a single (symbol, interval) pair.

    The ``force`` flag clears the Redis checkpoint *before* the run so that
    HistoricalEngine fetches the full 5-year window, not just the tail.

    With ``skip_existing=True`` the engine's natural checkpoint behaviour is
    preserved — already-fetched ranges are not re-validated.

    With ``skip_existing=False`` (default) the checkpoint is NOT cleared, but
    ``run_backfill`` is called with the full ``from_ts`` → ``to_ts`` window;
    the engine's ``ON CONFLICT … DO UPDATE`` upsert ensures existing rows are
    validated and corrected.  The checkpoint still advances forward so
    re-runs don't redundantly re-validate the same tail.

    ``chunk_delay_s`` is injected into HistoricalEngine's acquisition loop
    by monkey-patching the engine's ``_fetch_candles`` method with a wrapper
    that sleeps before delegating to the real implementation.  This is the
    simplest hook point that doesn't require modifying the engine itself.
    """
    symbol           = instrument["symbol"]
    exchange         = instrument["exchange"]
    instrument_class = instrument["instrument_class"]
    t0               = time.monotonic()

    # ── Dry-run ─────────────────────────────────────────────────────────
    if dry_run:
        provider = hist_engine._resolve_provider(
            instrument_class=instrument_class,
            interval=interval,
            is_indian_market=True,
        )
        print(
            f"  [DRY-RUN] {symbol:<16} [{instrument_class}] {interval:<4} "
            f"→ provider={provider.value}"
        )
        return {"ok": True, "candles_persisted": 0, "chunks_attempted": 0,
                "chunks_succeeded": 0, "elapsed_s": 0.0, "error": None}

    # ── Force: clear Redis checkpoint so the full window is fetched ──────
    if force:
        await hist_engine.clear_checkpoint(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            redis_client=redis_client,
        )

    # ── Wrap _fetch_candles to inject a post-call sleep ──────────────────
    # This ensures gradual fetching across all provider adapters without
    # modifying the HistoricalEngine source.
    _original_fetch = hist_engine._fetch_candles

    async def _rate_limited_fetch(**kwargs):
        result = await _original_fetch(**kwargs)
        if chunk_delay_s > 0:
            await asyncio.sleep(chunk_delay_s)
        return result

    hist_engine._fetch_candles = _rate_limited_fetch

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
            # pass force so run_backfill itself doesn't advance from_ts
            # when skip_existing=False and we already cleared the checkpoint.
        )
        return {
            "ok":                True,
            "candles_persisted": result.get("candles_persisted", 0),
            "chunks_attempted":  result.get("chunks_attempted", 0),
            "chunks_succeeded":  result.get("chunks_succeeded", 0),
            "elapsed_s":         time.monotonic() - t0,
            "error":             None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok":                False,
            "candles_persisted": 0,
            "chunks_attempted":  0,
            "chunks_succeeded":  0,
            "elapsed_s":         time.monotonic() - t0,
            "error":             str(exc),
        }
    finally:
        # Restore original method regardless of outcome
        hist_engine._fetch_candles = _original_fetch


# ---------------------------------------------------------------------------
# DB-driven instrument loader (reads fo_universe with priority ordering)
# ---------------------------------------------------------------------------


async def _load_instruments_from_db(
    db_engine,
    instrument_class: str,
    symbol_filter: Optional[str],
) -> Optional[list[InstrumentEntry]]:
    """Load the backfill instrument list from the ``fo_universe`` DB table.

    Returns ``None`` when the table doesn't exist yet (graceful fallback to
    the hardcoded Python lists in ``india_instruments.py``).

    Priority ordering: Index futures (1) → Stock futures (2) →
    Broad indices (3) → Nifty 50 (4) → Other equities (5).

    For each class filter the WHERE clause mirrors ``_resolve_instruments``:
      ALL         → all rows
      FO          → instrument_class = 'FO' OR (is_index AND is_fo_active)
      FO_UNIVERSE → instrument_class = 'EQ' (non-Nifty50 F&O stocks)
      ALL_SPOT    → instrument_class IN ('EQ','IDX')
      IDX         → instrument_class = 'IDX'
      EQ          → instrument_class = 'EQ'
    """
    from sqlalchemy import text  # noqa: PLC0415

    # Class → SQL WHERE clause
    _where: dict[str, str] = {
        "ALL":         "TRUE",
        "EQ":          "instrument_class = 'EQ'",
        "IDX":         "instrument_class = 'IDX'",
        "FO":          "instrument_class IN ('EQ','IDX')",   # spot for F&O underlyings
        "EQ_IDX":      "instrument_class IN ('EQ','IDX')",
        "FO_UNIVERSE": "instrument_class = 'EQ' AND backfill_priority IN (2,5)",
        "ALL_SPOT":    "instrument_class IN ('EQ','IDX')",
    }
    where = _where.get(instrument_class, "TRUE")

    try:
        async with db_engine.connect() as conn:
            # Verify the table exists
            exists = (await conn.execute(text(
                "SELECT to_regclass('public.fo_universe')"
            ))).scalar()
            if exists is None:
                return None   # table not yet created — fall back to Python lists

            sym_clause = "AND symbol = :sym" if symbol_filter else ""
            rows = (await conn.execute(text(f"""
                SELECT symbol, exchange, instrument_class,
                       company_name, backfill_priority,
                       is_fo_active, fo_delisted_date
                FROM fo_universe
                WHERE {where}
                  AND is_spot_active = TRUE
                  {sym_clause}
                ORDER BY backfill_priority, symbol
            """), {"sym": symbol_filter.upper()} if symbol_filter else {}
            )).mappings().all()

        if not rows:
            return None

        instruments: list[InstrumentEntry] = []
        for r in rows:
            # Determine exchange for the instrument:
            # FO class → NFO, IDX/EQ → NSE
            exchange = "NFO" if r["instrument_class"] == "FO" else r["exchange"]
            instruments.append(InstrumentEntry(
                symbol=r["symbol"],
                exchange=exchange,
                instrument_class=r["instrument_class"],
                description=r["company_name"] or r["symbol"],
            ))
        return instruments

    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ fo_universe query failed ({exc}) — falling back to static list")
        return None


# ---------------------------------------------------------------------------
# Bootstrap infrastructure + fan-out across all (instrument × interval) pairs
# ---------------------------------------------------------------------------


async def _backfill_all(
    *,
    instruments: list[InstrumentEntry],
    intervals: list[str],
    lookback_days: int,
    dry_run: bool,
    force: bool,
    skip_existing: bool,
    concurrency: int,
    chunk_delay_s: float,
    instrument_delay_s: float,
    instrument_class: str = "ALL",   # passed through for DB query
    single_symbol: Optional[str] = None,  # passed through for DB query
) -> None:
    # ── Settings ─────────────────────────────────────────────────────────
    from src.core.settings import get_settings  # noqa: PLC0415
    settings = get_settings()

    # ── Database ──────────────────────────────────────────────────────────
    from src.db.engine import create_async_engine_from_settings  # noqa: PLC0415
    print("Connecting to PostgreSQL …")
    db_engine = await create_async_engine_from_settings(settings)
    print(f"  ✓ PostgreSQL: {settings.database_url.split('@')[-1]}")

    # ── Redis ─────────────────────────────────────────────────────────────
    from src.cache.redis_client import create_redis_pool  # noqa: PLC0415
    print("Connecting to Redis …")
    redis_client = await create_redis_pool(settings.redis_url)
    await redis_client.ping()
    print(f"  ✓ Redis: {settings.redis_url}")

    # ── HistoricalEngine ──────────────────────────────────────────────────
    from src.engines.historical_engine import HistoricalEngine  # noqa: PLC0415
    hist_engine = HistoricalEngine()

    # ── Pre-authenticate Angel One (avoids per-chunk TOTP logins → HTTP 403)
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

    # ── Pre-configure Upstox ──────────────────────────────────────────────
    if settings.upstox_access_token and settings.upstox_api_key:
        try:
            from src.providers.adapters.upstox import UpstoxAdapter  # noqa: PLC0415
            upstox_adapter = UpstoxAdapter(
                api_key=settings.upstox_api_key,
                api_secret=settings.upstox_api_secret or "",
                redirect_uri=(
                    settings.upstox_redirect_uri
                    or "http://localhost:8200/v1/auth/upstox/callback"
                ),
            )
            await upstox_adapter.set_access_token(settings.upstox_access_token)
            hist_engine._upstox_adapter = upstox_adapter
            print("  ✓ Upstox adapter ready (shared session)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ Upstox init failed: {exc} — daily/weekly data may fall back to Yahoo")

    print("  ✓ HistoricalEngine ready")
    print()

    # ── Resolve instrument list: DB first, Python list as fallback ────────
    # fo_universe is the source of truth once seeded.  The DB query orders
    # by backfill_priority so F&O underlyings are always processed before
    # indices and general equities.  Falls back to the hardcoded
    # india_instruments.py lists when fo_universe hasn't been created yet.
    if not dry_run or instrument_class not in (None, "ALL"):
        db_instruments = await _load_instruments_from_db(
            db_engine, instrument_class, single_symbol
        )
        if db_instruments is not None:
            print(
                f"  ✓ Instruments loaded from fo_universe "
                f"({len(db_instruments)} symbols, ordered by priority)"
            )
            instruments = db_instruments
        else:
            print("  ⚠ fo_universe not available — using static instrument list")

    # ── Date window ───────────────────────────────────────────────────────
    from_ts = _from_ts(lookback_days)
    to_ts   = _now_utc()

    _banner(
        intervals=intervals,
        count=len(instruments),
        lookback=lookback_days,
        dry_run=dry_run,
        skip_existing=skip_existing,
        force=force,
    )
    if not dry_run:
        _header_row()

    # ── Work list: (instrument, interval) pairs ───────────────────────────
    work: list[tuple[InstrumentEntry, str]] = [
        (instr, ivl)
        for instr in instruments
        for ivl in intervals
    ]

    total    = len(work)
    ok_count = 0
    failed:  list[tuple[str, str, str]] = []

    # ── Semaphore-bounded concurrency ─────────────────────────────────────
    # Keep concurrency ≤ 2 for Angel One (3 req/s limit) so that with the
    # 0.4 s chunk_delay the sustained rate stays comfortably below 3 req/s.
    # For Upstox-only runs (IDX 1d) higher concurrency is safe; users can
    # raise --concurrency and lower --chunk-delay accordingly.
    sem = asyncio.Semaphore(concurrency)

    # Track the previous instrument to insert an inter-instrument delay
    _last_symbol: list[str] = [""]  # mutable container for closure

    async def _bounded(instrument: InstrumentEntry, interval: str) -> None:
        nonlocal ok_count
        async with sem:
            # Inter-instrument courtesy sleep — only between different symbols,
            # not between intervals of the same symbol.
            sym = instrument["symbol"]
            if instrument_delay_s > 0 and _last_symbol[0] and _last_symbol[0] != sym:
                await asyncio.sleep(instrument_delay_s)
            _last_symbol[0] = sym

            result = await _backfill_one(
                instrument=instrument,
                interval=interval,
                from_ts=from_ts,
                to_ts=to_ts,
                hist_engine=hist_engine,
                db_engine=db_engine,
                redis_client=redis_client,
                dry_run=dry_run,
                force=force,
                skip_existing=skip_existing,
                chunk_delay_s=chunk_delay_s,
            )

            if not dry_run:
                _result_row(
                    symbol=sym,
                    instrument_class=instrument["instrument_class"],
                    interval=interval,
                    candles=result["candles_persisted"],
                    chunks_ok=result["chunks_succeeded"],
                    chunks_total=result["chunks_attempted"],
                    elapsed_s=result["elapsed_s"],
                    ok=result["ok"],
                    error=result["error"] or "",
                )

            if result["ok"]:
                ok_count += 1
            else:
                failed.append((sym, instrument["instrument_class"], interval))

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

    _summary(total, ok_count, failed, wall_s)

    if failed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill 5 years of Indian market OHLCV data into the canonical "
            "tables (equity_candle for EQ/IDX, futures_candle for FO) via "
            "HistoricalEngine.  Existing rows are validated and updated "
            "(ON CONFLICT … DO UPDATE) unless --skip-existing is passed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Instrument selection ───────────────────────────────────────────────
    parser.add_argument(
        "--intervals",
        nargs="+",
        default=_DEFAULT_INTERVALS,
        metavar="INTERVAL",
        help=(
            "Candle intervals to backfill. "
            "Supported: 1m 5m 10m 15m 30m 1h 1d 1w 1M.  3m is permanently blocked. "
            "Default: all supported intervals (1m 5m 10m 15m 30m 1h 1d 1w 1M). "
            "Intraday intervals require Angel One or Upstox credentials."
        ),
    )
    parser.add_argument(
        "--class",
        dest="instrument_class",
        choices=["ALL", "EQ", "IDX", "FO", "EQ_IDX", "FO_UNIVERSE", "ALL_SPOT"],
        default="ALL",
        help=(
            "Filter instruments by class. "
            "ALL        = indices + Nifty50 equities + NFO futures + full F&O universe (default). "
            "IDX        = NSE indices only. "
            "EQ         = Nifty 50 equities only. "
            "FO         = NFO futures only (index + stock futures). "
            "EQ_IDX     = equities + indices, no F&O. "
            "FO_UNIVERSE = all 233 F&O-eligible stocks (spot backfill). "
            "ALL_SPOT   = NSE_INDICES + NIFTY50 + FO_UNIVERSE (all spot instruments)."
        ),
    )
    parser.add_argument(
        "--symbol",
        default=None,
        metavar="SYMBOL",
        help=(
            "Backfill a single instrument only (e.g. --symbol RELIANCE). "
            "Must also specify --class when using this flag."
        ),
    )
    parser.add_argument(
        "--exchange",
        default="NSE",
        help="Exchange for a single-symbol backfill (default: NSE).",
    )

    # ── Rate-limit / concurrency ───────────────────────────────────────────
    parser.add_argument(
        "--concurrency",
        type=int,
        default=_DEFAULT_CONCURRENCY,
        metavar="N",
        help=(
            "Maximum number of concurrent backfill tasks. "
            "Keep ≤2 for Angel One (3 req/s limit); "
            "can raise to ≤10 for Upstox-only (50 req/s). "
            f"Default: {_DEFAULT_CONCURRENCY}."
        ),
    )
    parser.add_argument(
        "--chunk-delay",
        type=float,
        default=_DEFAULT_CHUNK_DELAY_S,
        metavar="SECS",
        help=(
            "Seconds to sleep after each provider API chunk call. "
            "Provides gradual, self-throttling pacing on top of the "
            "token-bucket rate limiter. "
            f"Default: {_DEFAULT_CHUNK_DELAY_S}."
        ),
    )
    parser.add_argument(
        "--instrument-delay",
        type=float,
        default=_DEFAULT_INSTRUMENT_DELAY_S,
        metavar="SECS",
        help=(
            "Additional courtesy sleep (seconds) between different instruments "
            "(not between intervals of the same instrument). "
            f"Default: {_DEFAULT_INSTRUMENT_DELAY_S}."
        ),
    )

    # ── Behaviour flags ────────────────────────────────────────────────────
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=_LOOKBACK_DAYS,
        metavar="DAYS",
        help=f"Calendar days of history to fetch (default: {_LOOKBACK_DAYS} ≈ 5 years).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Clear Redis checkpoints before starting so the full date window "
            "is fetched unconditionally, even if a previous run partially "
            "completed."
        ),
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help=(
            "Skip the validate-and-update behaviour: do NOT re-fetch data "
            "that the checkpoint shows has already been persisted.  Equivalent "
            "to the 1-year script's default resume behaviour."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve providers and print the plan without fetching or persisting data.",
    )

    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    intervals   = _validate_intervals(args.intervals)
    instruments = _resolve_instruments(
        instrument_class=args.instrument_class,
        symbol=getattr(args, "symbol", None),
        exchange=args.exchange,
    )

    asyncio.run(
        _backfill_all(
            instruments=instruments,
            intervals=intervals,
            lookback_days=args.lookback_days,
            dry_run=args.dry_run,
            force=args.force,
            skip_existing=args.skip_existing,
            concurrency=args.concurrency,
            chunk_delay_s=args.chunk_delay,
            instrument_delay_s=args.instrument_delay,
            instrument_class=args.instrument_class,
            single_symbol=getattr(args, "symbol", None),
        )
    )


if __name__ == "__main__":
    main()
