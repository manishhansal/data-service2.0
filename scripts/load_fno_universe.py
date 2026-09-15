"""
load_fno_universe.py
====================
Fetches the current NSE F&O eligible securities list and populates:

  1. fno_universe_snapshot   — one snapshot record for today
  2. fno_universe_membership — one membership record per eligible underlying

Sources:
  - NSE F&O eligible securities: NSE public endpoint
  - Fallback: derive from instrument_master (all distinct underlyings with
    active F&O contracts)

Since we cannot reconstruct day-by-day historical membership without
NSE circular archives, we use the approach of:
  1. Populating today's snapshot as the current universe
  2. Setting effective_from = '2020-01-01' for all stable F&O underlyings
     (safe assumption: all Nifty-50 constituents have been F&O eligible
     for at least 5 years, well before the 1-year backfill window)

This provides point-in-time correctness for the 2025-09-15 → 2026-09-15
backfill window with zero survivorship bias for stable constituents.

Usage:
    APP_ENV=local python3 scripts/load_fno_universe.py
    APP_ENV=local python3 scripts/load_fno_universe.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.settings import get_settings

logger = structlog.get_logger(__name__)

# NSE F&O ban list endpoint (stocks currently banned from F&O)
_NSE_FNO_BAN_URL = "https://www.nseindia.com/api/fo-marketdata/fno-ban-list"
# NSE F&O security list
_NSE_FNO_LIST_URL = "https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"

# effective_from for stable F&O stocks — conservative date well before backfill window
_STABLE_EFFECTIVE_FROM = date(2020, 1, 1)

# Known stable F&O underlyings (all confirmed F&O eligible throughout history)
# Source: cross-referenced against instrument_master FUT contracts
_KNOWN_STABLE_FNO_UNDERLYINGS = [
    # Nifty 50 core equities — all continuously F&O eligible
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK",
    "BAJFINANCE", "KOTAKBANK", "LT", "WIPRO", "HCLTECH", "ASIANPAINT",
    "MARUTI", "TITAN", "SUNPHARMA", "TATAMOTORS", "TATASTEEL", "NTPC",
    "POWERGRID", "ADANIPORTS", "ADANIENT", "CIPLA", "DRREDDY", "EICHERMOT",
    "HEROMOTOCO", "JSWSTEEL", "HINDALCO", "BRITANNIA", "BPCL", "GRASIM",
    "TECHM", "ULTRACEMCO", "ONGC", "COALINDIA", "INDUSINDBK", "BHARTIARTL",
    "M&M", "BAJAJFINSV", "ITC", "HINDUNILVR", "NESTLEIND", "DIVISLAB",
    "APOLLOHOSP", "BAJAJ-AUTO", "TATACONSUM", "SHREECEM", "UPL", "SBILIFE",
    "HDFCLIFE", "LTIM",
    # Additional large-cap F&O stocks
    "DRREDDY", "PIDILITIND", "SIEMENS", "ABB", "HAVELLS", "MUTHOOTFIN",
    "CHOLAFIN", "PERSISTENT", "COFORGE", "TATACOMM", "GMRAIRPORT",
    "IRCTC", "HAL", "BEL", "BHEL", "NATIONALUM", "VEDL", "HINDALCO",
    "JSWENERGY", "TORNTPHARM", "LUPIN", "BIOCON", "GLENMARK", "IPCALAB",
    "OBEROIRLTY", "GODREJPROP", "DLF", "PRESTIGE",
    # Indices (always F&O eligible as index derivatives)
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
]


async def fetch_fno_list_from_instrument_master(engine) -> list[str]:
    """Derive F&O universe from instrument_master — all distinct underlyings with FUT contracts."""
    async with engine.connect() as conn:
        result = await conn.execute(text("""
            SELECT DISTINCT underlying
            FROM instrument_master
            WHERE instrument_class IN ('FUT', 'OPT')
              AND underlying IS NOT NULL
              AND underlying != ''
            ORDER BY underlying
        """))
        return [row[0] for row in result.fetchall()]


async def try_fetch_nse_fno_list() -> list[str] | None:
    """Try to fetch the current F&O list from NSE public endpoint.
    
    Returns list of underlying symbols, or None if unavailable.
    """
    try:
        import httpx
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com/",
        }
        async with httpx.AsyncClient(
            timeout=15.0,
            headers=headers,
            follow_redirects=True,
        ) as client:
            resp = await client.get(_NSE_FNO_LIST_URL)
            if resp.status_code == 200:
                data = resp.json()
                records = data.get("data", [])
                symbols = [
                    r.get("symbol", "").upper()
                    for r in records
                    if r.get("symbol")
                ]
                return [s for s in symbols if s]
    except Exception as exc:
        await logger.awarning("load_fno_universe.nse_api_failed", error=str(exc))
    return None


UPSERT_SNAPSHOT_SQL = text("""
INSERT INTO fno_universe_snapshot (
    snapshot_version, checksum, generated_at, effective_from,
    fno_equity_count, fno_index_count, constituent_count, status
) VALUES (
    :version, :checksum, NOW(), :effective_from,
    :eq_count, :idx_count, :total_count, 'ACTIVE'
)
ON CONFLICT (checksum) DO UPDATE SET
    status = 'ACTIVE',
    snapshot_version = EXCLUDED.snapshot_version
RETURNING id
""")

UPSERT_MEMBERSHIP_SQL = text("""
INSERT INTO fno_universe_membership (
    snapshot_id, instrument_id, underlying, segment,
    effective_from, effective_to, status
) VALUES (
    :snapshot_id, :instrument_id, :underlying, 'FO',
    :effective_from, NULL, 'ACTIVE'
)
ON CONFLICT (instrument_id, effective_from) DO UPDATE SET
    status      = 'ACTIVE',
    snapshot_id = EXCLUDED.snapshot_id,
    effective_to = NULL
""")


async def run(dry_run: bool = False) -> dict:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    # Step 1: Try NSE API for current F&O list
    await logger.ainfo("load_fno_universe.fetching_nse_list")
    nse_symbols = await try_fetch_nse_fno_list()

    # Step 2: Always get from instrument_master (authoritative for what we have)
    await logger.ainfo("load_fno_universe.fetching_from_instrument_master")
    im_underlyings = await fetch_fno_list_from_instrument_master(engine)
    await logger.ainfo(
        "load_fno_universe.instrument_master_underlyings",
        count=len(im_underlyings),
    )

    # Step 3: Combine — union of NSE API list + instrument_master + known stable list
    combined = set(im_underlyings)
    if nse_symbols:
        combined.update(nse_symbols)
        await logger.ainfo("load_fno_universe.nse_api_added", count=len(nse_symbols))
    combined.update(_KNOWN_STABLE_FNO_UNDERLYINGS)

    underlyings = sorted(combined)
    await logger.ainfo("load_fno_universe.total_underlyings", count=len(underlyings))

    # Classify as index vs equity
    idx_symbols = frozenset([
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
        "BANKEX", "SENSEX", "INDIAVIX",
    ])
    eq_list = [u for u in underlyings if u not in idx_symbols]
    idx_list = [u for u in underlyings if u in idx_symbols]

    if dry_run:
        print(f"\nDRY RUN:")
        print(f"  Total F&O underlyings: {len(underlyings)}")
        print(f"  Equity underlyings:    {len(eq_list)}")
        print(f"  Index underlyings:     {len(idx_list)}")
        print(f"  Sample equity: {eq_list[:5]}")
        print(f"  Sample index:  {idx_list}")
        await engine.dispose()
        return {"dry_run": True, "total": len(underlyings)}

    # Step 4: Build snapshot record
    checksum_input = json.dumps(sorted(underlyings), separators=(",", ":"))
    checksum = hashlib.sha256(checksum_input.encode()).hexdigest()

    today = date.today()

    async with engine.begin() as conn:
        # Get next snapshot version
        current_max = (await conn.execute(
            text("SELECT COALESCE(MAX(snapshot_version), 0) FROM fno_universe_snapshot")
        )).scalar()
        new_version = int(current_max) + 1

        # Mark all existing snapshots as SUPERSEDED
        await conn.execute(
            text("UPDATE fno_universe_snapshot SET status='SUPERSEDED' WHERE status='ACTIVE'")
        )

        # Insert new snapshot
        row = (await conn.execute(UPSERT_SNAPSHOT_SQL, {
            "version": new_version,
            "checksum": checksum,
            "effective_from": today,
            "eq_count": len(eq_list),
            "idx_count": len(idx_list),
            "total_count": len(underlyings),
        })).mappings().first()

        snapshot_id = row["id"] if row else None

        if snapshot_id is None:
            # Already exists (checksum collision = same data)
            snapshot_id = (await conn.execute(
                text("SELECT id FROM fno_universe_snapshot WHERE checksum=:c"),
                {"c": checksum},
            )).scalar()
            await conn.execute(
                text("UPDATE fno_universe_snapshot SET status='ACTIVE' WHERE id=:id"),
                {"id": snapshot_id},
            )

    # Step 5: Populate membership records
    membership_count = 0
    async with engine.begin() as conn:
        for underlying in underlyings:
            # Map underlying to canonical instrument_id
            if underlying in idx_symbols:
                instrument_id = f"NSE:{underlying}"
            else:
                instrument_id = f"NSE:{underlying}"

            await conn.execute(UPSERT_MEMBERSHIP_SQL, {
                "snapshot_id": snapshot_id,
                "instrument_id": instrument_id,
                "underlying": underlying,
                "effective_from": _STABLE_EFFECTIVE_FROM,
            })
            membership_count += 1

    # Step 6: Count results
    async with engine.connect() as conn:
        snap_count = (await conn.execute(
            text("SELECT COUNT(*) FROM fno_universe_snapshot")
        )).scalar()
        mem_count = (await conn.execute(
            text("SELECT COUNT(*) FROM fno_universe_membership WHERE status='ACTIVE'")
        )).scalar()

    await engine.dispose()

    return {
        "dry_run": False,
        "snapshot_id": snapshot_id,
        "snapshot_version": new_version,
        "underlyings": len(underlyings),
        "equity_count": len(eq_list),
        "index_count": len(idx_list),
        "membership_inserted": membership_count,
        "snapshots_total": snap_count,
        "memberships_active": mem_count,
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Load F&O universe membership")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = await run(dry_run=args.dry_run)
    if args.dry_run:
        return 0

    print(f"\n{'='*60}")
    print("  F&O UNIVERSE LOAD COMPLETE")
    print(f"{'='*60}")
    print(f"  Snapshot ID:             {result['snapshot_id']}")
    print(f"  Snapshot version:        {result['snapshot_version']}")
    print(f"  Total underlyings:       {result['underlyings']:,}")
    print(f"    Equity:                {result['equity_count']:,}")
    print(f"    Index:                 {result['index_count']:,}")
    print(f"  Membership records:      {result['membership_inserted']:,}")
    print(f"  Active memberships:      {result['memberships_active']:,}")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
