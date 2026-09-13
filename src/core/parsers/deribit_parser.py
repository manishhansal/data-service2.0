"""
Deribit Instrument Name Parser — Task 5.8

Parses and serialises Deribit option instrument names of the form:
    {CURRENCY}-{DD}{MON}{YY}-{STRIKE}-{C|P}

Examples:
    "BTC-27JAN23-20000-C"  →  baseCurrency="BTC", optionType="CE", strike=20000.0
    "ETH-03JUN24-2500-P"   →  baseCurrency="ETH", optionType="PE", strike=2500.0

Public API
----------
parse(instrument_name)        → Optional[dict]  — None on failure (with warning log)
serialise(canonical)          → str             — raises ValueError on invalid input
verify_round_trip(name)       → bool            — True iff round-trip holds

Round-trip property (Requirements 4.11, 17.8):
    parse(serialise(parse(raw))) == parse(raw)  for all valid inputs

Requirements: 14.3, 14.7
"""

from __future__ import annotations

import calendar
import datetime
import re
from typing import Optional

from src.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_CURRENCIES: frozenset[str] = frozenset({"BTC", "ETH", "SOL"})

# Month abbreviation maps (uppercase 3-letter)
_MONTH_STR_TO_NUM: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MONTH_NUM_TO_STR: dict[int, str] = {v: k for k, v in _MONTH_STR_TO_NUM.items()}

# Regex for the Deribit instrument name format
# {CURRENCY}-{DD}{MON}{YY}-{STRIKE}-{C|P}
_DERIBIT_PATTERN: re.Pattern[str] = re.compile(
    r"^([A-Z]{2,6})"            # group 1: currency (2–6 uppercase letters)
    r"-"
    r"(\d{2})"                  # group 2: day (2 digits)
    r"([A-Z]{3})"               # group 3: month abbreviation
    r"(\d{2})"                  # group 4: year (2 digits, YY)
    r"-"
    r"(\d+(?:\.\d+)?)"          # group 5: strike (integer or decimal)
    r"-"
    r"([CP])$"                  # group 6: option type (C or P)
)

# Expiry time: 08:00 UTC on expiry date (Deribit convention)
_EXPIRY_HOUR_UTC: int = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expiry_ts(day: int, month: int, year_2digit: int) -> int:
    """Convert a Deribit 2-digit year, month, day to UTC epoch ms at 08:00 UTC."""
    # Deribit uses 2-digit years relative to 2000
    full_year = 2000 + year_2digit
    dt = datetime.datetime(full_year, month, day, _EXPIRY_HOUR_UTC, 0, 0,
                           tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _expiry_ts_to_parts(expiry_ts: int) -> tuple[int, int, int]:
    """Convert UTC epoch ms back to (day, month, full_year) at 08:00 UTC."""
    dt = datetime.datetime.fromtimestamp(expiry_ts / 1000, tz=datetime.timezone.utc)
    return dt.day, dt.month, dt.year


def _format_strike(strike: float) -> str:
    """Format strike: drop trailing .0 for integer strikes, keep decimals otherwise."""
    if strike == int(strike):
        return str(int(strike))
    return str(strike)


# ---------------------------------------------------------------------------
# Public: parse
# ---------------------------------------------------------------------------


def parse(instrument_name: str) -> Optional[dict]:
    """Parse a Deribit instrument name into canonical fields.

    Parameters
    ----------
    instrument_name : e.g. "BTC-27JAN23-20000-C"

    Returns
    -------
    dict with keys:
        baseCurrency : str   — e.g. "BTC"
        expiryTs     : int   — UTC epoch ms at 08:00 UTC on expiry date
        strike       : float — option strike price
        optionType   : str   — "CE" (call) or "PE" (put)

    Returns ``None`` if parsing fails; logs a warning.

    Requirements: 14.3
    """
    if not isinstance(instrument_name, str):
        logger.warning(
            "deribit_parser_invalid_type",
            component="deribit_parser",
            instrument_name=repr(instrument_name),
            reason="not_a_string",
        )
        return None

    match = _DERIBIT_PATTERN.match(instrument_name.strip())
    if not match:
        logger.warning(
            "deribit_parser_parse_failed",
            component="deribit_parser",
            instrument_name=instrument_name,
            reason="pattern_mismatch",
        )
        return None

    currency = match.group(1)
    day_str = match.group(2)
    month_str = match.group(3)
    year_str = match.group(4)
    strike_str = match.group(5)
    option_type_raw = match.group(6)

    # Validate month abbreviation
    if month_str not in _MONTH_STR_TO_NUM:
        logger.warning(
            "deribit_parser_parse_failed",
            component="deribit_parser",
            instrument_name=instrument_name,
            reason=f"unknown_month_abbreviation:{month_str}",
        )
        return None

    month_num = _MONTH_STR_TO_NUM[month_str]

    try:
        day = int(day_str)
        year_2digit = int(year_str)
        strike = float(strike_str)
    except ValueError as exc:
        logger.warning(
            "deribit_parser_parse_failed",
            component="deribit_parser",
            instrument_name=instrument_name,
            reason=str(exc),
        )
        return None

    # Validate calendar date
    full_year = 2000 + year_2digit
    try:
        datetime.date(full_year, month_num, day)
    except ValueError as exc:
        logger.warning(
            "deribit_parser_invalid_date",
            component="deribit_parser",
            instrument_name=instrument_name,
            reason=str(exc),
        )
        return None

    # Map option type: C → CE, P → PE
    option_type = "CE" if option_type_raw == "C" else "PE"

    expiry_ts = _expiry_ts(day, month_num, year_2digit)

    return {
        "baseCurrency": currency,
        "expiryTs": expiry_ts,
        "strike": strike,
        "optionType": option_type,
    }


# ---------------------------------------------------------------------------
# Public: serialise
# ---------------------------------------------------------------------------


def serialise(canonical: dict) -> str:
    """Serialise a canonical Deribit instrument dict back to its name string.

    Parameters
    ----------
    canonical : dict with keys ``baseCurrency``, ``expiryTs``, ``strike``,
                ``optionType`` (CE/PE).

    Returns
    -------
    Instrument name string, e.g. ``"BTC-27JAN23-20000-C"``

    Raises
    ------
    ValueError
        If any required field is missing, invalid, or ``optionType`` is not
        one of "CE" / "PE".

    Requirements: 14.7
    """
    required = ("baseCurrency", "expiryTs", "strike", "optionType")
    missing = [k for k in required if k not in canonical or canonical[k] is None]
    if missing:
        raise ValueError(f"serialise: missing required canonical fields: {missing}")

    base_currency: str = str(canonical["baseCurrency"]).upper()
    expiry_ts: int = int(canonical["expiryTs"])
    strike: float = float(canonical["strike"])
    option_type: str = str(canonical["optionType"]).upper()

    if option_type not in ("CE", "PE"):
        raise ValueError(
            f"serialise: optionType must be 'CE' or 'PE', got {option_type!r}"
        )

    # Convert option type back to Deribit C/P
    deribit_option = "C" if option_type == "CE" else "P"

    # Reconstruct date parts from expiryTs
    day, month, full_year = _expiry_ts_to_parts(expiry_ts)
    year_2digit = full_year % 100

    month_abbr = _MONTH_NUM_TO_STR.get(month)
    if month_abbr is None:
        raise ValueError(f"serialise: invalid month number {month}")

    # Format: {DD}{MON}{YY} — day zero-padded, month uppercase 3-letter
    date_str = f"{day:02d}{month_abbr}{year_2digit:02d}"
    strike_str = _format_strike(strike)

    return f"{base_currency}-{date_str}-{strike_str}-{deribit_option}"


# ---------------------------------------------------------------------------
# Public: verify_round_trip
# ---------------------------------------------------------------------------


def verify_round_trip(instrument_name: str) -> bool:
    """Verify the round-trip property for a given Deribit instrument name.

    The property: ``parse(serialise(parse(raw))) == parse(raw)``

    Returns ``True`` if the property holds, ``False`` otherwise.

    Note: if ``parse(raw)`` returns ``None`` (unparseable), the property
    trivially holds because ``parse(None)`` would also return ``None``.
    However, we return ``False`` for unparseable input to surface issues.

    Requirements: 14.7, 4.11, 17.8
    """
    first_parse = parse(instrument_name)
    if first_parse is None:
        # Unparseable — property cannot be verified; treat as False
        return False

    try:
        serialised = serialise(first_parse)
    except ValueError:
        return False

    second_parse = parse(serialised)

    # Both parses must be equal (dict equality)
    return first_parse == second_parse
