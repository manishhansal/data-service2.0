"""
src/worker_tasks/ohlcv_catchup.py
==================================
Continuous OHLCV catch-up worker — keeps historical candle data current.

Behaviour
---------
When the service starts (or this task is triggered), it:

1. Loads the instrument universe from the ``fo_universe`` DB table, ordered by
   ``backfill_priority`` (Index futures → Stock futures → Broad indices →
   Nifty50 → Others).  Falls back to the static ``ALL_SPOT`` list if the table
   is not yet populated.

2. For each instrument × interval pair, reads the Redis checkpoint to find the
   **last successfully persisted candle timestamp**.  Fetches only the delta
   from that point to "now".  If no checkpoint exists the lookback window
   defaults to a per-interval maximum (see ``_DEFAULT_LOOKBACK`` below).

3. Uses the existing ``HistoricalEngine.run_backfill()`` — the same code path
   as the manual ``scripts/backfill_india_5y.py`` — so all provider routing,
   rate limiting, validation, and checkpoint writing are handled automatically.

4. Runs on a configurable concurrency (default 2) with a per-chunk delay so
   provider rate limits are never hit.

5. After completing the full universe it sleeps until the next scheduled run
   (default: checks every 4 hours for intraday, daily for 1d/1w/1M).

6. Listens to the shared ``stop_event`` so shutdown is clean and instant.

Intervals processed
-------------------
Two passes per cycle:

  Pass 1 — EOD intervals (1d, 1w, 1M)
    Runs once per day after market close (~16:30 IST).
    Data should be complete and settled.

  Pass 2 — Intraday intervals (1m, 5m, 10m, 15m, 30m, 1h)
    Runs continuously every 4 hours while the market may be open or has
    recently closed.  For most symbols, Upstox returns data up to the
    previous closed candle.

Configuration (via env vars, all optional)
------------------------------------------
  CATCHUP_CONCURRENCY          int   default 2   — simultaneous instruments
  CATCHUP_CHUNK_DELAY_S        float default 0.3 — sleep between provider calls
  CATCHUP_INSTRUMENT_DELAY_S   float default 0.2 — sleep between instruments
  CATCHUP_EOD_HOUR_IST         int   default 17  — hour (IST) to run EOD pass
  CATCHUP_INTRADAY_INTERVAL_H  int   default 4   — hours between intraday runs
  CATCHUP_INTERVALS_EOD        str   default "1d,1w,1M"
  CATCHUP_INTERVALS_INTRADAY   str   default "1m,5m,10m,15m,30m,1h"
  CATCHUP_ENABLED              bool  default true — set false to disable worker
"""

from __future__ import annotations

import asyncio
import datetime
import os
import time
from typing import Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine
    from redis.asyncio import Redis as AsyncRedis

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")
UTC = datetime.timezone.utc

# ---------------------------------------------------------------------------
# Configuration — read from env with sane defaults
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "").lower()
    if v in ("false", "0", "no", "off"):
        return False
    if v in ("true", "1", "yes", "on"):
        return True
    return default

_CONCURRENCY          = _env_int("CATCHUP_CONCURRENCY",         2)
_CHUNK_DELAY_S        = _env_float("CATCHUP_CHUNK_DELAY_S",    0.3)
_INSTRUMENT_DELAY_S   = _env_float("CATCHUP_INSTRUMENT_DELAY_S",0.2)
_EOD_HOUR_IST         = _env_int("CATCHUP_EOD_HOUR_IST",       17)
_INTRADAY_INTERVAL_H  = _env_int("CATCHUP_INTRADAY_INTERVAL_H", 4)
_ENABLED              = _env_bool("CATCHUP_ENABLED",            True)

_RAW_EOD       = os.environ.get("CATCHUP_INTERVALS_EOD",      "1d,1w,1M")
_RAW_INTRADAY  = os.environ.get("CATCHUP_INTERVALS_INTRADAY", "1m,5m,10m,15m,30m,1h")
_INTERVALS_EOD      = [s.strip() for s in _RAW_EOD.split(",")     if s.strip()]
_INTERVALS_INTRADAY = [s.strip() for s in _RAW_INTRADAY.split(",") if s.strip()]

# Default lookback when no checkpoint exists — how far back to seed fresh.
# For IDX the intraday intervals are blocked entirely (see _run_pass),
# so only EOD lookbacks matter for indices.
_DEFAULT_LOOKBACK: dict[str, int] = {
    "1m":  730,    # 2 years (Upstox free plan limit for EQ)
    "5m":  730,
    "10m": 730,
    "15m": 730,
    "30m": 730,
    "1h":  730,
    "1d":  1826,   # 5 years
    "1w":  1826,
    "1M":  1826,
}

# Upstox intraday is NOT available for NSE_INDEX instruments.
# This set is used to skip those combinations without even attempting a fetch.
_IDX_UNSUPPORTED_INTRADAY = frozenset({"1m", "5m", "10m", "15m", "30m", "1h"})


# ---------------------------------------------------------------------------
# Instrument loader — fo_universe DB first, static list fallback
# ---------------------------------------------------------------------------

async def _load_instruments(db_engine: "AsyncEngine") -> list[dict]:
    """Load ordered instrument list from fo_universe, fallback to static."""
    from sqlalchemy import text  # noqa: PLC0415

    try:
        async with db_engine.connect() as conn:
            exists = (await conn.execute(
                text("SELECT to_regclass('public.fo_universe')")
            )).scalar()
            if exists is None:
                raise RuntimeError("fo_universe table not found")

            rows = (await conn.execute(text("""
                SELECT symbol, exchange, instrument_class, company_name
                FROM fo_universe
                WHERE is_spot_active = TRUE
                ORDER BY backfill_priority, symbol
            """))).mappings().all()

        instruments = [
            {
                "symbol":           r["symbol"],
                "exchange":         r["exchange"],
                "instrument_class": r["instrument_class"],
                "description":      r["company_name"] or r["symbol"],
            }
            for r in rows
        ]
        await logger.ainfo(
            "ohlcv_catchup.instruments_loaded_from_db",
            component="ohlcv_catchup",
            count=len(instruments),
            source="fo_universe",
        )
        return instruments

    except Exception as exc:  # noqa: BLE001
        await logger.awarning(
            "ohlcv_catchup.fo_universe_fallback",
            component="ohlcv_catchup",
            error=str(exc),
            note="Falling back to static ALL_SPOT list",
        )
        # Fallback: static list
        import sys  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415
        _root = Path(__file__).resolve().parent.parent.parent
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        from scripts.india_instruments import ALL_SPOT  # noqa: PLC0415
        return [dict(i) for i in ALL_SPOT]


# ---------------------------------------------------------------------------
# Single-instrument catch-up with gradual rate limiting
# ---------------------------------------------------------------------------

async def _catchup_one(
    *,
    instrument: dict,
    interval: str,
    hist_engine,        # HistoricalEngine
    db_engine: "AsyncEngine",
    redis_client: "AsyncRedis",
    chunk_delay_s: float,
) -> dict:
    """Catch up a single (symbol, interval) pair from last checkpoint to now."""
    symbol           = instrument["symbol"]
    exchange         = instrument["exchange"]
    instrument_class = instrument["instrument_class"]
    t0               = time.monotonic()

    # Determine from_ts: use a wide window; run_backfill will advance to checkpoint
    lookback_days = _DEFAULT_LOOKBACK.get(interval, 365)
    from_ts = (datetime.datetime.now(UTC) - datetime.timedelta(days=lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    to_ts = datetime.datetime.now(UTC)

    # Inject chunk delay via _fetch_candles wrapper
    _original_fetch = hist_engine._fetch_candles

    async def _delayed_fetch(**kwargs):
        result = await _original_fetch(**kwargs)
        if chunk_delay_s > 0:
            await asyncio.sleep(chunk_delay_s)
        return result

    hist_engine._fetch_candles = _delayed_fetch

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
        candles = result.get("candles_persisted", 0)
        if candles > 0:
            await logger.ainfo(
                "ohlcv_catchup.candles_persisted",
                component="ohlcv_catchup",
                symbol=symbol,
                interval=interval,
                candles=candles,
                elapsed_s=round(time.monotonic() - t0, 1),
            )
        return {"ok": True, "candles": candles, "elapsed_s": time.monotonic() - t0}
    except Exception as exc:  # noqa: BLE001
        await logger.awarning(
            "ohlcv_catchup.instrument_error",
            component="ohlcv_catchup",
            symbol=symbol,
            interval=interval,
            error=str(exc),
        )
        return {"ok": False, "candles": 0, "elapsed_s": time.monotonic() - t0}
    finally:
        hist_engine._fetch_candles = _original_fetch


# ---------------------------------------------------------------------------
# Full-universe pass
# ---------------------------------------------------------------------------

async def _run_pass(
    *,
    pass_name: str,
    intervals: list[str],
    instruments: list[dict],
    hist_engine,
    db_engine: "AsyncEngine",
    redis_client: "AsyncRedis",
    stop_event: asyncio.Event,
    concurrency: int,
    chunk_delay_s: float,
    instrument_delay_s: float,
) -> dict:
    """Run one catch-up pass (EOD or intraday) over the full instrument universe."""
    t0 = time.monotonic()
    total_candles = 0
    errors = 0

    # Build work list: intervals first (so each interval completes before next)
    # Priority: intraday work is interleaved per-instrument so no single
    # instrument monopolises the semaphore for all its intervals.
    # Skip intraday intervals for IDX instruments — Upstox NSE_INDEX keys
    # do not support intraday (1m-1h) via the historical-candle endpoint.
    _IDX_EOD_ONLY = frozenset({"1d", "1w", "1M"})

    work: list[tuple[dict, str]] = []
    for instr in instruments:
        for ivl in intervals:
            # Skip intraday for index instruments — not supported by any free provider
            if instr.get("instrument_class") == "IDX" and ivl not in _IDX_EOD_ONLY:
                continue
            work.append((instr, ivl))

    sem = asyncio.Semaphore(concurrency)
    last_symbol: list[str] = [""]

    async def _bounded(instr: dict, ivl: str) -> None:
        nonlocal total_candles, errors
        if stop_event.is_set():
            return
        async with sem:
            if stop_event.is_set():
                return
            # Inter-instrument courtesy delay
            sym = instr["symbol"]
            if instrument_delay_s > 0 and last_symbol[0] and last_symbol[0] != sym:
                await asyncio.sleep(instrument_delay_s)
            last_symbol[0] = sym

            result = await _catchup_one(
                instrument=instr,
                interval=ivl,
                hist_engine=hist_engine,
                db_engine=db_engine,
                redis_client=redis_client,
                chunk_delay_s=chunk_delay_s,
            )
            total_candles += result["candles"]
            if not result["ok"]:
                errors += 1

    await asyncio.gather(*[_bounded(instr, ivl) for instr, ivl in work])

    elapsed = time.monotonic() - t0
    await logger.ainfo(
        "ohlcv_catchup.pass_complete",
        component="ohlcv_catchup",
        pass_name=pass_name,
        instruments=len(instruments),
        intervals=intervals,
        total_candles=total_candles,
        errors=errors,
        elapsed_s=round(elapsed, 1),
    )
    return {"candles": total_candles, "errors": errors, "elapsed_s": elapsed}


# ---------------------------------------------------------------------------
# Main worker task — runs until stop_event is set
# ---------------------------------------------------------------------------

async def run_ohlcv_catchup(
    *,
    db_engine: "AsyncEngine",
    redis_client: "AsyncRedis",
    hist_engine,             # HistoricalEngine (pre-authenticated)
    stop_event: asyncio.Event,
) -> None:
    """Continuous OHLCV catch-up loop.

    Runs two kinds of passes on a schedule:

    EOD pass   — once daily at ``_EOD_HOUR_IST`` IST.
                 Fetches 1d / 1w / 1M for all instruments.

    Intraday   — every ``_INTRADAY_INTERVAL_H`` hours.
                 Fetches 1m–1h for all instruments.

    On startup, both passes run immediately so the DB is current before the
    first scheduled run.
    """
    if not _ENABLED:
        await logger.ainfo(
            "ohlcv_catchup.disabled",
            component="ohlcv_catchup",
            note="Set CATCHUP_ENABLED=true to enable",
        )
        return

    await logger.ainfo(
        "ohlcv_catchup.starting",
        component="ohlcv_catchup",
        concurrency=_CONCURRENCY,
        eod_intervals=_INTERVALS_EOD,
        intraday_intervals=_INTERVALS_INTRADAY,
        eod_hour_ist=_EOD_HOUR_IST,
        intraday_interval_h=_INTRADAY_INTERVAL_H,
    )

    # Load instrument universe once at startup (refreshed each cycle)
    instruments: list[dict] = []

    last_eod_date: Optional[datetime.date] = None          # tracks last EOD pass date
    last_intraday_ts: float = 0.0                           # monotonic timestamp

    _INTRADAY_INTERVAL_S = _INTRADAY_INTERVAL_H * 3600

    while not stop_event.is_set():
        now_ist   = datetime.datetime.now(IST)
        now_date  = now_ist.date()
        mono_now  = time.monotonic()

        # Re-load instrument universe each cycle (picks up fo_universe changes)
        try:
            instruments = await _load_instruments(db_engine)
        except Exception as exc:  # noqa: BLE001
            await logger.awarning(
                "ohlcv_catchup.instrument_load_failed",
                component="ohlcv_catchup",
                error=str(exc),
            )
            await asyncio.sleep(60)
            continue

        if not instruments:
            await logger.awarning(
                "ohlcv_catchup.no_instruments",
                component="ohlcv_catchup",
            )
            await asyncio.sleep(300)
            continue

        # ── EOD pass: once per day after _EOD_HOUR_IST ────────────────────
        eod_due = (
            last_eod_date is None               # never run
            or last_eod_date < now_date         # new calendar day
        ) and now_ist.hour >= _EOD_HOUR_IST

        if eod_due and not stop_event.is_set():
            await logger.ainfo(
                "ohlcv_catchup.eod_pass_start",
                component="ohlcv_catchup",
                date=str(now_date),
                intervals=_INTERVALS_EOD,
                instruments=len(instruments),
            )
            await _run_pass(
                pass_name="EOD",
                intervals=_INTERVALS_EOD,
                instruments=instruments,
                hist_engine=hist_engine,
                db_engine=db_engine,
                redis_client=redis_client,
                stop_event=stop_event,
                concurrency=_CONCURRENCY,
                chunk_delay_s=_CHUNK_DELAY_S,
                instrument_delay_s=_INSTRUMENT_DELAY_S,
            )
            last_eod_date = now_date

        # ── Intraday pass: every N hours ──────────────────────────────────
        intraday_due = (
            mono_now - last_intraday_ts >= _INTRADAY_INTERVAL_S
        )

        if intraday_due and not stop_event.is_set() and _INTERVALS_INTRADAY:
            await logger.ainfo(
                "ohlcv_catchup.intraday_pass_start",
                component="ohlcv_catchup",
                intervals=_INTERVALS_INTRADAY,
                instruments=len(instruments),
            )
            await _run_pass(
                pass_name="INTRADAY",
                intervals=_INTERVALS_INTRADAY,
                instruments=instruments,
                hist_engine=hist_engine,
                db_engine=db_engine,
                redis_client=redis_client,
                stop_event=stop_event,
                concurrency=_CONCURRENCY,
                chunk_delay_s=_CHUNK_DELAY_S,
                instrument_delay_s=_INSTRUMENT_DELAY_S,
            )
            last_intraday_ts = time.monotonic()

        # Sleep 60s between scheduling checks (not blocking a tight loop)
        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            pass  # expected — just means stop wasn't set in 60s

    await logger.ainfo(
        "ohlcv_catchup.stopped",
        component="ohlcv_catchup",
    )
