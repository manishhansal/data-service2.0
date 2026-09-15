"""
load_fno_instrument_master.py
==============================
Downloads the Angel One OpenAPI scrip master (all 147K+ instruments) and
loads all NFO (F&O) contracts into:

  1. instrument_master      — canonical F&O contract registry
  2. instrument_provider_mapping — Angel One token mapping per contract

Source:
  https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json
  This is the official Angel One instrument master JSON. No authentication required
  to download it (public endpoint). Authentication is used only to verify the
  adapter is configured.

NFO instrument types handled:
  FUTSTK  → FUT (stock futures)
  FUTIDX  → FUT (index futures)
  OPTSTK  → OPT (stock options)
  OPTIDX  → OPT (index options)

Strike semantics:
  strike_raw / 100 = actual strike price
  (e.g. 2900000 → 29000.00 for NIFTY; 121000 → 1210.00 for RELIANCE)

Expiry format in source data: '29SEP2026' → parsed as datetime.strptime('%d%b%Y')

Usage:
    APP_ENV=local python3 scripts/load_fno_instrument_master.py
    APP_ENV=local python3 scripts/load_fno_instrument_master.py --dry-run
    APP_ENV=local python3 scripts/load_fno_instrument_master.py --limit 500

Safety:
  - Fully idempotent: ON CONFLICT DO UPDATE
  - candle_bar is never touched
  - Existing EQ/IDX instruments are not modified
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.settings import get_settings

logger = structlog.get_logger(__name__)

_SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)

# Angel One exchange segment for NFO
_NFO_SEG = "NFO"

# instrument_type → instrument_class mapping
_TYPE_TO_CLASS = {
    "FUTSTK": "FUT",
    "FUTIDX": "FUT",
    "OPTSTK": "OPT",
    "OPTIDX": "OPT",
}

# instrument_type → segment
_TYPE_TO_SEGMENT = {
    "FUTSTK": "FO",
    "FUTIDX": "FO",
    "OPTSTK": "FO",
    "OPTIDX": "FO",
}

# Underlying index names that map to NSE IDX instruments
_INDEX_NAMES = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYNXT50", "BANKEX", "SENSEX", "CRUDEOIL",
    "NATURALGAS", "GOLD", "SILVER",
})


def _parse_expiry(expiry_str: str) -> datetime.date | None:
    """Parse '29SEP2026' → date(2026,9,29). Returns None on failure."""
    try:
        return datetime.strptime(expiry_str.strip().upper(), "%d%b%Y").date()
    except (ValueError, AttributeError):
        return None


def _extract_option_type(symbol: str, instrument_type: str) -> str | None:
    """Extract CE or PE from option symbol. Returns None for non-options."""
    if instrument_type not in ("OPTSTK", "OPTIDX"):
        return None
    s = symbol.upper()
    if s.endswith("CE"):
        return "CE"
    if s.endswith("PE"):
        return "PE"
    return None


def _canonical_instrument_id(row: dict) -> str:
    """Build canonical instrument_id: 'NFO:{SYMBOL}'."""
    return f"NFO:{row['symbol'].upper()}"


def _underlying_instrument_id(name: str) -> str:
    """Build the instrument_id of the underlying."""
    return f"NSE:{name.upper()}"


def _build_instrument_record(row: dict) -> dict | None:
    """Map a scrip master row to an instrument_master row dict.

    Returns None if the row is malformed or should be skipped.
    """
    instrument_type = row.get("instrumenttype", "")
    if instrument_type not in _TYPE_TO_CLASS:
        return None

    expiry = _parse_expiry(row.get("expiry", ""))
    if expiry is None:
        return None

    symbol = row.get("symbol", "").upper()
    name = row.get("name", "").upper()
    instrument_id = f"NFO:{symbol}"
    instrument_class = _TYPE_TO_CLASS[instrument_type]
    segment = _TYPE_TO_SEGMENT[instrument_type]

    # Strike: raw / 100 for actual price. -1.0 means not applicable (futures).
    try:
        strike_raw = float(row.get("strike", "-1"))
    except (ValueError, TypeError):
        strike_raw = -1.0
    strike = strike_raw / 100.0 if strike_raw > 0 else None

    option_type = _extract_option_type(symbol, instrument_type)

    # Lot size and tick size
    try:
        lot_size = int(row.get("lotsize", 1))
    except (ValueError, TypeError):
        lot_size = 1

    try:
        tick_size = float(row.get("tick_size", 0.05))
    except (ValueError, TypeError):
        tick_size = 0.05

    underlying = name  # e.g. 'RELIANCE', 'NIFTY'

    return {
        "instrument_id": instrument_id,
        "trading_symbol": symbol,
        "display_symbol": symbol,
        "name": row.get("name", ""),
        "isin": None,
        "exchange": "NFO",
        "segment": segment,
        "instrument_type": instrument_type,
        "instrument_class": instrument_class,
        "underlying": underlying,
        "expiry": expiry,
        "strike": strike,
        "option_type": option_type,
        "lot_size": lot_size,
        "tick_size": tick_size,
        # active_from: contract is "listed" at least 3 months before expiry
        # For short-dated weekly options, use 30 days before expiry
        # For monthly/quarterly contracts, the NSE typically lists them ~3 months ahead
        # We conservatively set active_from to a date well in the past so all
        # currently-listed contracts are visible in the in-memory search.
        # The actual listing date is not in the Angel One scrip master response.
        "active_from": date(2020, 1, 1),  # conservative: all known contracts active since 2020
        "active_to": expiry,              # contract expires on expiry date
        "angel_token": row.get("token"),
        "angel_symbol": symbol,
        "upstox_key": None,
        "upstox_symbol": None,
    }


UPSERT_INSTRUMENT_SQL = text("""
INSERT INTO instrument_master (
    instrument_id, trading_symbol, display_symbol, name, isin,
    exchange, segment, instrument_type, instrument_class,
    underlying, expiry, strike, option_type,
    lot_size, tick_size, active_from, active_to,
    angel_token, angel_symbol, upstox_key, upstox_symbol,
    created_at, updated_at
) VALUES (
    :instrument_id, :trading_symbol, :display_symbol, :name, :isin,
    :exchange, :segment, :instrument_type, :instrument_class,
    :underlying, :expiry, :strike, :option_type,
    :lot_size, :tick_size, :active_from, :active_to,
    :angel_token, :angel_symbol, :upstox_key, :upstox_symbol,
    NOW(), NOW()
)
ON CONFLICT (instrument_id) DO UPDATE SET
    trading_symbol  = EXCLUDED.trading_symbol,
    name            = EXCLUDED.name,
    expiry          = EXCLUDED.expiry,
    strike          = EXCLUDED.strike,
    option_type     = EXCLUDED.option_type,
    lot_size        = EXCLUDED.lot_size,
    tick_size       = EXCLUDED.tick_size,
    active_from     = EXCLUDED.active_from,
    active_to       = EXCLUDED.active_to,
    angel_token     = EXCLUDED.angel_token,
    angel_symbol    = EXCLUDED.angel_symbol,
    instrument_class= EXCLUDED.instrument_class,
    updated_at      = NOW()
""")

UPSERT_MAPPING_SQL = text("""
INSERT INTO instrument_provider_mapping (
    instrument_id, provider, provider_instrument_id,
    provider_symbol, exchange_segment, valid_from, is_active
) VALUES (
    :instrument_id, 'angel_one', :token,
    :symbol, 'NFO_FO', :valid_from, TRUE
)
ON CONFLICT (instrument_id, provider, valid_from) DO UPDATE SET
    provider_instrument_id = EXCLUDED.provider_instrument_id,
    provider_symbol        = EXCLUDED.provider_symbol,
    is_active              = TRUE,
    updated_at             = NOW()
""")


async def fetch_scrip_master() -> list[dict]:
    """Download the Angel One scrip master JSON."""
    import httpx
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(_SCRIP_MASTER_URL)
        resp.raise_for_status()
        return resp.json()


async def run(dry_run: bool = False, limit: int | None = None) -> dict:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    await logger.ainfo("load_fno.fetching_scrip_master", url=_SCRIP_MASTER_URL)
    all_instruments = await fetch_scrip_master()
    await logger.ainfo("load_fno.fetched", total=len(all_instruments))

    # Filter to NFO only
    nfo_raw = [r for r in all_instruments if r.get("exch_seg") == _NFO_SEG]
    await logger.ainfo("load_fno.nfo_count", nfo=len(nfo_raw))

    # Apply optional limit (for dry-run / testing)
    if limit is not None:
        nfo_raw = nfo_raw[:limit]

    # Build instrument records
    records = []
    skipped = 0
    for row in nfo_raw:
        rec = _build_instrument_record(row)
        if rec is None:
            skipped += 1
            continue
        records.append(rec)

    await logger.ainfo(
        "load_fno.records_built",
        built=len(records),
        skipped=skipped,
        types={
            "FUTSTK": sum(1 for r in records if r["instrument_type"] == "FUTSTK"),
            "FUTIDX": sum(1 for r in records if r["instrument_type"] == "FUTIDX"),
            "OPTSTK": sum(1 for r in records if r["instrument_type"] == "OPTSTK"),
            "OPTIDX": sum(1 for r in records if r["instrument_type"] == "OPTIDX"),
        },
    )

    if dry_run:
        print(f"\nDRY RUN: would upsert {len(records):,} F&O instruments")
        print(f"  FUTSTK: {sum(1 for r in records if r['instrument_type']=='FUTSTK'):,}")
        print(f"  FUTIDX: {sum(1 for r in records if r['instrument_type']=='FUTIDX'):,}")
        print(f"  OPTSTK: {sum(1 for r in records if r['instrument_type']=='OPTSTK'):,}")
        print(f"  OPTIDX: {sum(1 for r in records if r['instrument_type']=='OPTIDX'):,}")
        print(f"  Skipped (malformed): {skipped:,}")
        # Show sample
        for rec in records[:3]:
            print(f"  Sample: {rec['instrument_id']} expiry={rec['expiry']} "
                  f"strike={rec['strike']} token={rec['angel_token']}")
        await engine.dispose()
        return {"dry_run": True, "would_upsert": len(records)}

    # Batch upsert in chunks of 1000 for performance
    inserted = 0
    mappings_inserted = 0
    batch_size = 1000

    async with engine.begin() as conn:
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            for rec in batch:
                await conn.execute(UPSERT_INSTRUMENT_SQL, rec)
                # Also upsert provider mapping if token present
                if rec.get("angel_token"):
                    await conn.execute(UPSERT_MAPPING_SQL, {
                        "instrument_id": rec["instrument_id"],
                        "token": rec["angel_token"],
                        "symbol": rec["trading_symbol"],
                        "valid_from": date(2020, 1, 1),  # consistent with active_from
                    })
                    mappings_inserted += 1
                inserted += 1

            await logger.ainfo(
                "load_fno.batch_complete",
                batch=i // batch_size + 1,
                inserted_so_far=inserted,
            )

    # Final counts
    async with engine.connect() as conn:
        im_total = (await conn.execute(text("SELECT COUNT(*) FROM instrument_master"))).scalar()
        im_fut = (await conn.execute(
            text("SELECT COUNT(*) FROM instrument_master WHERE instrument_class='FUT'")
        )).scalar()
        im_opt = (await conn.execute(
            text("SELECT COUNT(*) FROM instrument_master WHERE instrument_class='OPT'")
        )).scalar()
        ipm_total = (await conn.execute(
            text("SELECT COUNT(*) FROM instrument_provider_mapping")
        )).scalar()

    await engine.dispose()

    return {
        "dry_run": False,
        "upserted": inserted,
        "mappings": mappings_inserted,
        "instrument_master_total": im_total,
        "instrument_master_fut": im_fut,
        "instrument_master_opt": im_opt,
        "instrument_provider_mapping_total": ipm_total,
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Load F&O instruments from Angel One scrip master")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of NFO records (for testing)")
    args = parser.parse_args()

    result = await run(dry_run=args.dry_run, limit=args.limit)
    if args.dry_run:
        return 0

    print(f"\n{'='*60}")
    print("  F&O INSTRUMENT MASTER LOAD COMPLETE")
    print(f"{'='*60}")
    print(f"  F&O instruments upserted: {result['upserted']:,}")
    print(f"  Provider mappings:        {result['mappings']:,}")
    print(f"  instrument_master total:  {result['instrument_master_total']:,}")
    print(f"    FUT:                    {result['instrument_master_fut']:,}")
    print(f"    OPT:                    {result['instrument_master_opt']:,}")
    print(f"  instrument_provider_mapping: {result['instrument_provider_mapping_total']:,}")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
