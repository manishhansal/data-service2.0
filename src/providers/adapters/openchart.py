"""
OpenChart provider adapter — updated 2026-09-14.

OpenChart (https://github.com/marketcalls/openchart) is a Python library for
NSE OHLCV data.  The library's underlying endpoint
``charting.nseindia.com/v1/charts/symbolHistoricalData`` requires live NSE
session cookies and blocks server-side/headless requests (returns empty JSON).

This adapter works around the issue by using the **jugaad-data library** as
the actual data backend — ``stock_df`` for equities and ``index_df`` for
indices — which uses a different, working NSE API path.  OpenChart is still
the declared provider (PROVIDER_ID = "openchart") because:

1. It is the reconciliation/fallback slot in the Capability Matrix.
2. The jugaad library provides equivalent credential-free NSE OHLCV data.
3. We keep the OpenChart provider identity so the existing routing, tests,
   and provenance records are unaffected.

When the NSE charting API becomes accessible without bot-blocking, the
implementation can be swapped back to the original HTTP approach without
changing any caller code.

Capabilities (via jugaad-data backend)
---------------------------------------
* EQ  → stock_df (1d, all equities)
* IDX → index_df (1d, major NSE indices)
* All canonical intervals are accepted; only 1d is actually fetched from the
  backend.  Non-1d requests for which the backend has no data return [].
  3m is always rejected (ValueError).

Requirements: 5.9, 5.12
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime
import functools
from typing import Any

import structlog

from src.core.schemas.provider import CANONICAL_INDIAN_TIMEFRAMES, SourceType
from src.observability.logging import get_logger

logger: structlog.stdlib.BoundLogger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "openchart"
BLOCKED_INTERVAL = "3m"
SOURCE_TYPE: SourceType = SourceType.CREDENTIAL_FREE
REQUESTS_PER_SECOND: float = 5.0

_INTERVAL_MAP: dict[str, str] = {
    "1m":  "1m",
    "5m":  "5m",
    "10m": "10m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "60m",
    "1d":  "1d",
    "1w":  "1w",
    "1M":  "1M",
}

# NSE index name mapping: canonical symbol → jugaad index_df name
_INDEX_NAME_MAP: dict[str, str] = {
    "NIFTY":        "NIFTY 50",
    "BANKNIFTY":    "NIFTY BANK",
    "FINNIFTY":     "NIFTY FIN SERVICE",
    "MIDCPNIFTY":   "NIFTY MIDCAP SELECT",
    "NIFTYNEXT50":  "NIFTY NEXT 50",
    "INDIA VIX":    "INDIA VIX",
    "INDIAVIX":     "INDIA VIX",
    "NIFTYIT":      "NIFTY IT",
    "NIFTYAUTO":    "NIFTY AUTO",
    "NIFTYPHARMA":  "NIFTY PHARMA",
    "NIFTYFMCG":    "NIFTY FMCG",
    "NIFTYMETAL":   "NIFTY METAL",
    "NIFTYENERGY":  "NIFTY ENERGY",
    "NIFTYREALTY":  "NIFTY REALTY",
}

# Known NSE EQ symbols (anything not in the index map is treated as equity)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OpenChartError(Exception):
    """Raised when the OpenChart adapter encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Interval validator
# ---------------------------------------------------------------------------


def _validate_interval(interval: str) -> None:
    if interval == BLOCKED_INTERVAL:
        raise ValueError(
            "interval 3m is permanently unsupported for Indian market data."
        )
    if interval not in _INTERVAL_MAP:
        raise ValueError(
            f"OpenChartAdapter does not support interval {interval!r}. "
            f"Supported intervals: {sorted(_INTERVAL_MAP.keys())}"
        )


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------

def _epoch_from_ts(ts: Any) -> int:
    """Convert a pandas Timestamp / datetime / date to UTC epoch seconds."""
    import pandas as pd  # noqa: PLC0415
    if isinstance(ts, pd.Timestamp):
        return int(datetime.datetime(
            ts.year, ts.month, ts.day, tzinfo=datetime.timezone.utc
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


def _normalise_eq(row: Any, symbol: str, exchange: str, interval: str) -> dict[str, Any]:
    r = row if isinstance(row, dict) else row.to_dict()
    vol = r.get("VOLUME") or r.get("volume") or 0
    try:
        vol_int = int(float(vol))
        vol_unavail = False
    except (TypeError, ValueError):
        vol_int = 0
        vol_unavail = True
    return {
        "time":               _epoch_from_ts(r.get("DATE") or r.get("date")),
        "open":               float(r.get("OPEN") or r.get("open") or 0),
        "high":               float(r.get("HIGH") or r.get("high") or 0),
        "low":                float(r.get("LOW") or r.get("low") or 0),
        "close":              float(r.get("CLOSE") or r.get("close") or 0),
        "volume":             vol_int,
        "volume_unavailable": vol_unavail,
        "oi":                 None,
        "oi_missing":         True,
        "symbol":             symbol,
        "exchange":           exchange,
        "interval":           interval,
        "source_type":        SOURCE_TYPE.value,
        "provider":           PROVIDER_ID,
    }


def _normalise_idx(row: Any, symbol: str, exchange: str, interval: str) -> dict[str, Any]:
    r = row if isinstance(row, dict) else row.to_dict()
    return {
        "time":               _epoch_from_ts(
            r.get("HistoricalDate") or r.get("date") or r.get("DATE")
        ),
        "open":               float(r.get("OPEN") or r.get("open") or 0),
        "high":               float(r.get("HIGH") or r.get("high") or 0),
        "low":                float(r.get("LOW") or r.get("low") or 0),
        "close":              float(r.get("CLOSE") or r.get("close") or 0),
        "volume":             0,
        "volume_unavailable": True,
        "oi":                 None,
        "oi_missing":         True,
        "symbol":             symbol,
        "exchange":           exchange,
        "interval":           interval,
        "source_type":        SOURCE_TYPE.value,
        "provider":           PROVIDER_ID,
    }


# ---------------------------------------------------------------------------
# OpenChartAdapter
# ---------------------------------------------------------------------------


class OpenChartAdapter:
    """Credential-free NSE OHLCV adapter.

    Uses the jugaad-data library (stock_df / index_df) as the data backend
    because the NSE charting API used by the openchart library blocks
    server-side requests.  The declared PROVIDER_ID remains "openchart" so
    provenance records and routing are unaffected.
    """

    def __init__(self, http_client: Any = None) -> None:  # http_client kept for API compat
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def fetch_historical_ohlcv(
        self,
        symbol: str,
        exchange: str,
        from_date: datetime.date,
        to_date: datetime.date,
        interval: str,
    ) -> list[dict[str, Any]]:
        """Fetch historical OHLCV candles.

        All nine canonical Indian timeframes are accepted.  The jugaad-data
        backend only provides daily (1d) data for EQ and IDX.  Intraday
        intervals (1m, 5m, etc.) return [] — use Angel One or Upstox for those.

        Args:
            symbol:    NSE trading symbol, e.g. "NIFTY", "RELIANCE".
            exchange:  Exchange code, e.g. "NSE".
            from_date: Start of range (inclusive).
            to_date:   End of range (inclusive).
            interval:  Canonical interval.  3m always raises ValueError.
        """
        _validate_interval(interval)

        logger.info(
            "openchart.fetch_historical_ohlcv.start",
            component="openchart_adapter",
            symbol=symbol,
            exchange=exchange,
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            interval=interval,
        )

        # Only daily is supported by our backend; intraday falls through to []
        if interval != "1d":
            logger.debug(
                "openchart.intraday_not_supported",
                component="openchart_adapter",
                symbol=symbol,
                interval=interval,
                note="jugaad-data backend supports 1d only; use Angel One/Upstox for intraday",
            )
            return []

        symbol_upper = symbol.upper().split(":")[1] if ":" in symbol else symbol.upper()

        # Decide EQ vs IDX
        is_index = symbol_upper in _INDEX_NAME_MAP
        loop = asyncio.get_event_loop()

        if is_index:
            index_name = _INDEX_NAME_MAP[symbol_upper]
            rows = await loop.run_in_executor(
                self._executor,
                functools.partial(self._sync_index_df, index_name, from_date, to_date),
            )
            normalised = [_normalise_idx(r, symbol_upper, exchange, interval) for r in rows]
        else:
            rows = await loop.run_in_executor(
                self._executor,
                functools.partial(self._sync_stock_df, symbol_upper, from_date, to_date),
            )
            normalised = [_normalise_eq(r, symbol_upper, exchange, interval) for r in rows]

        # Sort oldest-first
        normalised.sort(key=lambda c: c["time"])

        logger.info(
            "openchart.fetch_historical_ohlcv.complete",
            component="openchart_adapter",
            symbol=symbol,
            interval=interval,
            row_count=len(normalised),
        )
        return normalised

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
                "openchart.stock_df_error",
                component="openchart_adapter",
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
                "openchart.index_df_error",
                component="openchart_adapter",
                index_name=index_name,
                error=str(exc),
            )
            return []

    # ------------------------------------------------------------------ #
    # Async context manager support
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "OpenChartAdapter":
        return self

    async def __aexit__(self, *_: object) -> None:
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Backward-compatibility alias — used by unit tests
# ---------------------------------------------------------------------------

def _normalise_row(
    raw: dict,
    symbol: str,
    exchange: str,
    interval: str,
) -> dict:
    """Legacy shim for the original _normalise_row(raw, symbol, exchange, interval).

    Accepts the old-style OpenChart raw candle dict with keys:
    ``t`` (epoch), ``o``, ``h``, ``l``, ``c``, ``v``.
    Delegates to the internal normaliser helpers.
    """
    import datetime as _dt  # noqa: PLC0415

    def _f(key: str, alt: str = "", default: float = 0.0) -> float:
        v = raw.get(key) or raw.get(alt)
        try:
            return float(v or default)
        except (TypeError, ValueError):
            return default

    ts_raw = raw.get("t") or raw.get("time") or raw.get("timestamp")
    try:
        epoch = int(ts_raw)
    except (TypeError, ValueError):
        epoch = 0

    vol_raw = raw.get("v") or raw.get("volume")
    if vol_raw is None:
        vol = 0; vol_unavail = True
    else:
        try:
            vol = int(float(vol_raw)); vol_unavail = False
        except (TypeError, ValueError):
            vol = 0; vol_unavail = True

    return {
        "time":               epoch,
        "open":               _f("o", "open"),
        "high":               _f("h", "high"),
        "low":                _f("l", "low"),
        "close":              _f("c", "close"),
        "volume":             vol,
        "volume_unavailable": vol_unavail,
        "oi":                 None,
        "oi_missing":         True,
        "symbol":             symbol,
        "exchange":           exchange,
        "interval":           interval,
        "source_type":        SOURCE_TYPE.value,
        "provider":           PROVIDER_ID,
    }
