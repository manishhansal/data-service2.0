"""
Jugaad-data provider adapter — updated 2026-09-14.

Uses the jugaad-data Python library (v0.35.5+) to fetch NSE historical data
via the library's built-in API client rather than raw HTTP calls to the old
bhavcopy ZIP archives.

Background
----------
* The old adapter hit ``archives.nseindia.com/content/historical/DERIVATIVES``
  ZIP endpoints directly.  NSE changed the F&O bhavcopy format to UDiff on
  July 8, 2024 (see jugaad-data docs/HISTORICAL_DATA_GUIDE.md). That broke
  all direct bhavcopy downloads and `derivatives_df` / `bhavcopy_fo_save`.
* The library's **equity** and **index** paths (`stock_df`, `index_df`) still
  work via NSE's web API and are used here.
* F&O derivatives_df is also broken for dates ≥ 2024-07-08 in v0.35.5;
  fetch_fo_eod logs a warning and returns [] for those dates.

Capabilities
------------
* `fetch_eq_eod(symbol, from_date, to_date)` — EQ daily OHLCV via stock_df
* `fetch_idx_eod(symbol, from_date, to_date)` — Index daily OHLCV via index_df
* `fetch_fo_eod(symbol, from_date, to_date)` — F&O EOD with OI (broken for
  dates ≥ 2024-07-08; returns [] with a warning).

Requirements: 5.9, 5.12
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime
import functools
from typing import Any

import structlog

from src.core.schemas.provider import SourceType
from src.observability.logging import get_logger

logger: structlog.stdlib.BoundLogger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "jugaad_data"
SUPPORTED_INTERVAL = "1d"
BLOCKED_INTERVAL = "3m"
SOURCE_TYPE: SourceType = SourceType.CREDENTIAL_FREE
REQUESTS_PER_SECOND: float = 1.0

# NSE changed F&O bhavcopy from ZIP to UDiff on this date.
# derivatives_df / bhavcopy_fo_save are broken for dates >= this cutoff.
_FO_BHAVCOPY_BROKEN_FROM = datetime.date(2024, 7, 8)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JugaadDataError(Exception):
    """Raised when the Jugaad-data adapter encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Interval validator
# ---------------------------------------------------------------------------


def _validate_interval(interval: str) -> None:
    if interval == BLOCKED_INTERVAL:
        raise ValueError(
            "interval 3m is permanently unsupported for Indian market data."
        )
    if interval != SUPPORTED_INTERVAL:
        raise ValueError(
            f"JugaadDataAdapter only supports the '1d' interval; got {interval!r}."
        )


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------

def _epoch_from_timestamp(ts: Any) -> int:
    """Convert a pandas Timestamp / datetime / date to UTC epoch seconds."""
    import pandas as pd  # noqa: PLC0415
    if isinstance(ts, pd.Timestamp):
        # jugaad stock_df returns IST-offset timestamps (DATE col is stored as
        # previous-day 18:30 UTC i.e. midnight IST).  Treat as a plain date.
        # Strip time component and use midnight UTC for canonical storage.
        return int(datetime.datetime(
            ts.year, ts.month, ts.day,
            tzinfo=datetime.timezone.utc,
        ).timestamp())
    if isinstance(ts, datetime.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return int(ts.timestamp())
    if isinstance(ts, datetime.date):
        return int(datetime.datetime(
            ts.year, ts.month, ts.day, tzinfo=datetime.timezone.utc
        ).timestamp())
    return 0


def _normalise_eq_row(row: Any, symbol: str) -> dict[str, Any]:
    """Normalise a jugaad stock_df row (pandas Series) to canonical OHLCV dict."""
    import pandas as pd  # noqa: PLC0415
    r = row if isinstance(row, dict) else row.to_dict()
    epoch = _epoch_from_timestamp(r.get("DATE") or r.get("date"))
    vol = r.get("VOLUME") or r.get("volume") or 0
    try:
        vol_int = int(float(vol))
        vol_unavailable = False
    except (TypeError, ValueError):
        vol_int = 0
        vol_unavailable = True

    return {
        "time":               epoch,
        "open":               float(r.get("OPEN") or r.get("open") or 0),
        "high":               float(r.get("HIGH") or r.get("high") or 0),
        "low":                float(r.get("LOW") or r.get("low") or 0),
        "close":              float(r.get("CLOSE") or r.get("close") or 0),
        "volume":             vol_int,
        "volume_unavailable": vol_unavailable,
        "oi":                 None,
        "oi_missing":         True,
        "symbol":             symbol,
        "exchange":           "NSE",
        "interval":           "1d",
        "source_type":        SOURCE_TYPE.value,
        "provider":           PROVIDER_ID,
    }


def _normalise_idx_row(row: Any, symbol: str) -> dict[str, Any]:
    """Normalise a jugaad index_df row to canonical OHLCV dict."""
    r = row if isinstance(row, dict) else row.to_dict()
    epoch = _epoch_from_timestamp(
        r.get("HistoricalDate") or r.get("date") or r.get("DATE")
    )
    return {
        "time":               epoch,
        "open":               float(r.get("OPEN") or r.get("open") or 0),
        "high":               float(r.get("HIGH") or r.get("high") or 0),
        "low":                float(r.get("LOW") or r.get("low") or 0),
        "close":              float(r.get("CLOSE") or r.get("close") or 0),
        "volume":             0,
        "volume_unavailable": True,   # index_df doesn't provide volume
        "oi":                 None,
        "oi_missing":         True,
        "symbol":             symbol,
        "exchange":           "NSE",
        "interval":           "1d",
        "source_type":        SOURCE_TYPE.value,
        "provider":           PROVIDER_ID,
    }


# NSE index name mapping: canonical trading symbol → jugaad index_df symbol
_INDEX_NAME_MAP: dict[str, str] = {
    "NIFTY":      "NIFTY 50",
    "BANKNIFTY":  "NIFTY BANK",
    "FINNIFTY":   "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MIDCAP SELECT",
    "NIFTYNEXT50":"NIFTY NEXT 50",
    "SENSEX":     "SENSEX",
}

# ---------------------------------------------------------------------------
# JugaadDataAdapter
# ---------------------------------------------------------------------------


class JugaadDataAdapter:
    """Credential-free adapter using the jugaad-data library.

    Uses `jugaad_data.nse.stock_df` for equities and `index_df` for NSE
    indices.  All calls are synchronous blocking calls wrapped in a thread
    pool so they don't block the asyncio event loop.
    """

    def __init__(self) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    # ------------------------------------------------------------------ #
    # Public API — Equities
    # ------------------------------------------------------------------ #

    async def fetch_eq_eod(
        self,
        symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
        interval: str = "1d",
    ) -> list[dict[str, Any]]:
        """Fetch daily EQ OHLCV via jugaad-data stock_df."""
        _validate_interval(interval)
        logger.info(
            "jugaad_data.fetch_eq_eod.start",
            component="jugaad_data_adapter",
            symbol=symbol,
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
        )
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            self._executor,
            functools.partial(self._sync_stock_df, symbol, from_date, to_date),
        )
        normalised = [_normalise_eq_row(r, symbol) for r in rows]
        # Sort oldest-first
        normalised.sort(key=lambda c: c["time"])
        logger.info(
            "jugaad_data.fetch_eq_eod.complete",
            component="jugaad_data_adapter",
            symbol=symbol,
            row_count=len(normalised),
        )
        return normalised

    # ------------------------------------------------------------------ #
    # Public API — Indices
    # ------------------------------------------------------------------ #

    async def fetch_idx_eod(
        self,
        symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
        interval: str = "1d",
    ) -> list[dict[str, Any]]:
        """Fetch daily index OHLCV via jugaad-data index_df."""
        _validate_interval(interval)
        index_name = _INDEX_NAME_MAP.get(symbol.upper(), symbol)
        logger.info(
            "jugaad_data.fetch_idx_eod.start",
            component="jugaad_data_adapter",
            symbol=symbol,
            index_name=index_name,
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
        )
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            self._executor,
            functools.partial(self._sync_index_df, index_name, from_date, to_date),
        )
        normalised = [_normalise_idx_row(r, symbol) for r in rows]
        normalised.sort(key=lambda c: c["time"])
        logger.info(
            "jugaad_data.fetch_idx_eod.complete",
            component="jugaad_data_adapter",
            symbol=symbol,
            row_count=len(normalised),
        )
        return normalised

    # ------------------------------------------------------------------ #
    # Public API — F&O (legacy; broken for dates ≥ 2024-07-08)
    # ------------------------------------------------------------------ #

    async def fetch_fo_eod(
        self,
        symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
        interval: str = "1d",
    ) -> list[dict[str, Any]]:
        """Fetch F&O EOD with OI.

        NOTE: NSE changed the F&O bhavcopy format (UDiff) on 2024-07-08.
        The jugaad-data library's bhavcopy_fo_save / derivatives_df are
        broken for dates >= 2024-07-08.  This method warns and returns []
        for those dates.  Historical data before 2024-07-08 works correctly.
        """
        _validate_interval(interval)

        # For dates after the format change, return empty with a clear warning
        if from_date >= _FO_BHAVCOPY_BROKEN_FROM:
            logger.warning(
                "jugaad_data.fo_eod_unavailable",
                component="jugaad_data_adapter",
                symbol=symbol,
                from_date=from_date.isoformat(),
                reason="NSE changed F&O bhavcopy to UDiff format on 2024-07-08; "
                       "jugaad-data v0.35.5 derivatives_df is broken for this date range. "
                       "Use Angel One or Upstox for F&O OHLCV data.",
            )
            return []

        logger.info(
            "jugaad_data.fetch_fo_eod.start",
            component="jugaad_data_adapter",
            symbol=symbol,
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
        )
        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            self._executor,
            functools.partial(self._sync_fo_bhavcopy, symbol, from_date, to_date),
        )
        logger.info(
            "jugaad_data.fetch_fo_eod.complete",
            component="jugaad_data_adapter",
            symbol=symbol,
            row_count=len(rows),
        )
        return rows

    # ------------------------------------------------------------------ #
    # Synchronous helpers (run in thread pool)
    # ------------------------------------------------------------------ #

    def _sync_stock_df(
        self,
        symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
    ) -> list[dict[str, Any]]:
        try:
            from jugaad_data.nse import stock_df  # noqa: PLC0415
            df = stock_df(symbol=symbol, from_date=from_date, to_date=to_date, series="EQ")
            if df is None or len(df) == 0:
                return []
            return df.to_dict(orient="records")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "jugaad_data.stock_df_error",
                component="jugaad_data_adapter",
                symbol=symbol,
                error=str(exc),
            )
            return []

    def _sync_index_df(
        self,
        index_name: str,
        from_date: datetime.date,
        to_date: datetime.date,
    ) -> list[dict[str, Any]]:
        try:
            from jugaad_data.nse import index_df  # noqa: PLC0415
            df = index_df(symbol=index_name, from_date=from_date, to_date=to_date)
            if df is None or len(df) == 0:
                return []
            return df.to_dict(orient="records")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "jugaad_data.index_df_error",
                component="jugaad_data_adapter",
                index_name=index_name,
                error=str(exc),
            )
            return []

    def _sync_fo_bhavcopy(
        self,
        symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
    ) -> list[dict[str, Any]]:
        """Fetch F&O bhavcopy for dates before 2024-07-08 (ZIP format era)."""
        import csv, io, zipfile  # noqa: PLC0415
        import httpx  # noqa: PLC0415

        results: list[dict[str, Any]] = []
        current = from_date
        NSE_FO_BASE = "https://archives.nseindia.com/content/historical/DERIVATIVES"

        while current <= to_date:
            if current.weekday() < 5:
                month_abbr = current.strftime("%b").upper()
                year_str   = current.strftime("%Y")
                filename   = f"fo{current.day:02d}{month_abbr}{year_str}bhav.csv.zip"
                url = f"{NSE_FO_BASE}/{year_str}/{month_abbr}/{filename}"
                try:
                    r = httpx.get(url, timeout=30, follow_redirects=True,
                                  headers={"User-Agent": "data-service/2.0"})
                    if r.status_code == 200:
                        zf = zipfile.ZipFile(io.BytesIO(r.content))
                        csv_name = next((n for n in zf.namelist() if n.endswith(".csv")), None)
                        if csv_name:
                            csv_text = zf.read(csv_name).decode("utf-8", errors="replace")
                            reader = csv.DictReader(io.StringIO(csv_text))
                            for row in reader:
                                if row.get("SYMBOL", "").strip().upper() == symbol.upper():
                                    row["TIMESTAMP"] = current
                                    results.append(self._normalise_fo_row(row, symbol))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "jugaad_data.fo_day_skip",
                        component="jugaad_data_adapter",
                        date=current.isoformat(),
                        error=str(exc),
                    )
            current += datetime.timedelta(days=1)
        return results

    @staticmethod
    def _normalise_fo_row(row: dict[str, Any], symbol: str) -> dict[str, Any]:
        """Normalise a raw F&O bhavcopy CSV row."""
        def _f(key: str) -> float:
            v = row.get(key) or row.get(key.lower()) or ""
            try:
                return float(str(v).replace(",", "").strip())
            except (ValueError, AttributeError):
                return 0.0

        def _i_opt(key: str) -> int | None:
            """Return int if key present and non-empty, else None."""
            v = row.get(key) or row.get(key.lower())
            if v is None or str(v).strip() == "":
                return None
            try:
                return int(float(str(v).replace(",", "").strip()))
            except (ValueError, AttributeError):
                return None

        def _i(key: str) -> int:
            v = _i_opt(key)
            return v if v is not None else 0

        # Check if CONTRACTS field exists and is non-empty
        contracts_raw = row.get("CONTRACTS") or row.get("contracts")
        if contracts_raw is not None and str(contracts_raw).strip() not in ("", "0"):
            vol_int = _i("CONTRACTS")
            vol_unavailable = False
        else:
            vol_int = 0
            vol_unavailable = True

        # OI from OPENINT or OPEN_INT (never from TRDVAL)
        oi_val = _i_opt("OPENINT") or _i_opt("OPEN_INT") or _i_opt("openint") or _i_opt("open_int")

        # Timestamp
        date_val = row.get("TIMESTAMP") or row.get("timestamp") or row.get("date") or row.get("DATE")
        if isinstance(date_val, datetime.datetime):
            epoch = int(date_val.replace(tzinfo=datetime.timezone.utc).timestamp())
        elif isinstance(date_val, datetime.date) and not isinstance(date_val, datetime.datetime):
            epoch = int(datetime.datetime(
                date_val.year, date_val.month, date_val.day,
                tzinfo=datetime.timezone.utc,
            ).timestamp())
        elif isinstance(date_val, str) and date_val.strip():
            import re  # noqa: PLC0415
            # Accept DD-Mon-YYYY (e.g. "15-Jan-2024") and YYYY-MM-DD
            m = re.match(r"(\d{2})-([A-Za-z]{3})-(\d{4})", date_val.strip())
            if m:
                import calendar  # noqa: PLC0415
                day, mon_str, year = int(m.group(1)), m.group(2), int(m.group(3))
                months = {v: k for k, v in enumerate(calendar.month_abbr) if v}
                mon = months.get(mon_str.capitalize(), 1)
                epoch = int(datetime.datetime(year, mon, day, tzinfo=datetime.timezone.utc).timestamp())
            else:
                # Try ISO format YYYY-MM-DD
                try:
                    dt = datetime.datetime.strptime(date_val.strip()[:10], "%Y-%m-%d")
                    epoch = int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
                except ValueError:
                    epoch = 0
        else:
            epoch = 0

        return {
            "time":               epoch,
            "open":               _f("OPEN"),
            "high":               _f("HIGH"),
            "low":                _f("LOW"),
            "close":              _f("CLOSE"),
            "volume":             vol_int,
            "volume_unavailable": vol_unavailable,
            "oi":                 oi_val,
            "oi_missing":         oi_val is None,
            "symbol":             symbol or row.get("SYMBOL", ""),
            "exchange":           "NFO",
            "interval":           "1d",
            "source_type":        SOURCE_TYPE.value,
            "provider":           PROVIDER_ID,
        }

    # ------------------------------------------------------------------ #
    # Async context manager support
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "JugaadDataAdapter":
        return self

    async def __aexit__(self, *_: object) -> None:
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Backward-compatibility alias — used by unit tests
# ---------------------------------------------------------------------------

def _normalise_row(raw: dict) -> dict:
    """Legacy alias for _normalise_fo_row; accepts F&O bhavcopy-style dicts.

    This shim preserves the original public API used by the unit test suite
    while the adapter's internal implementation was refactored.
    """
    return JugaadDataAdapter._normalise_fo_row(raw, raw.get("SYMBOL", ""))

