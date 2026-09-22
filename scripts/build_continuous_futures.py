"""
build_continuous_futures.py
============================
Build a Panama-adjusted continuous futures price series for each underlying
from the ``futures_candle`` (1d) data and store it in the
``continuous_futures`` table.

What is a continuous futures series?
-------------------------------------
F&O contracts expire every month.  A per-contract price series has an
artificial price gap at each expiry roll: the front-month contract closes at
price P₁ and the next contract opens at price P₂ (P₁ ≠ P₂ because of
cost-of-carry).  This gap creates a spurious "jump" that corrupts returns,
moving-averages, and any model that looks back across the roll date.

The Panama method eliminates these gaps by backward-adjusting all historical
prices at each roll so that the series is price-gap-free.  Returns are
preserved; only absolute price levels are shifted.  The ``cumulative_adj``
column stores the total backward adjustment factor so that the original price
can be recovered: original_price = adj_price * cumulative_adj.

Roll rule: roll to the next contract 5 trading days before expiry (NSE standard).

Algorithm
---------
For each underlying symbol with ≥ 2 contracts:
  1. Sort all contracts by expiry.
  2. Walk forward in time; at each roll date, compute the ratio
     P_new / P_old (ratio-adjusted Panama) or P_new - P_old (additive).
  3. Backward-multiply all historical prices by this ratio.
  4. Write the final adjusted OHLCV series to ``continuous_futures``.

This script is idempotent: it always rebuilds the full series for each
underlying by first deleting existing rows.

Usage
-----
    # Build for all underlyings:
    APP_ENV=local python3 scripts/build_continuous_futures.py

    # Build for one underlying:
    APP_ENV=local python3 scripts/build_continuous_futures.py --symbol NIFTY

    # Dry-run (print plan without DB writes):
    APP_ENV=local python3 scripts/build_continuous_futures.py --dry-run

    # Via Makefile:
    make build-continuous-futures

Prerequisites
-------------
    futures_candle must be populated (run load_fo_bhavcopy_5y.py first).

Table populated
---------------
    continuous_futures — see Alembic migration 20260919_000000.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_ROLL_DAYS_BEFORE_EXPIRY: int = 5   # NSE convention
_INTERVAL = "1d"
_PROVIDER = "derived_panama"
_SOURCE_TYPE = "DERIVED"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS continuous_futures (
    id                      BIGSERIAL,
    underlying_id           VARCHAR(64)     NOT NULL,
    exchange                VARCHAR(8)      NOT NULL  DEFAULT 'NFO',
    date                    DATE            NOT NULL,
    open                    NUMERIC(18,6)   NOT NULL,
    high                    NUMERIC(18,6)   NOT NULL,
    low                     NUMERIC(18,6)   NOT NULL,
    close                   NUMERIC(18,6)   NOT NULL,
    volume                  BIGINT          NOT NULL  DEFAULT 0,
    open_interest           BIGINT,
    oi_change               BIGINT,
    expiry_in_use           DATE            NOT NULL,
    roll_date               DATE,
    cumulative_adj          NUMERIC(20,8)   NOT NULL  DEFAULT 1.0,
    adjustment_type         VARCHAR(16)     NOT NULL  DEFAULT 'PANAMA_RATIO',
    provider                VARCHAR(32)     NOT NULL  DEFAULT 'derived_panama',
    source_type             VARCHAR(32)     NOT NULL  DEFAULT 'DERIVED',
    normalisation_version   VARCHAR(16)     NOT NULL  DEFAULT '2.0.0',
    created_at              TIMESTAMPTZ     NOT NULL  DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL  DEFAULT NOW(),
    PRIMARY KEY (id),
    UNIQUE (underlying_id, exchange, date)
);
CREATE INDEX IF NOT EXISTS cf_underlying_date
    ON continuous_futures (underlying_id, date DESC);
CREATE INDEX IF NOT EXISTS cf_exchange_date
    ON continuous_futures (exchange, date DESC);
"""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _roll_date_for(expiry: date) -> date:
    """Return the trading day 5 calendar days before expiry (roll trigger)."""
    return expiry - timedelta(days=_ROLL_DAYS_BEFORE_EXPIRY)


def _build_continuous_series(
    contracts: dict[date, list[dict]],
) -> list[dict]:
    """Build Panama-adjusted series from a dict of expiry → candle_list.

    Args:
        contracts: {expiry_date: [candle_dict, ...]} sorted by date within each list.

    Returns:
        List of adjusted candle dicts in chronological order.
    """
    # Sort expiries chronologically
    sorted_expiries = sorted(contracts.keys())

    if not sorted_expiries:
        return []

    # Build segments: each segment = one contract's candles within its active window
    # Active window for contract i: from day after previous roll_date to current roll_date
    segments: list[tuple[date, date, date, list[dict]]] = []  # (seg_start, roll_date, expiry, candles)

    prev_roll: Optional[date] = None
    for expiry in sorted_expiries:
        roll = _roll_date_for(expiry)
        candles = contracts[expiry]
        # Only include candles up to (and including) the roll_date for this contract
        if prev_roll is not None:
            candles = [c for c in candles if c["date"] > prev_roll]
        candles = [c for c in candles if c["date"] <= roll]
        if candles:
            seg_start = candles[0]["date"]
            segments.append((seg_start, roll, expiry, candles))
        prev_roll = roll

    # Add candles AFTER the last roll date from the last contract (it runs to expiry)
    if sorted_expiries:
        last_expiry = sorted_expiries[-1]
        last_roll = _roll_date_for(last_expiry)
        tail = [c for c in contracts[last_expiry] if c["date"] > last_roll]
        if tail:
            segments.append((tail[0]["date"], last_expiry, last_expiry, tail))

    if not segments:
        return []

    # Now backward-adjust: walk from newest to oldest segment
    # At each roll boundary, compute ratio = next_segment_first_close / prev_segment_last_close
    # and multiply ALL earlier candles by that ratio (Panama backward-adjust)

    cumulative_adj = 1.0
    roll_dates: set[date] = set()

    # We process segments in reverse to accumulate the adjustment backward
    adjusted_segments: list[tuple[float, list[dict], date]] = []

    for seg_idx in range(len(segments) - 1, -1, -1):
        _, roll_d, expiry, candles = segments[seg_idx]

        if seg_idx < len(segments) - 1:
            # Find the ratio between last close of THIS segment and first close of NEXT segment
            this_last_close = float(candles[-1]["close"])
            next_first_close = float(segments[seg_idx + 1][3][0]["close"])
            if this_last_close > 0 and next_first_close > 0:
                ratio = next_first_close / this_last_close
                cumulative_adj *= ratio
                roll_dates.add(roll_d)

        adjusted_segments.insert(0, (cumulative_adj, candles, expiry))

    # Build the final flat list applying the cumulative adjustment
    result: list[dict] = []
    seg_adj_reset = 1.0
    running_adj: list[tuple[float, list[dict], date]] = []

    # We need to re-derive each segment's individual factor
    # cumulative_adj starts at 1.0 for the most recent segment and grows backward
    # Recompute cleanly:
    cumulative_adj = 1.0
    final_segments: list[tuple[float, list[dict], date]] = []

    for seg_idx in range(len(segments) - 1, -1, -1):
        _, roll_d, expiry, candles = segments[seg_idx]

        if seg_idx < len(segments) - 1:
            this_last = float(candles[-1]["close"])
            next_first = float(segments[seg_idx + 1][3][0]["close"])
            if this_last > 0 and next_first > 0:
                cumulative_adj = cumulative_adj * (next_first / this_last)

        final_segments.insert(0, (cumulative_adj, candles, expiry))

    for seg_adj, candles, expiry in final_segments:
        roll_d = _roll_date_for(expiry)
        for c in candles:
            result.append({
                "date":          c["date"],
                "open":          round(float(c["open"])  * seg_adj, 6),
                "high":          round(float(c["high"])  * seg_adj, 6),
                "low":           round(float(c["low"])   * seg_adj, 6),
                "close":         round(float(c["close"]) * seg_adj, 6),
                "volume":        int(c.get("volume") or 0),
                "open_interest": int(c["open_interest"]) if c.get("open_interest") else None,
                "oi_change":     int(c["oi_change"])     if c.get("oi_change")     else None,
                "expiry_in_use": expiry,
                "roll_date":     roll_d if c["date"] == roll_d else None,
                "cumulative_adj": round(seg_adj, 8),
            })

    result.sort(key=lambda x: x["date"])
    return result


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_DELETE_SQL = "DELETE FROM continuous_futures WHERE underlying_id = :uid AND exchange = 'NFO'"

_INSERT_SQL = """
INSERT INTO continuous_futures (
    underlying_id, exchange, date,
    open, high, low, close, volume, open_interest, oi_change,
    expiry_in_use, roll_date, cumulative_adj,
    adjustment_type, provider, source_type, normalisation_version
) VALUES (
    :underlying_id, 'NFO', :date,
    :open, :high, :low, :close, :volume, :open_interest, :oi_change,
    :expiry_in_use, :roll_date, :cumulative_adj,
    'PANAMA_RATIO', 'derived_panama', 'DERIVED', '2.0.0'
)
ON CONFLICT (underlying_id, exchange, date) DO UPDATE SET
    open            = EXCLUDED.open,
    high            = EXCLUDED.high,
    low             = EXCLUDED.low,
    close           = EXCLUDED.close,
    volume          = EXCLUDED.volume,
    open_interest   = EXCLUDED.open_interest,
    oi_change       = EXCLUDED.oi_change,
    expiry_in_use   = EXCLUDED.expiry_in_use,
    roll_date       = EXCLUDED.roll_date,
    cumulative_adj  = EXCLUDED.cumulative_adj,
    updated_at      = NOW()
"""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run(symbol_filter: Optional[str], dry_run: bool) -> dict:
    from src.core.settings import get_settings  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    async with engine.begin() as conn:
        # Ensure table exists
        for stmt in _CREATE_TABLE_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await conn.execute(text(stmt))

    # Fetch all futures 1d candles from futures_candle
    print("Fetching futures_candle (1d) from DB …")
    where_clause = "AND underlying_id = :sym" if symbol_filter else ""
    params: dict = {}
    if symbol_filter:
        params["sym"] = f"NSE:{symbol_filter.upper()}"

    async with engine.connect() as conn:
        rows = (await conn.execute(text(f"""
            SELECT instrument_id, underlying_id, expiry,
                   time::date AS date,
                   open, high, low, close, volume,
                   open_interest, oi_change
            FROM futures_candle
            WHERE interval_str = '1d'
              AND expiry IS NOT NULL
              {where_clause}
            ORDER BY underlying_id, expiry, time
        """), params)).mappings().all()

    if not rows:
        print("No futures_candle (1d) data found. Run load_fo_bhavcopy_5y.py first.")
        await engine.dispose()
        return {"error": "no_data"}

    # Group by underlying → expiry → candles
    by_underlying: dict[str, dict[date, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        uid = row["underlying_id"] or row["instrument_id"].split(":")[0]
        # Normalise: underlying_id should be 'NSE:NIFTY' etc.
        by_underlying[uid][row["expiry"]].append(dict(row))

    print(f"  Underlyings found: {len(by_underlying)}")

    if dry_run:
        for uid, contracts in by_underlying.items():
            total = sum(len(v) for v in contracts.values())
            print(f"    {uid}: {len(contracts)} contracts, {total} candles")
        await engine.dispose()
        return {"dry_run": True, "underlyings": len(by_underlying)}

    total_rows_written = 0
    underlyings_built = 0

    for uid, contracts in sorted(by_underlying.items()):
        if len(contracts) < 1:
            continue

        series = _build_continuous_series(contracts)
        if not series:
            continue

        async with engine.begin() as conn:
            await conn.execute(text(_DELETE_SQL), {"uid": uid})
            for row in series:
                await conn.execute(text(_INSERT_SQL), {
                    "underlying_id": uid,
                    **row,
                })

        total_rows_written += len(series)
        underlyings_built += 1
        print(f"  ✓  {uid:<30}  {len(series):>5} rows  "
              f"{len(contracts)} contracts  "
              f"adj_factor_range=[{min(r['cumulative_adj'] for r in series):.4f},"
              f"{max(r['cumulative_adj'] for r in series):.4f}]")

    await engine.dispose()

    print()
    print("=" * 62)
    print("  CONTINUOUS FUTURES BUILD COMPLETE")
    print("=" * 62)
    print(f"  Underlyings built : {underlyings_built}")
    print(f"  Total rows        : {total_rows_written:,}")
    print("=" * 62)

    return {"underlyings": underlyings_built, "rows": total_rows_written}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Panama-adjusted continuous futures series",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--symbol", default=None,
                        help="Build for a single underlying only (e.g. NIFTY)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without writing to DB")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(run(symbol_filter=args.symbol, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
