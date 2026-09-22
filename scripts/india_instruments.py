"""
India Instruments Master List
==============================
Curated set of NSE instruments used by the backfill scripts.

Each entry carries the fields needed by HistoricalEngine.run_backfill:
    symbol           : NSE trading symbol
    exchange         : "NSE" for equities/indices, "NFO" for F&O futures
    instrument_class : "EQ" | "IDX" | "FO"
    description      : human-readable label

To add or remove instruments, edit the lists below and re-run the backfill.

Source:  NSE India — https://www.nseindia.com/
"""

from __future__ import annotations

from typing import TypedDict


class InstrumentEntry(TypedDict):
    symbol: str
    exchange: str
    instrument_class: str
    description: str


# ---------------------------------------------------------------------------
# NSE Indices
# ---------------------------------------------------------------------------
NSE_INDICES: list[InstrumentEntry] = [
    {"symbol": "NIFTY 50",         "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty 50 Index"},
    {"symbol": "NIFTY BANK",       "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Bank Index"},
    {"symbol": "NIFTY IT",         "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty IT Index"},
    {"symbol": "NIFTY MIDCAP 50",  "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Midcap 50 Index"},
    {"symbol": "NIFTY FMCG",       "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty FMCG Index"},
    {"symbol": "INDIA VIX",        "exchange": "NSE", "instrument_class": "IDX", "description": "India VIX"},
    {"symbol": "NIFTY FIN SERVICE","exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Financial Services Index"},
    {"symbol": "NIFTY PHARMA",     "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Pharma Index"},
    {"symbol": "NIFTY AUTO",       "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Auto Index"},
    {"symbol": "NIFTY REALTY",     "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Realty Index"},
]

# ---------------------------------------------------------------------------
# Nifty 50 Constituent Equities (as of Sep 2026)
# Source: https://www.nseindia.com/market-data/live-equity-market?symbol=NIFTY%2050
# ---------------------------------------------------------------------------
NIFTY50_EQUITIES: list[InstrumentEntry] = [
    # ── Financials ──────────────────────────────────────────────────────────
    {"symbol": "HDFCBANK",   "exchange": "NSE", "instrument_class": "EQ", "description": "HDFC Bank Ltd"},
    {"symbol": "ICICIBANK",  "exchange": "NSE", "instrument_class": "EQ", "description": "ICICI Bank Ltd"},
    {"symbol": "KOTAKBANK",  "exchange": "NSE", "instrument_class": "EQ", "description": "Kotak Mahindra Bank"},
    {"symbol": "AXISBANK",   "exchange": "NSE", "instrument_class": "EQ", "description": "Axis Bank Ltd"},
    {"symbol": "SBIN",       "exchange": "NSE", "instrument_class": "EQ", "description": "State Bank of India"},
    {"symbol": "BAJFINANCE", "exchange": "NSE", "instrument_class": "EQ", "description": "Bajaj Finance Ltd"},
    {"symbol": "BAJAJFINSV", "exchange": "NSE", "instrument_class": "EQ", "description": "Bajaj Finserv Ltd"},
    {"symbol": "HDFCLIFE",   "exchange": "NSE", "instrument_class": "EQ", "description": "HDFC Life Insurance"},
    {"symbol": "SBILIFE",    "exchange": "NSE", "instrument_class": "EQ", "description": "SBI Life Insurance"},
    # ── Technology ──────────────────────────────────────────────────────────
    {"symbol": "TCS",        "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Consultancy Services"},
    {"symbol": "INFY",       "exchange": "NSE", "instrument_class": "EQ", "description": "Infosys Ltd"},
    {"symbol": "WIPRO",      "exchange": "NSE", "instrument_class": "EQ", "description": "Wipro Ltd"},
    {"symbol": "HCLTECH",    "exchange": "NSE", "instrument_class": "EQ", "description": "HCL Technologies"},
    {"symbol": "TECHM",      "exchange": "NSE", "instrument_class": "EQ", "description": "Tech Mahindra Ltd"},
    {"symbol": "LTI",        "exchange": "NSE", "instrument_class": "EQ", "description": "LTIMindtree Ltd"},
    # ── Energy & Utilities ───────────────────────────────────────────────────
    {"symbol": "RELIANCE",   "exchange": "NSE", "instrument_class": "EQ", "description": "Reliance Industries Ltd"},
    {"symbol": "ONGC",       "exchange": "NSE", "instrument_class": "EQ", "description": "Oil & Natural Gas Corp"},
    {"symbol": "POWERGRID",  "exchange": "NSE", "instrument_class": "EQ", "description": "Power Grid Corporation"},
    {"symbol": "NTPC",       "exchange": "NSE", "instrument_class": "EQ", "description": "NTPC Ltd"},
    {"symbol": "BPCL",       "exchange": "NSE", "instrument_class": "EQ", "description": "Bharat Petroleum Corp"},
    {"symbol": "COALINDIA",  "exchange": "NSE", "instrument_class": "EQ", "description": "Coal India Ltd"},
    # ── Consumer Staples / FMCG ────────────────────────────────────────────
    {"symbol": "HINDUNILVR", "exchange": "NSE", "instrument_class": "EQ", "description": "Hindustan Unilever Ltd"},
    {"symbol": "ITC",        "exchange": "NSE", "instrument_class": "EQ", "description": "ITC Ltd"},
    {"symbol": "NESTLEIND",  "exchange": "NSE", "instrument_class": "EQ", "description": "Nestle India Ltd"},
    {"symbol": "BRITANNIA",  "exchange": "NSE", "instrument_class": "EQ", "description": "Britannia Industries"},
    # ── Industrials / Conglomerates ────────────────────────────────────────
    {"symbol": "LT",         "exchange": "NSE", "instrument_class": "EQ", "description": "Larsen & Toubro Ltd"},
    {"symbol": "ADANIENT",   "exchange": "NSE", "instrument_class": "EQ", "description": "Adani Enterprises Ltd"},
    {"symbol": "ADANIPORTS", "exchange": "NSE", "instrument_class": "EQ", "description": "Adani Ports & SEZ"},
    {"symbol": "TATAMOTORS", "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Motors Ltd"},
    {"symbol": "TATASTEEL",  "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Steel Ltd"},
    {"symbol": "JSWSTEEL",   "exchange": "NSE", "instrument_class": "EQ", "description": "JSW Steel Ltd"},
    {"symbol": "M&M",        "exchange": "NSE", "instrument_class": "EQ", "description": "Mahindra & Mahindra Ltd"},
    {"symbol": "MARUTI",     "exchange": "NSE", "instrument_class": "EQ", "description": "Maruti Suzuki India Ltd"},
    {"symbol": "HEROMOTOCO", "exchange": "NSE", "instrument_class": "EQ", "description": "Hero MotoCorp Ltd"},
    {"symbol": "EICHERMOT",  "exchange": "NSE", "instrument_class": "EQ", "description": "Eicher Motors Ltd"},
    # ── Healthcare / Pharma ─────────────────────────────────────────────────
    {"symbol": "SUNPHARMA",  "exchange": "NSE", "instrument_class": "EQ", "description": "Sun Pharmaceutical Industries"},
    {"symbol": "DRREDDY",    "exchange": "NSE", "instrument_class": "EQ", "description": "Dr Reddy's Laboratories"},
    {"symbol": "CIPLA",      "exchange": "NSE", "instrument_class": "EQ", "description": "Cipla Ltd"},
    {"symbol": "DIVISLAB",   "exchange": "NSE", "instrument_class": "EQ", "description": "Divi's Laboratories"},
    {"symbol": "APOLLOHOSP", "exchange": "NSE", "instrument_class": "EQ", "description": "Apollo Hospitals Enterprise"},
    # ── Telecom & Media ─────────────────────────────────────────────────────
    {"symbol": "BHARTIARTL", "exchange": "NSE", "instrument_class": "EQ", "description": "Bharti Airtel Ltd"},
    # ── Cement & Materials ──────────────────────────────────────────────────
    {"symbol": "ULTRACEMCO", "exchange": "NSE", "instrument_class": "EQ", "description": "UltraTech Cement Ltd"},
    {"symbol": "GRASIM",     "exchange": "NSE", "instrument_class": "EQ", "description": "Grasim Industries Ltd"},
    {"symbol": "SHREECEM",   "exchange": "NSE", "instrument_class": "EQ", "description": "Shree Cement Ltd"},
    # ── Mining / Metals ─────────────────────────────────────────────────────
    {"symbol": "HINDALCO",   "exchange": "NSE", "instrument_class": "EQ", "description": "Hindalco Industries"},
    # ── Asset Management / Insurance ────────────────────────────────────────
    {"symbol": "ASIANPAINT", "exchange": "NSE", "instrument_class": "EQ", "description": "Asian Paints Ltd"},
    {"symbol": "TITAN",      "exchange": "NSE", "instrument_class": "EQ", "description": "Titan Company Ltd"},
    # ── Misc ────────────────────────────────────────────────────────────────
    {"symbol": "INDUSINDBK", "exchange": "NSE", "instrument_class": "EQ", "description": "IndusInd Bank Ltd"},
    {"symbol": "WIPRO",      "exchange": "NSE", "instrument_class": "EQ", "description": "Wipro Ltd"},
]

# De-duplicate by symbol (some Nifty 50 lists overlap)
_seen: set[str] = set()
_deduped: list[InstrumentEntry] = []
for _entry in NIFTY50_EQUITIES:
    if _entry["symbol"] not in _seen:
        _seen.add(_entry["symbol"])
        _deduped.append(_entry)
NIFTY50_EQUITIES = _deduped

# ---------------------------------------------------------------------------
# NFO Futures — index futures + Nifty 50 stock futures
#
# exchange = "NFO" (NSE's Futures & Options segment).
# instrument_class = "FO".
#
# The HistoricalEngine resolves the actual expiry-specific contract token
# from instrument_provider_mapping / instrument_master at fetch time.
# Here we supply only the underlying symbol — the engine fetches the
# nearest active contract via Angel One or Upstox.
#
# Note: F&O contracts expire monthly (and weekly for indices).  The engine's
# ON CONFLICT … DO UPDATE upsert handles rolling contracts correctly because
# each contract's instrument_id encodes the expiry date.
# ---------------------------------------------------------------------------
NFO_FUTURES: list[InstrumentEntry] = [
    # ── Index futures ───────────────────────────────────────────────────────
    {"symbol": "NIFTY",      "exchange": "NFO", "instrument_class": "FO", "description": "Nifty 50 Index Futures"},
    {"symbol": "BANKNIFTY",  "exchange": "NFO", "instrument_class": "FO", "description": "Nifty Bank Index Futures"},
    {"symbol": "FINNIFTY",   "exchange": "NFO", "instrument_class": "FO", "description": "Nifty Financial Services Futures"},
    {"symbol": "MIDCPNIFTY", "exchange": "NFO", "instrument_class": "FO", "description": "Nifty Midcap Select Futures"},
    # ── Stock futures (Nifty 50 constituents with active F&O contracts) ─────
    {"symbol": "RELIANCE",   "exchange": "NFO", "instrument_class": "FO", "description": "Reliance Industries Futures"},
    {"symbol": "HDFCBANK",   "exchange": "NFO", "instrument_class": "FO", "description": "HDFC Bank Futures"},
    {"symbol": "ICICIBANK",  "exchange": "NFO", "instrument_class": "FO", "description": "ICICI Bank Futures"},
    {"symbol": "INFY",       "exchange": "NFO", "instrument_class": "FO", "description": "Infosys Futures"},
    {"symbol": "TCS",        "exchange": "NFO", "instrument_class": "FO", "description": "TCS Futures"},
    {"symbol": "KOTAKBANK",  "exchange": "NFO", "instrument_class": "FO", "description": "Kotak Mahindra Bank Futures"},
    {"symbol": "AXISBANK",   "exchange": "NFO", "instrument_class": "FO", "description": "Axis Bank Futures"},
    {"symbol": "SBIN",       "exchange": "NFO", "instrument_class": "FO", "description": "SBI Futures"},
    {"symbol": "BAJFINANCE", "exchange": "NFO", "instrument_class": "FO", "description": "Bajaj Finance Futures"},
    {"symbol": "BAJAJFINSV", "exchange": "NFO", "instrument_class": "FO", "description": "Bajaj Finserv Futures"},
    {"symbol": "HINDUNILVR", "exchange": "NFO", "instrument_class": "FO", "description": "Hindustan Unilever Futures"},
    {"symbol": "ITC",        "exchange": "NFO", "instrument_class": "FO", "description": "ITC Futures"},
    {"symbol": "LT",         "exchange": "NFO", "instrument_class": "FO", "description": "Larsen & Toubro Futures"},
    {"symbol": "WIPRO",      "exchange": "NFO", "instrument_class": "FO", "description": "Wipro Futures"},
    {"symbol": "HCLTECH",    "exchange": "NFO", "instrument_class": "FO", "description": "HCL Technologies Futures"},
    {"symbol": "SUNPHARMA",  "exchange": "NFO", "instrument_class": "FO", "description": "Sun Pharma Futures"},
    {"symbol": "TATAMOTORS", "exchange": "NFO", "instrument_class": "FO", "description": "Tata Motors Futures"},
    {"symbol": "TATASTEEL",  "exchange": "NFO", "instrument_class": "FO", "description": "Tata Steel Futures"},
    {"symbol": "MARUTI",     "exchange": "NFO", "instrument_class": "FO", "description": "Maruti Suzuki Futures"},
    {"symbol": "ADANIENT",   "exchange": "NFO", "instrument_class": "FO", "description": "Adani Enterprises Futures"},
    {"symbol": "ADANIPORTS", "exchange": "NFO", "instrument_class": "FO", "description": "Adani Ports Futures"},
    {"symbol": "NTPC",       "exchange": "NFO", "instrument_class": "FO", "description": "NTPC Futures"},
    {"symbol": "POWERGRID",  "exchange": "NFO", "instrument_class": "FO", "description": "Power Grid Futures"},
    {"symbol": "ONGC",       "exchange": "NFO", "instrument_class": "FO", "description": "ONGC Futures"},
    {"symbol": "BPCL",       "exchange": "NFO", "instrument_class": "FO", "description": "BPCL Futures"},
    {"symbol": "COALINDIA",  "exchange": "NFO", "instrument_class": "FO", "description": "Coal India Futures"},
    {"symbol": "BHARTIARTL", "exchange": "NFO", "instrument_class": "FO", "description": "Bharti Airtel Futures"},
    {"symbol": "DRREDDY",    "exchange": "NFO", "instrument_class": "FO", "description": "Dr Reddy's Futures"},
    {"symbol": "CIPLA",      "exchange": "NFO", "instrument_class": "FO", "description": "Cipla Futures"},
    {"symbol": "DIVISLAB",   "exchange": "NFO", "instrument_class": "FO", "description": "Divi's Laboratories Futures"},
    {"symbol": "TECHM",      "exchange": "NFO", "instrument_class": "FO", "description": "Tech Mahindra Futures"},
    {"symbol": "ULTRACEMCO", "exchange": "NFO", "instrument_class": "FO", "description": "UltraTech Cement Futures"},
    {"symbol": "GRASIM",     "exchange": "NFO", "instrument_class": "FO", "description": "Grasim Industries Futures"},
    {"symbol": "HINDALCO",   "exchange": "NFO", "instrument_class": "FO", "description": "Hindalco Industries Futures"},
    {"symbol": "JSWSTEEL",   "exchange": "NFO", "instrument_class": "FO", "description": "JSW Steel Futures"},
    {"symbol": "M&M",        "exchange": "NFO", "instrument_class": "FO", "description": "Mahindra & Mahindra Futures"},
    {"symbol": "HEROMOTOCO", "exchange": "NFO", "instrument_class": "FO", "description": "Hero MotoCorp Futures"},
    {"symbol": "EICHERMOT",  "exchange": "NFO", "instrument_class": "FO", "description": "Eicher Motors Futures"},
    {"symbol": "ASIANPAINT", "exchange": "NFO", "instrument_class": "FO", "description": "Asian Paints Futures"},
    {"symbol": "TITAN",      "exchange": "NFO", "instrument_class": "FO", "description": "Titan Company Futures"},
    {"symbol": "NESTLEIND",  "exchange": "NFO", "instrument_class": "FO", "description": "Nestle India Futures"},
    {"symbol": "BRITANNIA",  "exchange": "NFO", "instrument_class": "FO", "description": "Britannia Industries Futures"},
    {"symbol": "INDUSINDBK", "exchange": "NFO", "instrument_class": "FO", "description": "IndusInd Bank Futures"},
    {"symbol": "APOLLOHOSP", "exchange": "NFO", "instrument_class": "FO", "description": "Apollo Hospitals Futures"},
]

# ---------------------------------------------------------------------------
# Combined universes
# ---------------------------------------------------------------------------

# All instruments across all classes: IDX + EQ + FO
ALL_INSTRUMENTS: list[InstrumentEntry] = NSE_INDICES + NIFTY50_EQUITIES + NFO_FUTURES

# EQ + IDX only (no F&O) — used by the 1-year script
ALL_EQ_IDX: list[InstrumentEntry] = NSE_INDICES + NIFTY50_EQUITIES

# ---------------------------------------------------------------------------
# Full NSE F&O Universe — all ~250 stocks with active options/futures
# that are NOT already in NIFTY50_EQUITIES.
# Exchange = NSE, instrument_class = EQ.
# Spot (equity_candle) backfill uses Yahoo Finance as primary provider
# for 1d since Upstox keys are not pre-mapped for these symbols.
# ---------------------------------------------------------------------------
FO_UNIVERSE_EQUITIES: list[InstrumentEntry] = [
    # ── Adani Group ──────────────────────────────────────────────────────
    {"symbol": "ADANIENSOL",  "exchange": "NSE", "instrument_class": "EQ", "description": "Adani Energy Solutions"},
    {"symbol": "ADANIGREEN",  "exchange": "NSE", "instrument_class": "EQ", "description": "Adani Green Energy"},
    {"symbol": "ADANIPOWER",  "exchange": "NSE", "instrument_class": "EQ", "description": "Adani Power"},
    # ── Banks & Finance ──────────────────────────────────────────────────
    {"symbol": "360ONE",      "exchange": "NSE", "instrument_class": "EQ", "description": "360 One WAM (IIFL Wealth)"},
    {"symbol": "ABCAPITAL",   "exchange": "NSE", "instrument_class": "EQ", "description": "Aditya Birla Capital"},
    {"symbol": "AUBANK",      "exchange": "NSE", "instrument_class": "EQ", "description": "AU Small Finance Bank"},
    {"symbol": "BANDHANBNK",  "exchange": "NSE", "instrument_class": "EQ", "description": "Bandhan Bank"},
    {"symbol": "BANKBARODA",  "exchange": "NSE", "instrument_class": "EQ", "description": "Bank of Baroda"},
    {"symbol": "BANKINDIA",   "exchange": "NSE", "instrument_class": "EQ", "description": "Bank of India"},
    {"symbol": "CANFINHOME",  "exchange": "NSE", "instrument_class": "EQ", "description": "Can Fin Homes"},
    {"symbol": "CANBK",       "exchange": "NSE", "instrument_class": "EQ", "description": "Canara Bank"},
    {"symbol": "CHOLAFIN",    "exchange": "NSE", "instrument_class": "EQ", "description": "Cholamandalam Investment"},
    {"symbol": "FEDERALBNK",  "exchange": "NSE", "instrument_class": "EQ", "description": "Federal Bank"},
    {"symbol": "HDFC",        "exchange": "NSE", "instrument_class": "EQ", "description": "HDFC Ltd (merged Jul 2023)"},
    {"symbol": "IBULHSGFIN",  "exchange": "NSE", "instrument_class": "EQ", "description": "Indiabulls Housing Finance"},
    {"symbol": "IDFC",        "exchange": "NSE", "instrument_class": "EQ", "description": "IDFC Ltd"},
    {"symbol": "IDFCFIRSTB",  "exchange": "NSE", "instrument_class": "EQ", "description": "IDFC First Bank"},
    {"symbol": "INDIANB",     "exchange": "NSE", "instrument_class": "EQ", "description": "Indian Bank"},
    {"symbol": "JIOFIN",      "exchange": "NSE", "instrument_class": "EQ", "description": "Jio Financial Services"},
    {"symbol": "KALYANKJIL",  "exchange": "NSE", "instrument_class": "EQ", "description": "Kalyan Jewellers"},
    {"symbol": "L&TFH",       "exchange": "NSE", "instrument_class": "EQ", "description": "L&T Finance Holdings"},
    {"symbol": "LICHSGFIN",   "exchange": "NSE", "instrument_class": "EQ", "description": "LIC Housing Finance"},
    {"symbol": "LICI",        "exchange": "NSE", "instrument_class": "EQ", "description": "LIC of India"},
    {"symbol": "LTF",         "exchange": "NSE", "instrument_class": "EQ", "description": "L&T Finance"},
    {"symbol": "M&MFIN",      "exchange": "NSE", "instrument_class": "EQ", "description": "Mahindra & Mahindra Financial"},
    {"symbol": "MANAPPURAM",  "exchange": "NSE", "instrument_class": "EQ", "description": "Manappuram Finance"},
    {"symbol": "MFSL",        "exchange": "NSE", "instrument_class": "EQ", "description": "Max Financial Services"},
    {"symbol": "MUTHOOTFIN",  "exchange": "NSE", "instrument_class": "EQ", "description": "Muthoot Finance"},
    {"symbol": "PNB",         "exchange": "NSE", "instrument_class": "EQ", "description": "Punjab National Bank"},
    {"symbol": "PNBHOUSING",  "exchange": "NSE", "instrument_class": "EQ", "description": "PNB Housing Finance"},
    {"symbol": "RBLBANK",     "exchange": "NSE", "instrument_class": "EQ", "description": "RBL Bank"},
    {"symbol": "SAIL",        "exchange": "NSE", "instrument_class": "EQ", "description": "Steel Authority of India"},
    {"symbol": "SBICARD",     "exchange": "NSE", "instrument_class": "EQ", "description": "SBI Cards and Payment Services"},
    {"symbol": "SHRIRAMFIN",  "exchange": "NSE", "instrument_class": "EQ", "description": "Shriram Finance"},
    {"symbol": "UNIONBANK",   "exchange": "NSE", "instrument_class": "EQ", "description": "Union Bank of India"},
    {"symbol": "YESBANK",     "exchange": "NSE", "instrument_class": "EQ", "description": "Yes Bank"},
    # ── IT / Technology ──────────────────────────────────────────────────
    {"symbol": "COFORGE",     "exchange": "NSE", "instrument_class": "EQ", "description": "Coforge"},
    {"symbol": "CYIENT",      "exchange": "NSE", "instrument_class": "EQ", "description": "Cyient"},
    {"symbol": "KPITTECH",    "exchange": "NSE", "instrument_class": "EQ", "description": "KPIT Technologies"},
    {"symbol": "LTIM",        "exchange": "NSE", "instrument_class": "EQ", "description": "LTIMindtree"},
    {"symbol": "LTTS",        "exchange": "NSE", "instrument_class": "EQ", "description": "L&T Technology Services"},
    {"symbol": "MINDTREE",    "exchange": "NSE", "instrument_class": "EQ", "description": "Mindtree (merged Nov 2022)"},
    {"symbol": "MPHASIS",     "exchange": "NSE", "instrument_class": "EQ", "description": "Mphasis"},
    {"symbol": "NAUKRI",      "exchange": "NSE", "instrument_class": "EQ", "description": "Info Edge (Naukri)"},
    {"symbol": "OFSS",        "exchange": "NSE", "instrument_class": "EQ", "description": "Oracle Financial Services"},
    {"symbol": "PERSISTENT",  "exchange": "NSE", "instrument_class": "EQ", "description": "Persistent Systems"},
    # ── Pharma / Healthcare ──────────────────────────────────────────────
    {"symbol": "AARTIIND",    "exchange": "NSE", "instrument_class": "EQ", "description": "Aarti Industries"},
    {"symbol": "ABBOTINDIA",  "exchange": "NSE", "instrument_class": "EQ", "description": "Abbott India"},
    {"symbol": "ALKEM",       "exchange": "NSE", "instrument_class": "EQ", "description": "Alkem Laboratories"},
    {"symbol": "AUROPHARMA",  "exchange": "NSE", "instrument_class": "EQ", "description": "Aurobindo Pharma"},
    {"symbol": "BIOCON",      "exchange": "NSE", "instrument_class": "EQ", "description": "Biocon"},
    {"symbol": "FORTIS",      "exchange": "NSE", "instrument_class": "EQ", "description": "Fortis Healthcare"},
    {"symbol": "GLENMARK",    "exchange": "NSE", "instrument_class": "EQ", "description": "Glenmark Pharmaceuticals"},
    {"symbol": "GRANULES",    "exchange": "NSE", "instrument_class": "EQ", "description": "Granules India"},
    {"symbol": "IPCALAB",     "exchange": "NSE", "instrument_class": "EQ", "description": "Ipca Laboratories"},
    {"symbol": "LAURUSLABS",  "exchange": "NSE", "instrument_class": "EQ", "description": "Laurus Labs"},
    {"symbol": "LUPIN",       "exchange": "NSE", "instrument_class": "EQ", "description": "Lupin"},
    {"symbol": "MANKIND",     "exchange": "NSE", "instrument_class": "EQ", "description": "Mankind Pharma"},
    {"symbol": "MAXHEALTH",   "exchange": "NSE", "instrument_class": "EQ", "description": "Max Healthcare Institute"},
    {"symbol": "METROPOLIS",  "exchange": "NSE", "instrument_class": "EQ", "description": "Metropolis Healthcare"},
    {"symbol": "NAVINFLUOR",  "exchange": "NSE", "instrument_class": "EQ", "description": "Navin Fluorine International"},
    {"symbol": "PFIZER",      "exchange": "NSE", "instrument_class": "EQ", "description": "Pfizer"},
    {"symbol": "SYNGENE",     "exchange": "NSE", "instrument_class": "EQ", "description": "Syngene International"},
    {"symbol": "TORNTPHARM",  "exchange": "NSE", "instrument_class": "EQ", "description": "Torrent Pharmaceuticals"},
    {"symbol": "ZYDUSLIFE",   "exchange": "NSE", "instrument_class": "EQ", "description": "Zydus Lifesciences"},
    # ── Industrials / Capital Goods ──────────────────────────────────────
    {"symbol": "ABB",         "exchange": "NSE", "instrument_class": "EQ", "description": "ABB India"},
    {"symbol": "BEL",         "exchange": "NSE", "instrument_class": "EQ", "description": "Bharat Electronics"},
    {"symbol": "BHEL",        "exchange": "NSE", "instrument_class": "EQ", "description": "Bharat Heavy Electricals"},
    {"symbol": "BDL",         "exchange": "NSE", "instrument_class": "EQ", "description": "Bharat Dynamics"},
    {"symbol": "CGPOWER",     "exchange": "NSE", "instrument_class": "EQ", "description": "CG Power and Industrial Solutions"},
    {"symbol": "CONCOR",      "exchange": "NSE", "instrument_class": "EQ", "description": "Container Corp of India"},
    {"symbol": "CUMMINSIND",  "exchange": "NSE", "instrument_class": "EQ", "description": "Cummins India"},
    {"symbol": "HAL",         "exchange": "NSE", "instrument_class": "EQ", "description": "Hindustan Aeronautics"},
    {"symbol": "HAVELLS",     "exchange": "NSE", "instrument_class": "EQ", "description": "Havells India"},
    {"symbol": "MAZDOCK",     "exchange": "NSE", "instrument_class": "EQ", "description": "Mazagon Dock Shipbuilders"},
    {"symbol": "POLYCAB",     "exchange": "NSE", "instrument_class": "EQ", "description": "Polycab India"},
    {"symbol": "SIEMENS",     "exchange": "NSE", "instrument_class": "EQ", "description": "Siemens India"},
    {"symbol": "TIINDIA",     "exchange": "NSE", "instrument_class": "EQ", "description": "Tube Investments of India"},
    {"symbol": "TVSMOTOR",    "exchange": "NSE", "instrument_class": "EQ", "description": "TVS Motor Company"},
    # ── Energy / Oil & Gas ───────────────────────────────────────────────
    {"symbol": "GAIL",        "exchange": "NSE", "instrument_class": "EQ", "description": "GAIL India"},
    {"symbol": "HINDPETRO",   "exchange": "NSE", "instrument_class": "EQ", "description": "Hindustan Petroleum Corp"},
    {"symbol": "IOC",         "exchange": "NSE", "instrument_class": "EQ", "description": "Indian Oil Corporation"},
    {"symbol": "OIL",         "exchange": "NSE", "instrument_class": "EQ", "description": "Oil India"},
    {"symbol": "PETRONET",    "exchange": "NSE", "instrument_class": "EQ", "description": "Petronet LNG"},
    {"symbol": "TATAPOWER",   "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Power Company"},
    {"symbol": "TORNTPOWER",  "exchange": "NSE", "instrument_class": "EQ", "description": "Torrent Power"},
    # ── Chemicals ────────────────────────────────────────────────────────
    {"symbol": "DEEPAKNTR",   "exchange": "NSE", "instrument_class": "EQ", "description": "Deepak Nitrite"},
    {"symbol": "GNFC",        "exchange": "NSE", "instrument_class": "EQ", "description": "Gujarat Narmada Valley Fertilizers"},
    {"symbol": "PIIND",       "exchange": "NSE", "instrument_class": "EQ", "description": "PI Industries"},
    {"symbol": "SRF",         "exchange": "NSE", "instrument_class": "EQ", "description": "SRF"},
    {"symbol": "TATACHEM",    "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Chemicals"},
    # ── Consumer / FMCG ──────────────────────────────────────────────────
    {"symbol": "COLPAL",      "exchange": "NSE", "instrument_class": "EQ", "description": "Colgate-Palmolive India"},
    {"symbol": "DABUR",       "exchange": "NSE", "instrument_class": "EQ", "description": "Dabur India"},
    {"symbol": "GODREJCP",    "exchange": "NSE", "instrument_class": "EQ", "description": "Godrej Consumer Products"},
    {"symbol": "MARICO",      "exchange": "NSE", "instrument_class": "EQ", "description": "Marico"},
    {"symbol": "MCDOWELL-N",  "exchange": "NSE", "instrument_class": "EQ", "description": "United Spirits (McDowell)"},
    {"symbol": "PAGEIND",     "exchange": "NSE", "instrument_class": "EQ", "description": "Page Industries"},
    {"symbol": "PIDILITIND",  "exchange": "NSE", "instrument_class": "EQ", "description": "Pidilite Industries"},
    {"symbol": "UBL",         "exchange": "NSE", "instrument_class": "EQ", "description": "United Breweries"},
    {"symbol": "TATACONSUM",  "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Consumer Products"},
    # ── Auto / Auto Ancillary ────────────────────────────────────────────
    {"symbol": "AMARAJABAT",  "exchange": "NSE", "instrument_class": "EQ", "description": "Amara Raja Energy & Mobility"},
    {"symbol": "APOLLOTYRE",  "exchange": "NSE", "instrument_class": "EQ", "description": "Apollo Tyres"},
    {"symbol": "ASHOKLEY",    "exchange": "NSE", "instrument_class": "EQ", "description": "Ashok Leyland"},
    {"symbol": "BAJAJ-AUTO",  "exchange": "NSE", "instrument_class": "EQ", "description": "Bajaj Auto"},
    {"symbol": "BALKRISIND",  "exchange": "NSE", "instrument_class": "EQ", "description": "Balkrishna Industries"},
    {"symbol": "ESCORTS",     "exchange": "NSE", "instrument_class": "EQ", "description": "Escorts Kubota"},
    {"symbol": "EXIDEIND",    "exchange": "NSE", "instrument_class": "EQ", "description": "Exide Industries"},
    {"symbol": "FORCEMOT",    "exchange": "NSE", "instrument_class": "EQ", "description": "Force Motors"},
    {"symbol": "HEROMOTOCO",  "exchange": "NSE", "instrument_class": "EQ", "description": "Hero MotoCorp"},
    {"symbol": "HYUNDAI",     "exchange": "NSE", "instrument_class": "EQ", "description": "Hyundai Motor India"},
    {"symbol": "MOTHERSON",   "exchange": "NSE", "instrument_class": "EQ", "description": "Samvardhana Motherson International"},
    {"symbol": "MRF",         "exchange": "NSE", "instrument_class": "EQ", "description": "MRF"},
    # ── Metals & Mining ──────────────────────────────────────────────────
    {"symbol": "HINDCOPPER",  "exchange": "NSE", "instrument_class": "EQ", "description": "Hindustan Copper"},
    {"symbol": "HINDZINC",    "exchange": "NSE", "instrument_class": "EQ", "description": "Hindustan Zinc"},
    {"symbol": "JINDALSTEL",  "exchange": "NSE", "instrument_class": "EQ", "description": "Jindal Steel & Power"},
    {"symbol": "NATIONALUM",  "exchange": "NSE", "instrument_class": "EQ", "description": "National Aluminium Company"},
    {"symbol": "NMDC",        "exchange": "NSE", "instrument_class": "EQ", "description": "NMDC"},
    {"symbol": "VEDL",        "exchange": "NSE", "instrument_class": "EQ", "description": "Vedanta"},
    # ── Real Estate ──────────────────────────────────────────────────────
    {"symbol": "DLF",         "exchange": "NSE", "instrument_class": "EQ", "description": "DLF"},
    {"symbol": "GODREJPROP",  "exchange": "NSE", "instrument_class": "EQ", "description": "Godrej Properties"},
    {"symbol": "OBEROIRLTY",  "exchange": "NSE", "instrument_class": "EQ", "description": "Oberoi Realty"},
    {"symbol": "LODHA",       "exchange": "NSE", "instrument_class": "EQ", "description": "Macrotech Developers (Lodha)"},
    {"symbol": "PRESTIGE",    "exchange": "NSE", "instrument_class": "EQ", "description": "Prestige Estates Projects"},
    # ── Infrastructure / Logistics ───────────────────────────────────────
    {"symbol": "GMRAIRPORT",  "exchange": "NSE", "instrument_class": "EQ", "description": "GMR Airports Infrastructure"},
    {"symbol": "GMRINFRA",    "exchange": "NSE", "instrument_class": "EQ", "description": "GMR Infrastructure"},
    {"symbol": "INDIGO",      "exchange": "NSE", "instrument_class": "EQ", "description": "InterGlobe Aviation (IndiGo)"},
    {"symbol": "IRCTC",       "exchange": "NSE", "instrument_class": "EQ", "description": "Indian Railway Catering & Tourism"},
    # ── PSU / Government ─────────────────────────────────────────────────
    {"symbol": "HUDCO",       "exchange": "NSE", "instrument_class": "EQ", "description": "Housing & Urban Development Corp"},
    {"symbol": "IREDA",       "exchange": "NSE", "instrument_class": "EQ", "description": "Indian Renewable Energy Dev Agency"},
    {"symbol": "IRFC",        "exchange": "NSE", "instrument_class": "EQ", "description": "Indian Railway Finance Corp"},
    {"symbol": "NBCC",        "exchange": "NSE", "instrument_class": "EQ", "description": "NBCC India"},
    {"symbol": "NHPC",        "exchange": "NSE", "instrument_class": "EQ", "description": "NHPC"},
    {"symbol": "PFC",         "exchange": "NSE", "instrument_class": "EQ", "description": "Power Finance Corporation"},
    {"symbol": "RECLTD",      "exchange": "NSE", "instrument_class": "EQ", "description": "REC"},
    {"symbol": "RVNL",        "exchange": "NSE", "instrument_class": "EQ", "description": "Rail Vikas Nigam"},
    {"symbol": "SJVN",        "exchange": "NSE", "instrument_class": "EQ", "description": "SJVN"},
    # ── Cement & Building Materials ──────────────────────────────────────
    {"symbol": "ACC",         "exchange": "NSE", "instrument_class": "EQ", "description": "ACC"},
    {"symbol": "AMBUJACEM",   "exchange": "NSE", "instrument_class": "EQ", "description": "Ambuja Cements"},
    {"symbol": "DALBHARAT",   "exchange": "NSE", "instrument_class": "EQ", "description": "Dalmia Bharat"},
    {"symbol": "JKCEMENT",    "exchange": "NSE", "instrument_class": "EQ", "description": "JK Cement"},
    {"symbol": "RAMCOCEM",    "exchange": "NSE", "instrument_class": "EQ", "description": "Ramco Cements"},
    {"symbol": "ASTRAL",      "exchange": "NSE", "instrument_class": "EQ", "description": "Astral"},
    # ── Retail / Consumer Discretionary ─────────────────────────────────
    {"symbol": "ABFRL",       "exchange": "NSE", "instrument_class": "EQ", "description": "Aditya Birla Fashion & Retail"},
    {"symbol": "BATAINDIA",   "exchange": "NSE", "instrument_class": "EQ", "description": "Bata India"},
    {"symbol": "DMART",       "exchange": "NSE", "instrument_class": "EQ", "description": "Avenue Supermarts (DMart)"},
    {"symbol": "JUBLFOOD",    "exchange": "NSE", "instrument_class": "EQ", "description": "Jubilant FoodWorks"},
    {"symbol": "PVR",         "exchange": "NSE", "instrument_class": "EQ", "description": "PVR (merged May 2023)"},
    {"symbol": "PVRINOX",     "exchange": "NSE", "instrument_class": "EQ", "description": "PVR INOX"},
    {"symbol": "TRENT",       "exchange": "NSE", "instrument_class": "EQ", "description": "Trent"},
    # ── Insurance & Asset Management ─────────────────────────────────────
    {"symbol": "ANGELONE",    "exchange": "NSE", "instrument_class": "EQ", "description": "Angel One"},
    {"symbol": "BSE",         "exchange": "NSE", "instrument_class": "EQ", "description": "BSE"},
    {"symbol": "CAMS",        "exchange": "NSE", "instrument_class": "EQ", "description": "CAMS"},
    {"symbol": "CDSL",        "exchange": "NSE", "instrument_class": "EQ", "description": "Central Depository Services"},
    {"symbol": "HDFCAMC",     "exchange": "NSE", "instrument_class": "EQ", "description": "HDFC AMC"},
    {"symbol": "ICICIGI",     "exchange": "NSE", "instrument_class": "EQ", "description": "ICICI Lombard General Insurance"},
    {"symbol": "ICICIPRULI",  "exchange": "NSE", "instrument_class": "EQ", "description": "ICICI Prudential Life Insurance"},
    {"symbol": "IEX",         "exchange": "NSE", "instrument_class": "EQ", "description": "Indian Energy Exchange"},
    {"symbol": "MCX",         "exchange": "NSE", "instrument_class": "EQ", "description": "Multi Commodity Exchange"},
    {"symbol": "MOTILALOFS",  "exchange": "NSE", "instrument_class": "EQ", "description": "Motilal Oswal Financial Services"},
    {"symbol": "NAM-INDIA",   "exchange": "NSE", "instrument_class": "EQ", "description": "Nippon India Mutual Fund"},
    {"symbol": "POLICYBZR",   "exchange": "NSE", "instrument_class": "EQ", "description": "PB Fintech (PolicyBazaar)"},
    # ── Telecom & Media ──────────────────────────────────────────────────
    {"symbol": "IDEA",        "exchange": "NSE", "instrument_class": "EQ", "description": "Vodafone Idea"},
    {"symbol": "INDUSTOWER",  "exchange": "NSE", "instrument_class": "EQ", "description": "Indus Towers"},
    {"symbol": "SUNTV",       "exchange": "NSE", "instrument_class": "EQ", "description": "Sun TV Network"},
    {"symbol": "TATACOMM",    "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Communications"},
    # ── Speciality / Others ──────────────────────────────────────────────
    {"symbol": "AMBER",       "exchange": "NSE", "instrument_class": "EQ", "description": "Amber Enterprises India"},
    {"symbol": "APLAPOLLO",   "exchange": "NSE", "instrument_class": "EQ", "description": "APL Apollo Tubes"},
    {"symbol": "ATGL",        "exchange": "NSE", "instrument_class": "EQ", "description": "Adani Total Gas"},
    {"symbol": "ATHERENERG",  "exchange": "NSE", "instrument_class": "EQ", "description": "Ather Energy"},
    {"symbol": "BAJAJHLDNG",  "exchange": "NSE", "instrument_class": "EQ", "description": "Bajaj Holdings & Investment"},
    {"symbol": "BERGEPAINT",  "exchange": "NSE", "instrument_class": "EQ", "description": "Berger Paints India"},
    {"symbol": "BHARATFORG",  "exchange": "NSE", "instrument_class": "EQ", "description": "Bharat Forge"},
    {"symbol": "BLUESTARCO",  "exchange": "NSE", "instrument_class": "EQ", "description": "Blue Star"},
    {"symbol": "BOSCHLTD",    "exchange": "NSE", "instrument_class": "EQ", "description": "Bosch"},
    {"symbol": "BSOFT",       "exchange": "NSE", "instrument_class": "EQ", "description": "BSOFT (Birlasoft)"},
    {"symbol": "CADILAHC",    "exchange": "NSE", "instrument_class": "EQ", "description": "Cadila Healthcare (now Zydus)"},
    {"symbol": "CANFINHOME",  "exchange": "NSE", "instrument_class": "EQ", "description": "Can Fin Homes"},
    {"symbol": "CESC",        "exchange": "NSE", "instrument_class": "EQ", "description": "CESC"},
    {"symbol": "CHAMBLFERT",  "exchange": "NSE", "instrument_class": "EQ", "description": "Chambal Fertilisers"},
    {"symbol": "COCHINSHIP",  "exchange": "NSE", "instrument_class": "EQ", "description": "Cochin Shipyard"},
    {"symbol": "COROMANDEL",  "exchange": "NSE", "instrument_class": "EQ", "description": "Coromandel International"},
    {"symbol": "CROMPTON",    "exchange": "NSE", "instrument_class": "EQ", "description": "Crompton Greaves Consumer"},
    {"symbol": "CUB",         "exchange": "NSE", "instrument_class": "EQ", "description": "City Union Bank"},
    {"symbol": "DELHIVERY",   "exchange": "NSE", "instrument_class": "EQ", "description": "Delhivery"},
    {"symbol": "DELTACORP",   "exchange": "NSE", "instrument_class": "EQ", "description": "Delta Corp"},
    {"symbol": "DIXON",       "exchange": "NSE", "instrument_class": "EQ", "description": "Dixon Technologies"},
    {"symbol": "ETERNAL",     "exchange": "NSE", "instrument_class": "EQ", "description": "Eternal (Zomato parent)"},
    {"symbol": "FSL",         "exchange": "NSE", "instrument_class": "EQ", "description": "Firstsource Solutions"},
    {"symbol": "GUJGASLTD",   "exchange": "NSE", "instrument_class": "EQ", "description": "Gujarat Gas"},
    {"symbol": "GSPL",        "exchange": "NSE", "instrument_class": "EQ", "description": "Gujarat State Petronet"},
    {"symbol": "HFCL",        "exchange": "NSE", "instrument_class": "EQ", "description": "HFCL"},
    {"symbol": "HONAUT",      "exchange": "NSE", "instrument_class": "EQ", "description": "Honeywell Automation India"},
    {"symbol": "IGL",         "exchange": "NSE", "instrument_class": "EQ", "description": "Indraprastha Gas"},
    {"symbol": "IIFL",        "exchange": "NSE", "instrument_class": "EQ", "description": "IIFL Finance"},
    {"symbol": "INDHOTEL",    "exchange": "NSE", "instrument_class": "EQ", "description": "Indian Hotels (Taj)"},
    {"symbol": "INDIACEM",    "exchange": "NSE", "instrument_class": "EQ", "description": "India Cements"},
    {"symbol": "INDIAMART",   "exchange": "NSE", "instrument_class": "EQ", "description": "IndiaMART InterMESH"},
    {"symbol": "INOXWIND",    "exchange": "NSE", "instrument_class": "EQ", "description": "Inox Wind"},
    {"symbol": "INTELLECT",   "exchange": "NSE", "instrument_class": "EQ", "description": "Intellect Design Arena"},
    {"symbol": "IRB",         "exchange": "NSE", "instrument_class": "EQ", "description": "IRB Infrastructure"},
    {"symbol": "JSWENERGY",   "exchange": "NSE", "instrument_class": "EQ", "description": "JSW Energy"},
    {"symbol": "JSL",         "exchange": "NSE", "instrument_class": "EQ", "description": "Jindal Stainless"},
    {"symbol": "KAYNES",      "exchange": "NSE", "instrument_class": "EQ", "description": "Kaynes Technology India"},
    {"symbol": "KEI",         "exchange": "NSE", "instrument_class": "EQ", "description": "KEI Industries"},
    {"symbol": "KFINTECH",    "exchange": "NSE", "instrument_class": "EQ", "description": "KFin Technologies"},
    {"symbol": "LALPATHLAB",  "exchange": "NSE", "instrument_class": "EQ", "description": "Dr Lal PathLabs"},
    {"symbol": "MAHABANK",    "exchange": "NSE", "instrument_class": "EQ", "description": "Bank of Maharashtra"},
    {"symbol": "MGL",         "exchange": "NSE", "instrument_class": "EQ", "description": "Mahanagar Gas"},
    {"symbol": "MOTHERSUMI",  "exchange": "NSE", "instrument_class": "EQ", "description": "Motherson Sumi (pre-merger)"},
    {"symbol": "NCC",         "exchange": "NSE", "instrument_class": "EQ", "description": "NCC"},
    {"symbol": "NYKAA",       "exchange": "NSE", "instrument_class": "EQ", "description": "FSN E-Commerce Ventures (Nykaa)"},
    {"symbol": "PATANJALI",   "exchange": "NSE", "instrument_class": "EQ", "description": "Patanjali Foods"},
    {"symbol": "PAYTM",       "exchange": "NSE", "instrument_class": "EQ", "description": "One 97 Communications (Paytm)"},
    {"symbol": "PEL",         "exchange": "NSE", "instrument_class": "EQ", "description": "Piramal Enterprises"},
    {"symbol": "PHOENIXLTD",  "exchange": "NSE", "instrument_class": "EQ", "description": "Phoenix Mills"},
    {"symbol": "POONAWALLA",  "exchange": "NSE", "instrument_class": "EQ", "description": "Poonawalla Fincorp"},
    {"symbol": "RADICO",      "exchange": "NSE", "instrument_class": "EQ", "description": "Radico Khaitan"},
    {"symbol": "RAIN",        "exchange": "NSE", "instrument_class": "EQ", "description": "Rain Industries"},
    {"symbol": "RBLBANK",     "exchange": "NSE", "instrument_class": "EQ", "description": "RBL Bank"},
    {"symbol": "SBICARD",     "exchange": "NSE", "instrument_class": "EQ", "description": "SBI Cards"},
    {"symbol": "SOLARINDS",   "exchange": "NSE", "instrument_class": "EQ", "description": "Solar Industries India"},
    {"symbol": "SONACOMS",    "exchange": "NSE", "instrument_class": "EQ", "description": "Sona BLW Precision Forgings"},
    {"symbol": "SRTRANSFIN",  "exchange": "NSE", "instrument_class": "EQ", "description": "Shriram Transport Finance (old)"},
    {"symbol": "STAR",        "exchange": "NSE", "instrument_class": "EQ", "description": "Star Health Insurance"},
    {"symbol": "SUPREMEIND",  "exchange": "NSE", "instrument_class": "EQ", "description": "Supreme Industries"},
    {"symbol": "SUZLON",      "exchange": "NSE", "instrument_class": "EQ", "description": "Suzlon Energy"},
    {"symbol": "SWIGGY",      "exchange": "NSE", "instrument_class": "EQ", "description": "Bundl Technologies (Swiggy)"},
    {"symbol": "TATACHEM",    "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Chemicals"},
    {"symbol": "TATAELXSI",   "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Elxsi"},
    {"symbol": "TATATECH",    "exchange": "NSE", "instrument_class": "EQ", "description": "Tata Technologies"},
    {"symbol": "TORNTPOWER",  "exchange": "NSE", "instrument_class": "EQ", "description": "Torrent Power"},
    {"symbol": "UPL",         "exchange": "NSE", "instrument_class": "EQ", "description": "UPL"},
    {"symbol": "VOLTAS",      "exchange": "NSE", "instrument_class": "EQ", "description": "Voltas"},
    {"symbol": "WHIRLPOOL",   "exchange": "NSE", "instrument_class": "EQ", "description": "Whirlpool of India"},
    {"symbol": "ZEEL",        "exchange": "NSE", "instrument_class": "EQ", "description": "Zee Entertainment Enterprises"},
    {"symbol": "ZOMATO",      "exchange": "NSE", "instrument_class": "EQ", "description": "Zomato"},
    # ── Additional indices that have options ──────────────────────────────
    {"symbol": "NIFTYNXT50",  "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Next 50 Index"},
    {"symbol": "MIDCPNIFTY",  "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Midcap Select Index"},
]

# De-duplicate FO_UNIVERSE_EQUITIES
_fo_seen: set[str] = set()
_fo_deduped: list[InstrumentEntry] = []
for _entry in FO_UNIVERSE_EQUITIES:
    _key = (_entry["symbol"], _entry["exchange"])
    if _key not in _fo_seen:
        _fo_seen.add(_key)
        _fo_deduped.append(_entry)
FO_UNIVERSE_EQUITIES = _fo_deduped

# ---------------------------------------------------------------------------
# Complete universe for ALL Indian Market spot + FO backfill
# ---------------------------------------------------------------------------
ALL_INSTRUMENTS: list[InstrumentEntry] = (
    NSE_INDICES + NIFTY50_EQUITIES + NFO_FUTURES + FO_UNIVERSE_EQUITIES
)

# EQ + IDX only (no NFO futures) — used by the 1-year script
ALL_EQ_IDX: list[InstrumentEntry] = NSE_INDICES + NIFTY50_EQUITIES

# Spot-only universe: all EQ + IDX (Nifty50 + full F&O universe)
ALL_SPOT: list[InstrumentEntry] = (
    NSE_INDICES + NIFTY50_EQUITIES + FO_UNIVERSE_EQUITIES
)