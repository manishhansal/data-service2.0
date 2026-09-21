"""
seed_fo_universe.py
====================
Populate the ``fo_universe`` table with every NSE F&O-eligible instrument
observed in the last 5 years, enriched with ISIN, provider keys, sector,
and backfill priority.

Data sources (merged in priority order):
  1. ``options_candle`` / ``futures_candle`` DB tables — gives us the exact
     symbols that have ever had F&O contracts, their first/last dates
  2. Angel One OpenAPI scrip master (public URL, no auth) — ISINs, lot sizes,
     company names, tokens
  3. Static curated metadata (sector labels, Yahoo tickers, successor symbols,
     known listing / delisting dates for merged companies)

Priority assignment:
    1  NSE index futures underlyings  (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY)
    2  Stock futures underlyings       (all equities with futures contracts)
    3  NSE broad indices               (INDIA VIX, sectoral indices, etc.)
    4  Nifty 50 constituents           (already in primary backfill list)
    5  Other F&O-eligible equities

Usage
-----
    # Seed / refresh the fo_universe table:
    APP_ENV=local python3 scripts/seed_fo_universe.py

    # Dry-run — show what would be inserted/updated:
    APP_ENV=local python3 scripts/seed_fo_universe.py --dry-run

    # Via Makefile:
    make seed-fo-universe

Safety
------
Idempotent — uses INSERT … ON CONFLICT (symbol, exchange) DO UPDATE.
Existing rows are updated in-place; no rows are deleted.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
import structlog

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Static metadata — sector labels, Yahoo symbols, notes, successor mapping
# ---------------------------------------------------------------------------

# Nifty 50 set (priority = 4)
_NIFTY50 = frozenset({
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK",
    "BAJAJFINSV","BAJFINANCE","BHARTIARTL","BPCL","BRITANNIA",
    "CIPLA","COALINDIA","DIVISLAB","DRREDDY","EICHERMOT","GRASIM",
    "HCLTECH","HDFCBANK","HDFCLIFE","HEROMOTOCO","HINDALCO","HINDUNILVR",
    "ICICIBANK","INDUSINDBK","INFY","ITC","JSWSTEEL","KOTAKBANK",
    "LT","M&M","MARUTI","NESTLEIND","NTPC","ONGC","POWERGRID",
    "RELIANCE","SBILIFE","SBIN","SHREECEM","SUNPHARMA","TATAMOTORS",
    "TATASTEEL","TCS","TECHM","TITAN","ULTRACEMCO","WIPRO",
})

# Index futures underlyings (priority = 1)
_INDEX_FUTURES = frozenset({"NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY"})

# Broad NSE indices (priority = 3)
_BROAD_INDICES = frozenset({
    "NIFTY 50","NIFTY BANK","NIFTY IT","NIFTY MIDCAP 50","NIFTY FMCG",
    "INDIA VIX","NIFTY FIN SERVICE","NIFTY PHARMA","NIFTY AUTO","NIFTY REALTY",
    "NIFTYNXT50","MIDCPNIFTY",
})

# Successor symbol map — merged/renamed instruments
_SUCCESSOR: dict[str, str] = {
    "HDFC":       "HDFCBANK",    # HDFC Ltd merged into HDFCBANK Jul 2023
    "MINDTREE":   "LTIM",        # Merged into LTIMindtree Nov 2022
    "MOTHERSUMI": "MOTHERSON",   # Renamed to MOTHERSON Jun 2022
    "AMARAJABAT": "AMARARAJABAT",# Renamed
    "CADILAHC":   "ZYDUSLIFE",   # Renamed Apr 2022
    "IDFC":       "IDFCFIRSTB",  # Merged
    "L&TFH":      "LTF",         # Renamed
    "MCDOWELL-N": "UNITDSPR",    # Renamed
    "PVR":        "PVRINOX",     # Merged with INOX May 2023
    "SRTRANSFIN": "SHRIRAMFIN",  # Renamed Dec 2022
    "GMRINFRA":   "GMRAIRPORT",  # Demerger Dec 2024
    "APLLTD":     "APLAPOLLO",   # Renamed
    "DELTACORP":  "DELTA",
    "FSL":        "FIRSTSOURCE",
}

# Spot delisting dates (approximate) for merged/delisted symbols
_SPOT_DELISTED: dict[str, date] = {
    "HDFC":       date(2023, 7, 13),
    "MINDTREE":   date(2022, 11, 30),
    "MOTHERSUMI": date(2022, 6, 8),
    "CADILAHC":   date(2022, 4, 7),
    "PVR":        date(2023, 5, 12),
    "IDFC":       date(2024, 10, 10),
}

# Known F&O delisting dates
_FO_DELISTED: dict[str, date] = {
    "HDFC":       date(2023, 7, 13),
    "MINDTREE":   date(2022, 11, 30),
    "MOTHERSUMI": date(2022, 6, 8),
    "CADILAHC":   date(2022, 3, 4),
    "PVR":        date(2023, 5, 11),
    "AMARAJABAT": date(2022, 12, 29),
    "APLLTD":     date(2022, 6, 30),
    "BALRAMCHIN": date(2024, 10, 31),
    "SRTRANSFIN": date(2022, 12, 19),
    "DELTACORP":  date(2024, 2, 29),
    "FSL":        date(2023, 3, 29),
    "GSPL":       date(2022, 11, 24),
    "HONAUT":     date(2023, 4, 27),
    "IBULHSGFIN": date(2023, 12, 13),
    "IDFC":       date(2024, 10, 9),
    "L&TFH":      date(2024, 4, 22),
    "MCDOWELL-N": date(2024, 6, 6),
    "GMRINFRA":   date(2024, 12, 10),
    "COROMANDEL": date(2025, 2, 27),
    "CANFINHOME": date(2025, 2, 27),
    "NAVINFLUOR": date(2025, 2, 27),
}

# Sector mapping (curated)
_SECTOR: dict[str, str] = {
    # Financials
    "HDFCBANK":"Financials","ICICIBANK":"Financials","KOTAKBANK":"Financials",
    "AXISBANK":"Financials","SBIN":"Financials","BAJFINANCE":"Financials",
    "BAJAJFINSV":"Financials","HDFCLIFE":"Financials","SBILIFE":"Financials",
    "ICICIGI":"Financials","ICICIPRULI":"Financials","MFSL":"Financials",
    "LICI":"Financials","HDFCAMC":"Financials","CAMS":"Financials",
    "CDSL":"Financials","BSE":"Financials","MCX":"Financials",
    "IEX":"Financials","ANGELONE":"Financials","MOTILALOFS":"Financials",
    "NAM-INDIA":"Financials","POLICYBZR":"Financials","KFINTECH":"Financials",
    "BANDHANBNK":"Financials","FEDERALBNK":"Financials","RBLBANK":"Financials",
    "IDFCFIRSTB":"Financials","INDUSINDBK":"Financials","CANBK":"Financials",
    "BANKBARODA":"Financials","PNB":"Financials","BANKBARODA":"Financials",
    "INDIANB":"Financials","UNIONBANK":"Financials","MAHABANK":"Financials",
    "BANKINDIA":"Financials","LICHSGFIN":"Financials","PNBHOUSING":"Financials",
    "CANFINHOME":"Financials","MANAPPURAM":"Financials","MUTHOOTFIN":"Financials",
    "CHOLAFIN":"Financials","M&MFIN":"Financials","SHRIRAMFIN":"Financials",
    "L&TFH":"Financials","LTF":"Financials","IIFL":"Financials",
    "POONAWALLA":"Financials","ABCAPITAL":"Financials","JIOFIN":"Financials",
    "LODHA":"Financials","IBULHSGFIN":"Financials","IDFC":"Financials",
    "HDFC":"Financials","HUDCO":"Financials","IRFC":"Financials",
    "IREDA":"Financials","PFC":"Financials","RECLTD":"Financials",
    "YESBANK":"Financials","SBICARD":"Financials",
    # IT
    "TCS":"IT","INFY":"IT","WIPRO":"IT","HCLTECH":"IT","TECHM":"IT",
    "LTIM":"IT","LTTS":"IT","MPHASIS":"IT","PERSISTENT":"IT","COFORGE":"IT",
    "KPITTECH":"IT","CYIENT":"IT","OFSS":"IT","MINDTREE":"IT",
    "BSOFT":"IT","INTELLECT":"IT",
    # Energy
    "RELIANCE":"Energy","ONGC":"Energy","IOC":"Energy","BPCL":"Energy",
    "HINDPETRO":"Energy","GAIL":"Energy","OIL":"Energy","PETRONET":"Energy",
    "ATGL":"Energy","TATAPOWER":"Energy","TORNTPOWER":"Energy",
    "JSWENERGY":"Energy","NTPC":"Energy","POWERGRID":"Energy","NHPC":"Energy",
    "SJVN":"Energy","COALINDIA":"Energy","INOXWIND":"Energy","SUZLON":"Energy",
    "IREDA":"Energy","WAAREEENER":"Energy",
    # Consumer
    "HINDUNILVR":"Consumer","ITC":"Consumer","NESTLEIND":"Consumer",
    "BRITANNIA":"Consumer","DABUR":"Consumer","MARICO":"Consumer",
    "COLPAL":"Consumer","GODREJCP":"Consumer","UBL":"Consumer",
    "MCDOWELL-N":"Consumer","PAGEIND":"Consumer","ABFRL":"Consumer",
    "TRENT":"Consumer","DMART":"Consumer","JUBLFOOD":"Consumer",
    "PVRINOX":"Consumer","PVR":"Consumer","PATANJALI":"Consumer",
    "BATAINDIA":"Consumer","NYKAA":"Consumer","ETERNAL":"Consumer",
    "SWIGGY":"Consumer","RADICO":"Consumer",
    # Auto
    "MARUTI":"Auto","TATAMOTORS":"Auto","M&M":"Auto","HEROMOTOCO":"Auto",
    "EICHERMOT":"Auto","BAJAJ-AUTO":"Auto","TVSMOTOR":"Auto",
    "ASHOKLEY":"Auto","MOTHERSON":"Auto","MOTHERSUMI":"Auto",
    "APOLLOTYRE":"Auto","BALKRISIND":"Auto","EXIDEIND":"Auto",
    "AMARAJABAT":"Auto","MRF":"Auto","ESCORTS":"Auto","TIINDIA":"Auto",
    "FORCEMOT":"Auto","HYUNDAI":"Auto","SONACOMS":"Auto","UNOMINDA":"Auto",
    # Pharma
    "SUNPHARMA":"Pharma","DRREDDY":"Pharma","CIPLA":"Pharma","DIVISLAB":"Pharma",
    "APOLLOHOSP":"Pharma","AUROPHARMA":"Pharma","LUPIN":"Pharma",
    "ALKEM":"Pharma","GLENMARK":"Pharma","BIOCON":"Pharma",
    "IPCALAB":"Pharma","NAVINFLUOR":"Pharma","ABBOTINDIA":"Pharma",
    "METROPOLIS":"Pharma","LALPATHLAB":"Pharma","GRANULES":"Pharma",
    "PFIZER":"Pharma","ZYDUSLIFE":"Pharma","CADILAHC":"Pharma",
    "LAURUSLABS":"Pharma","SYNGENE":"Pharma","MANKIND":"Pharma",
    "MAXHEALTH":"Pharma","FORTIS":"Pharma","PPLPHARMA":"Pharma",
    # Metals
    "TATASTEEL":"Metals","JSWSTEEL":"Metals","HINDALCO":"Metals",
    "VEDL":"Metals","JINDALSTEL":"Metals","SAIL":"Metals","NMDC":"Metals",
    "NATIONALUM":"Metals","HINDCOPPER":"Metals","HINDZINC":"Metals",
    "JSL":"Metals",
    # Cement
    "ULTRACEMCO":"Cement","GRASIM":"Cement","SHREECEM":"Cement",
    "ACC":"Cement","AMBUJACEM":"Cement","DALBHARAT":"Cement",
    "JKCEMENT":"Cement","RAMCOCEM":"Cement","INDIACEM":"Cement",
    # IT (extended)
    "NAUKRI":"IT","INDIAMART":"IT",
    # Chemicals
    "PIIND":"Chemicals","SRF":"Chemicals","DEEPAKNTR":"Chemicals",
    "TATACHEM":"Chemicals","GNFC":"Chemicals","AARTIIND":"Chemicals",
    "CHAMBLFERT":"Chemicals","COROMANDEL":"Chemicals","RAIN":"Chemicals",
    # Infra / Industrial
    "LT":"Industrials","HAL":"Industrials","BEL":"Industrials",
    "BHEL":"Industrials","CONCOR":"Industrials","IRCTC":"Industrials",
    "ABB":"Industrials","SIEMENS":"Industrials","HAVELLS":"Industrials",
    "CGPOWER":"Industrials","POLYCAB":"Industrials","CUMMINSIND":"Industrials",
    "BOSCHLTD":"Industrials","TITAGARH":"Industrials","COCHINSHIP":"Industrials",
    "MAZDOCK":"Industrials","BDL":"Industrials","POWERINDIA":"Industrials",
    "KAYNES":"Industrials","KEI":"Industrials","APLAPOLLO":"Industrials",
    "ASTRAL":"Industrials","GMRAIRPORT":"Industrials","GMRINFRA":"Industrials",
    "INDIGO":"Industrials","DELHIVERY":"Industrials","BLUESTARCO":"Industrials",
    "CROMPTON":"Industrials","HONAUT":"Industrials","AMBER":"Industrials",
    "DIXON":"Industrials","TATATECH":"Industrials","TATAELXSI":"Industrials",
    "GVT&D":"Industrials","VMM":"Industrials","TMPV":"Industrials",
    "MOTILALOFS":"Financials",
    # Real Estate
    "DLF":"RealEstate","GODREJPROP":"RealEstate","OBEROIRLTY":"RealEstate",
    "LODHA":"RealEstate","PRESTIGE":"RealEstate","PHOENIXLTD":"RealEstate",
    # Telecom
    "BHARTIARTL":"Telecom","IDEA":"Telecom","INDUSTOWER":"Telecom","TATACOMM":"Telecom",
    # Media
    "SUNTV":"Media","ZEEL":"Media",
    # Gas / Utilities
    "IGL":"Utilities","MGL":"Utilities","GUJGASLTD":"Utilities","GSPL":"Utilities",
    # FMCG (misc)
    "PIDILITIND":"Consumer","BERGEPAINT":"Consumer","ASIANPAINT":"Consumer",
    "TITAN":"Consumer","BAJAJHLDNG":"Financials",
}

# Yahoo Finance suffix map — most NSE stocks use .NS but some have quirks
def _yahoo_sym(symbol: str) -> str:
    """Build Yahoo Finance ticker from NSE symbol."""
    # A few known overrides
    _overrides = {
        "M&M": "M%26M.NS",     # ampersand in URL
        "M&MFIN": "M%26MFIN.NS",
        "L&TFH": "L%26TFH.NS",
    }
    return _overrides.get(symbol, f"{symbol}.NS")


# ---------------------------------------------------------------------------
# UPSERT SQL
# ---------------------------------------------------------------------------
_UPSERT_SQL = """
INSERT INTO fo_universe (
    instrument_id, symbol, company_name, isin,
    exchange, instrument_class, instrument_type, sector, is_index,
    backfill_priority,
    fo_listed_date, fo_delisted_date, is_fo_active,
    spot_listed_date, spot_delisted_date, is_spot_active,
    successor_symbol, notes, lot_size,
    upstox_key, angel_token, yahoo_symbol,
    created_at, updated_at
) VALUES (
    :instrument_id, :symbol, :company_name, :isin,
    :exchange, :instrument_class, :instrument_type, :sector, :is_index,
    :backfill_priority,
    :fo_listed_date, :fo_delisted_date, :is_fo_active,
    :spot_listed_date, :spot_delisted_date, :is_spot_active,
    :successor_symbol, :notes, :lot_size,
    :upstox_key, :angel_token, :yahoo_symbol,
    NOW(), NOW()
)
ON CONFLICT (symbol, exchange) DO UPDATE SET
    instrument_id       = EXCLUDED.instrument_id,
    company_name        = COALESCE(EXCLUDED.company_name, fo_universe.company_name),
    isin                = COALESCE(EXCLUDED.isin,         fo_universe.isin),
    instrument_class    = EXCLUDED.instrument_class,
    instrument_type     = EXCLUDED.instrument_type,
    sector              = COALESCE(EXCLUDED.sector,       fo_universe.sector),
    is_index            = EXCLUDED.is_index,
    backfill_priority   = EXCLUDED.backfill_priority,
    fo_listed_date      = COALESCE(EXCLUDED.fo_listed_date,   fo_universe.fo_listed_date),
    fo_delisted_date    = COALESCE(EXCLUDED.fo_delisted_date, fo_universe.fo_delisted_date),
    is_fo_active        = EXCLUDED.is_fo_active,
    spot_listed_date    = COALESCE(EXCLUDED.spot_listed_date,   fo_universe.spot_listed_date),
    spot_delisted_date  = COALESCE(EXCLUDED.spot_delisted_date, fo_universe.spot_delisted_date),
    is_spot_active      = EXCLUDED.is_spot_active,
    successor_symbol    = COALESCE(EXCLUDED.successor_symbol, fo_universe.successor_symbol),
    notes               = COALESCE(EXCLUDED.notes,            fo_universe.notes),
    lot_size            = COALESCE(EXCLUDED.lot_size,         fo_universe.lot_size),
    upstox_key          = COALESCE(EXCLUDED.upstox_key,       fo_universe.upstox_key),
    angel_token         = COALESCE(EXCLUDED.angel_token,      fo_universe.angel_token),
    yahoo_symbol        = COALESCE(EXCLUDED.yahoo_symbol,     fo_universe.yahoo_symbol),
    updated_at          = NOW()
"""


# ---------------------------------------------------------------------------
# Scrip master download (Angel One — for ISINs, lot sizes, tokens)
# ---------------------------------------------------------------------------
async def _fetch_scrip_master() -> dict[str, dict]:
    """Download Angel One scrip master and index by NSE symbol → metadata."""
    url = ("https://margincalculator.angelbroking.com"
           "/OpenAPI_File/files/OpenAPIScripMaster.json")
    h = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(url, headers=h)
    data = r.json()

    # BSE segment carries ISINs for NSE equities
    lookup: dict[str, dict] = {}
    for row in data:
        seg  = row.get("exch_seg", "")
        sym  = row.get("symbol", "").upper()
        isin = row.get("isin", "")
        name = row.get("name", "")
        tok  = row.get("token", "")
        lot  = row.get("lotsize", "")
        if sym and isin and isin.startswith("IN"):
            if sym not in lookup:
                lookup[sym] = {
                    "isin": isin, "name": name,
                    "token": tok if seg == "NSE" else "",
                    "lot": int(lot) if str(lot).isdigit() else None,
                }
            elif not lookup[sym].get("token") and seg == "NSE":
                lookup[sym]["token"] = tok

    # Also pick up NSE segment entries for tokens
    for row in data:
        if row.get("exch_seg") != "NSE":
            continue
        sym = row.get("symbol","").upper()
        if sym in lookup and not lookup[sym].get("token"):
            lookup[sym]["token"] = row.get("token","")

    return lookup


# ---------------------------------------------------------------------------
# Build instrument records
# ---------------------------------------------------------------------------

async def _build_records(engine) -> list[dict]:
    """Derive full fo_universe records from DB + scrip master."""
    from sqlalchemy import text  # noqa: PLC0415

    await logger.ainfo("seed_fo_universe.fetching_scrip_master")
    scrip = await _fetch_scrip_master()
    await logger.ainfo("seed_fo_universe.scrip_master_fetched",
                       total=len(scrip))

    # Pull all underlyings from options_candle + futures_candle
    async with engine.connect() as conn:
        opt_rows = (await conn.execute(text("""
            SELECT underlying_id,
              MIN(session_date) AS first_date,
              MAX(session_date) AS last_date,
              COUNT(DISTINCT session_date) AS days
            FROM options_candle
            GROUP BY underlying_id
        """))).mappings().all()

        fut_rows = (await conn.execute(text("""
            SELECT underlying_id,
              MIN(session_date) AS first_date,
              MAX(session_date) AS last_date,
              COUNT(DISTINCT session_date) AS days
            FROM futures_candle
            WHERE provider = 'nse_bhavcopy'
            GROUP BY underlying_id
        """))).mappings().all()

    # NSE index futures underlyings (NIFTY, BANKNIFTY etc.)
    # These appear as options_candle.underlying_id = 'NSE:NIFTY' etc.
    # but their instrument_class should be IDX, not EQ.
    _IDX_FUTURES_FULL = {"NSE:NIFTY","NSE:BANKNIFTY","NSE:FINNIFTY",
                         "NSE:MIDCPNIFTY","NSE:NIFTYFPI","NSE:NIFTYNXT50"}

    # Merge opt + fut into one dict keyed by underlying_id
    universe: dict[str, dict] = {}
    for row in list(opt_rows) + list(fut_rows):
        uid = row["underlying_id"]
        if uid is None:
            continue
        if uid not in universe:
            universe[uid] = {
                "first_date": row["first_date"],
                "last_date":  row["last_date"],
            }
        else:
            if row["first_date"] and (
                universe[uid]["first_date"] is None
                or row["first_date"] < universe[uid]["first_date"]
            ):
                universe[uid]["first_date"] = row["first_date"]
            if row["last_date"] and (
                universe[uid]["last_date"] is None
                or row["last_date"] > universe[uid]["last_date"]
            ):
                universe[uid]["last_date"] = row["last_date"]

    # Also add the 10 NSE broad indices that don't appear in F&O tables
    # but need backfill with priority 3
    _EXTRA_INDICES = [
        ("NSE:NIFTY 50",          "NIFTY 50",          "Nifty 50 Index",                3),
        ("NSE:NIFTY BANK",        "NIFTY BANK",         "Nifty Bank Index",              3),
        ("NSE:NIFTY IT",          "NIFTY IT",           "Nifty IT Index",                3),
        ("NSE:NIFTY MIDCAP 50",   "NIFTY MIDCAP 50",    "Nifty Midcap 50 Index",         3),
        ("NSE:NIFTY FMCG",        "NIFTY FMCG",         "Nifty FMCG Index",              3),
        ("NSE:INDIA VIX",         "INDIA VIX",          "India Volatility Index",        3),
        ("NSE:NIFTY FIN SERVICE", "NIFTY FIN SERVICE",  "Nifty Financial Services",      3),
        ("NSE:NIFTY PHARMA",      "NIFTY PHARMA",       "Nifty Pharma Index",            3),
        ("NSE:NIFTY AUTO",        "NIFTY AUTO",         "Nifty Auto Index",              3),
        ("NSE:NIFTY REALTY",      "NIFTY REALTY",       "Nifty Realty Index",            3),
    ]
    for uid, sym, name, prio in _EXTRA_INDICES:
        if uid not in universe:
            universe[uid] = {"first_date": None, "last_date": None}

    records: list[dict] = []

    for uid, dates in universe.items():
        # uid format: "NSE:RELIANCE"
        parts = uid.split(":", 1)
        exchange = parts[0] if len(parts) == 2 else "NSE"
        symbol   = parts[1] if len(parts) == 2 else uid

        first_d: Optional[date] = dates["first_date"]
        last_d:  Optional[date] = dates["last_date"]
        today    = date.today()

        # Determine instrument class
        is_index = uid in _IDX_FUTURES_FULL or symbol in _BROAD_INDICES or \
                   symbol.startswith("NIFTY") or symbol in ("INDIA VIX","BANKNIFTY",
                   "FINNIFTY","MIDCPNIFTY")
        if is_index:
            instrument_class = "IDX"
            instrument_type  = "IDX"
        else:
            instrument_class = "EQ"
            instrument_type  = "EQ"

        # Determine backfill priority
        if symbol in _INDEX_FUTURES:
            priority = 1
        elif instrument_class == "EQ" and symbol not in _BROAD_INDICES:
            priority = 2  # stock futures underlying
        elif is_index:
            priority = 3
        elif symbol in _NIFTY50:
            priority = 4
        else:
            priority = 5

        # F&O activity
        fo_delisted = _FO_DELISTED.get(symbol)
        is_fo_active = fo_delisted is None

        # Spot activity
        spot_delisted = _SPOT_DELISTED.get(symbol)
        is_spot_active = spot_delisted is None

        # Scrip master enrichment
        sm = scrip.get(symbol, {})
        isin        = sm.get("isin")
        company_name = sm.get("name") or symbol
        angel_token = sm.get("token") or None
        lot_size    = sm.get("lot")

        upstox_key = f"NSE_EQ|{isin}" if (isin and not is_index) else None
        if is_index:
            # Map well-known index keys
            _idx_key_map = {
                "NIFTY":            "NSE_INDEX|Nifty 50",
                "NIFTY 50":         "NSE_INDEX|Nifty 50",
                "BANKNIFTY":        "NSE_INDEX|Nifty Bank",
                "NIFTY BANK":       "NSE_INDEX|Nifty Bank",
                "FINNIFTY":         "NSE_INDEX|Nifty Fin Service",
                "NIFTY FIN SERVICE":"NSE_INDEX|Nifty Fin Service",
                "MIDCPNIFTY":       "NSE_INDEX|Nifty Midcap Select",
                "NIFTY MIDCAP 50":  "NSE_INDEX|Nifty Midcap 50",
                "INDIA VIX":        "NSE_INDEX|India VIX",
                "NIFTY IT":         "NSE_INDEX|Nifty IT",
                "NIFTY AUTO":       "NSE_INDEX|Nifty Auto",
                "NIFTY PHARMA":     "NSE_INDEX|Nifty Pharma",
                "NIFTY FMCG":       "NSE_INDEX|Nifty FMCG",
                "NIFTY REALTY":     "NSE_INDEX|Nifty Realty",
            }
            upstox_key = _idx_key_map.get(symbol)

        records.append({
            "instrument_id":    uid,
            "symbol":           symbol,
            "company_name":     company_name if company_name != symbol else None,
            "isin":             isin if isin else None,
            "exchange":         exchange,
            "instrument_class": instrument_class,
            "instrument_type":  instrument_type,
            "sector":           _SECTOR.get(symbol),
            "is_index":         is_index,
            "backfill_priority": priority,
            "fo_listed_date":   first_d,
            "fo_delisted_date": fo_delisted,
            "is_fo_active":     is_fo_active,
            "spot_listed_date": None,
            "spot_delisted_date": spot_delisted,
            "is_spot_active":   is_spot_active,
            "successor_symbol": _SUCCESSOR.get(symbol),
            "notes":            f"Delisted {fo_delisted}" if fo_delisted else None,
            "lot_size":         lot_size,
            "upstox_key":       upstox_key,
            "angel_token":      angel_token if angel_token else None,
            "yahoo_symbol":     _yahoo_sym(symbol) if not is_index else None,
        })

    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(dry_run: bool) -> None:
    from src.core.settings import get_settings  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    records = await _build_records(engine)

    # Enrich with ISINs from instrument_provider_mapping (Upstox NSE_EQ|ISIN keys)
    # and from the static _UPSTOX_INSTRUMENT_KEYS map in the engine
    from sqlalchemy import text as _text2  # noqa: PLC0415
    from src.engines.historical_engine import _UPSTOX_INSTRUMENT_KEYS  # noqa: PLC0415

    async with engine.connect() as conn2:
        ipm_rows = (await conn2.execute(_text2("""
            SELECT
              SPLIT_PART(instrument_id, ':', 2) AS symbol,
              SPLIT_PART(provider_instrument_id, '|', 2) AS isin,
              provider_instrument_id AS upstox_key
            FROM instrument_provider_mapping
            WHERE provider = 'upstox'
              AND provider_instrument_id LIKE 'NSE_EQ|%'
              AND SPLIT_PART(provider_instrument_id,'|',2) != ''
        """))).mappings().all()

    im_lookup: dict[str, dict] = {}
    for r in ipm_rows:
        sym = r["symbol"].upper()
        im_lookup[sym] = {
            "isin": r["isin"],
            "upstox_key": r["upstox_key"],
        }

    # Also pull from static engine map (covers symbols not in instrument_provider_mapping)
    for sym, key in _UPSTOX_INSTRUMENT_KEYS.items():
        if key.startswith("NSE_EQ|") and sym.upper() not in im_lookup:
            isin = key.split("|", 1)[1]
            im_lookup[sym.upper()] = {"isin": isin, "upstox_key": key}

    await logger.ainfo("seed_fo_universe.isin_lookup_built",
                       symbols_with_isin=len(im_lookup))

    # Merge ISIN data into records
    for rec in records:
        sym = rec["symbol"].upper()
        if sym in im_lookup:
            row = im_lookup[sym]
            if not rec.get("isin") and row.get("isin"):
                rec["isin"] = row["isin"]
            if not rec.get("upstox_key") and row.get("upstox_key"):
                rec["upstox_key"] = row["upstox_key"]
            # Rebuild upstox_key from ISIN if not yet set
            if not rec.get("upstox_key") and rec.get("isin") and not rec["is_index"]:
                rec["upstox_key"] = f"NSE_EQ|{rec['isin']}"

    await logger.ainfo("seed_fo_universe.records_built", total=len(records))

    if dry_run:
        print(f"\nDRY RUN — would upsert {len(records)} records into fo_universe\n")
        # Show priority breakdown
        from collections import Counter  # noqa: PLC0415
        prio_counts = Counter(r["backfill_priority"] for r in records)
        labels = {1:"Index futures", 2:"Stock futures", 3:"Broad indices",
                  4:"Nifty 50", 5:"Other"}
        for p in sorted(prio_counts):
            print(f"  Priority {p} ({labels.get(p,'?')}): {prio_counts[p]:>3} instruments")
        print()
        # Sample 5 rows
        for r in records[:5]:
            print(f"  {r['instrument_id']:<28} prio={r['backfill_priority']}"
                  f"  isin={r['isin'] or 'N/A':<14}"
                  f"  fo_active={r['is_fo_active']}"
                  f"  sector={r['sector'] or ''}")
        await engine.dispose()
        return

    inserted = 0
    async with engine.begin() as conn:
        for r in records:
            await conn.execute(text(_UPSERT_SQL), r)
            inserted += 1

    await engine.dispose()

    # Summary
    print()
    print("=" * 64)
    print("  FO_UNIVERSE SEED COMPLETE")
    print("=" * 64)
    print(f"  Records upserted : {inserted}")

    from collections import Counter  # noqa: PLC0415
    prio_counts = Counter(r["backfill_priority"] for r in records)
    labels = {1:"Index futures", 2:"Stock futures", 3:"Broad indices",
              4:"Nifty 50", 5:"Other"}
    for p in sorted(prio_counts):
        print(f"  Priority {p} ({labels.get(p,'?')}): {prio_counts[p]:>3}")

    active  = sum(1 for r in records if r["is_fo_active"])
    retired = sum(1 for r in records if not r["is_fo_active"])
    print(f"  Currently F&O active : {active}")
    print(f"  Retired / delisted   : {retired}")
    print("=" * 64)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed fo_universe table from DB history + Angel One scrip master",
        epilog=__doc__,
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    import argparse  # noqa: PLC0415
    main()
