"""
India Instruments Master List
==============================
Curated set of NSE instruments used by the 1-year backfill script.

Each entry carries the fields needed by HistoricalEngine.run_backfill:
    symbol           : NSE trading symbol
    exchange         : "NSE" for equities/indices, "NFO" for F&O
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
    {"symbol": "NIFTY 50",        "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty 50 Index"},
    {"symbol": "NIFTY BANK",      "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Bank Index"},
    {"symbol": "NIFTY IT",        "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty IT Index"},
    {"symbol": "NIFTY MIDCAP 50", "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Midcap 50 Index"},
    {"symbol": "NIFTY FMCG",      "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty FMCG Index"},
    {"symbol": "INDIA VIX",       "exchange": "NSE", "instrument_class": "IDX", "description": "India VIX"},
    {"symbol": "NIFTY FIN SERVICE","exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Financial Services Index"},
    {"symbol": "NIFTY PHARMA",    "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Pharma Index"},
    {"symbol": "NIFTY AUTO",      "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Auto Index"},
    {"symbol": "NIFTY REALTY",    "exchange": "NSE", "instrument_class": "IDX", "description": "Nifty Realty Index"},
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
# Combined universe: indices first, then equities
# ---------------------------------------------------------------------------
ALL_INSTRUMENTS: list[InstrumentEntry] = NSE_INDICES + NIFTY50_EQUITIES
