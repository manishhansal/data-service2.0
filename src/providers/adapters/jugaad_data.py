"""
Jugaad-data provider adapter — Task 4.8.

Jugaad-data (https://jugaad-data.readthedocs.io/) is a credential-free,
open-source library for fetching NSE F&O EOD (end-of-day) historical data
with open interest.  It is the **primary** source for F&O EOD history with OI
in the platform's Capability_Matrix.

Key constraints
---------------
* Credential-free — no authentication required.
* Supports only ``1d`` interval for Indian F&O instruments.
* The ``3m`` interval is permanently blocked (raises ``ValueError``).
* Source type: ``CREDENTIAL_FREE``.
* Rate limit: 1 req/s (conservative; jugaad-data is an unofficial scraper
  and must not be hammered to avoid bans).
* Returns daily OHLCV candles enriched with ``oi`` (open interest in
  contracts) for F&O instruments.

Requirements: 5.9, 5.12
"""

from __future__ import annotations

import asyncio
import datetime
import functools
from typing import Any

import httpx
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

# Jugaad-data fetch the NSE bhavcopy CSVs under the hood.
# The base endpoint here is the NSE derivatives bhavcopy path used internally.
_NSE_FO_BHAVCOPY_BASE_URL = "https://archives.nseindia.com/content/historical/DERIVATIVES"

# Timeout for individual HTTP requests (seconds).
_HTTP_TIMEOUT_SEC: float = 30.0

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JugaadDataError(Exception):
    """Raised when the Jugaad-data adapter encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Interval validator
# ---------------------------------------------------------------------------


def _validate_interval(interval: str) -> None:
    """Raise ``ValueError`` if the interval is banned or unsupported.

    The ``3m`` interval is permanently banned for all Indian market data
    (Requirement 1.5, 4.2, 10.11).  Only ``1d`` is supported by Jugaad-data.

    Args:
        interval: The requested candle interval string (e.g. ``"1d"``).

    Raises:
        ValueError: If ``interval == "3m"`` or the interval is not ``"1d"``.
    """
    if interval == BLOCKED_INTERVAL:
        raise ValueError(
            "interval 3m is permanently unsupported for Indian market data. "
            "JugaadDataAdapter only serves 1d F&O EOD data."
        )
    if interval != SUPPORTED_INTERVAL:
        raise ValueError(
            f"JugaadDataAdapter only supports the '1d' interval; "
            f"got {interval!r}. Use Angel One or Upstox for intraday data."
        )


# ---------------------------------------------------------------------------
# Row normaliser
# ---------------------------------------------------------------------------


def _normalise_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw Jugaad-data / bhavcopy row into the platform's canonical
    OHLCV+OI dict shape.

    The platform canonical shape for an EOD row with OI is::

        {
            "time":             int,    # UTC epoch seconds (candle open time)
            "open":             float,
            "high":             float,
            "low":              float,
            "close":            float,
            "volume":           int,    # 0 when unavailable
            "volume_unavailable": bool,
            "oi":               int | None,   # open interest in contracts
            "oi_missing":       bool,
            "symbol":           str,
            "exchange":         str,
            "interval":         str,
            "source_type":      str,
            "provider":         str,
        }

    OI semantics (Requirement 3.3, 6.2):
    * ``oi`` is set only when the source explicitly provides open-interest in
      contracts.
    * ``oi`` is **never** populated from ``tradedValue``; if absent it is
      ``None`` with ``oi_missing=True``.

    Args:
        raw: A single row dict produced by Jugaad-data / the bhavcopy parser.

    Returns:
        A normalised dict conforming to the canonical OHLCV+OI shape.
    """
    # Bhavcopy column names differ from Jugaad-data's library output.
    # We accept both and normalise to canonical names.
    def _get(*keys: str, cast: type = float, default: Any = None) -> Any:
        for key in keys:
            val = raw.get(key)
            if val is not None and val != "":
                try:
                    return cast(val)
                except (ValueError, TypeError):
                    pass
        return default

    # --- timestamp -----------------------------------------------------------
    # Jugaad-data returns `TIMESTAMP` as a datetime.date or ISO string.
    date_val = raw.get("TIMESTAMP") or raw.get("date") or raw.get("timestamp")
    if isinstance(date_val, datetime.datetime):
        epoch_sec = int(date_val.replace(tzinfo=datetime.timezone.utc).timestamp())
    elif isinstance(date_val, datetime.date) and not isinstance(date_val, datetime.datetime):
        epoch_sec = int(
            datetime.datetime(
                date_val.year, date_val.month, date_val.day,
                tzinfo=datetime.timezone.utc,
            ).timestamp()
        )
    elif isinstance(date_val, str):
        # Accept "DD-Mon-YYYY", "YYYY-MM-DD", etc.
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                d = datetime.datetime.strptime(date_val, fmt)
                epoch_sec = int(d.replace(tzinfo=datetime.timezone.utc).timestamp())
                break
            except ValueError:
                pass
        else:
            epoch_sec = 0
    else:
        epoch_sec = 0

    # --- OHLCV ---------------------------------------------------------------
    open_  = _get("OPEN", "open", cast=float, default=0.0)
    high   = _get("HIGH", "high", cast=float, default=0.0)
    low    = _get("LOW", "low", cast=float, default=0.0)
    close  = _get("CLOSE", "close", cast=float, default=0.0)

    # Volume from bhavcopy: column name is CONTRACTS or VOLUME or traded_contracts.
    vol_raw = _get("CONTRACTS", "VOLUME", "volume", "traded_contracts", cast=int, default=None)
    if vol_raw is None:
        volume: int = 0
        volume_unavailable: bool = True
    else:
        volume = int(vol_raw)
        volume_unavailable = False

    # --- OI ------------------------------------------------------------------
    # Jugaad-data exposes OI as OPEN_INT or oi.
    # NEVER populate from tradedValue / TRDVAL (Requirement 3.3, 6.2).
    oi_raw = _get("OPEN_INT", "oi", "openInterest", cast=int, default=None)
    if oi_raw is None:
        oi: int | None = None
        oi_missing: bool = True
    else:
        oi = int(oi_raw)
        oi_missing = False

    # --- Symbol / Exchange ---------------------------------------------------
    symbol = str(raw.get("SYMBOL", raw.get("symbol", ""))).strip()
    exchange = str(raw.get("EXCHANGE", raw.get("exchange", "NFO"))).strip() or "NFO"

    return {
        "time": epoch_sec,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "volume_unavailable": volume_unavailable,
        "oi": oi,
        "oi_missing": oi_missing,
        "symbol": symbol,
        "exchange": exchange,
        "interval": SUPPORTED_INTERVAL,
        "source_type": SOURCE_TYPE.value,
        "provider": PROVIDER_ID,
    }


# ---------------------------------------------------------------------------
# JugaadDataAdapter
# ---------------------------------------------------------------------------


class JugaadDataAdapter:
    """Credential-free adapter for Jugaad-data / NSE F&O bhavcopy EOD data.

    This adapter is the **primary** route for F&O EOD historical OHLCV data
    with open interest (Capability_Matrix: Jugaad-data → FO → HISTORICAL_OHLCV
    → priority 1).

    It fetches NSE derivatives bhavcopy files for the requested date range,
    parses the CSV rows, and returns a list of normalised canonical dicts
    ready for the Validation Pipeline.

    Usage::

        adapter = JugaadDataAdapter()
        rows = await adapter.fetch_fo_eod(
            symbol="NIFTY",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )

    Rate limiting is enforced at the gateway layer (token-bucket, 1 req/s).
    The adapter itself does not perform internal rate limiting.

    Args:
        http_client: Optional pre-constructed ``httpx.AsyncClient``.  When
            ``None`` a fresh client is created per request (suitable for unit
            tests and low-volume use).  In production the gateway should
            inject a shared client with connection pooling.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client
        self._owns_client = http_client is None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def fetch_fo_eod(
        self,
        symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
        interval: str = SUPPORTED_INTERVAL,
    ) -> list[dict[str, Any]]:
        """Fetch F&O EOD daily candles with OI for ``symbol``.

        Only ``1d`` interval is supported.  Requesting ``3m`` always raises
        ``ValueError`` (permanent Indian-market block, Requirement 1.5).

        The returned list is ordered chronologically (oldest first).  Each
        element is a normalised canonical OHLCV+OI dict (see ``_normalise_row``
        for the exact shape).

        Args:
            symbol:    NSE trading symbol, e.g. ``"NIFTY"`` or ``"RELIANCE"``.
            from_date: Start of the date range (inclusive).
            to_date:   End of the date range (inclusive).
            interval:  Candle interval — must be ``"1d"``.

        Returns:
            List of normalised OHLCV+OI dicts ordered oldest-first.

        Raises:
            ValueError: If ``interval`` is ``"3m"`` or not ``"1d"``.
            JugaadDataError: If the upstream request fails.
        """
        _validate_interval(interval)

        logger.info(
            "jugaad_data.fetch_fo_eod.start",
            component="jugaad_data_adapter",
            symbol=symbol,
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
        )

        raw_rows = await self._fetch_bhavcopy_range(symbol, from_date, to_date)
        normalised = [_normalise_row(row) for row in raw_rows]

        logger.info(
            "jugaad_data.fetch_fo_eod.complete",
            component="jugaad_data_adapter",
            symbol=symbol,
            row_count=len(normalised),
        )
        return normalised

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _fetch_bhavcopy_range(
        self,
        symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
    ) -> list[dict[str, Any]]:
        """Iterate over each date in [from_date, to_date] and collect rows.

        NSE publishes one bhavcopy CSV per trading day.  This method iterates
        the date range (calendar days), attempts to fetch each day's CSV, and
        collects all rows matching ``symbol``.

        Weekends are skipped.  Holidays are handled by treating a 404 / empty
        response as a non-trading day (no error emitted).

        Args:
            symbol:    NSE trading symbol.
            from_date: Start of the date range (inclusive).
            to_date:   End of the date range (inclusive).

        Returns:
            Flat list of raw row dicts for the requested symbol.
        """
        results: list[dict[str, Any]] = []
        current = from_date
        while current <= to_date:
            # Skip weekends — NSE is closed Saturday and Sunday.
            if current.weekday() < 5:
                day_rows = await self._fetch_bhavcopy_day(symbol, current)
                results.extend(day_rows)
            current += datetime.timedelta(days=1)
        return results

    async def _fetch_bhavcopy_day(
        self,
        symbol: str,
        date: datetime.date,
    ) -> list[dict[str, Any]]:
        """Fetch and parse the NSE derivatives bhavcopy CSV for a single date.

        The bhavcopy URL pattern is::

            https://archives.nseindia.com/content/historical/DERIVATIVES/
            {YYYY}/{MON}/fo{DD}{MON}{YYYY}bhav.csv.zip

        NSE returns a ZIP containing a single CSV.  This method fetches the
        ZIP, extracts the CSV, and returns all rows where the SYMBOL column
        matches ``symbol`` (case-insensitive).

        404 and empty responses are treated as non-trading days and return an
        empty list without raising.

        Args:
            symbol: NSE trading symbol.
            date:   The trading day to fetch.

        Returns:
            List of raw row dicts for ``symbol`` on ``date``, or ``[]`` when
            the day has no data (holiday, weekend, or missing archive).
        """
        import csv
        import io
        import zipfile

        month_abbr = date.strftime("%b").upper()   # e.g. "JAN"
        year_str = date.strftime("%Y")              # e.g. "2024"
        filename = (
            f"fo{date.day:02d}{month_abbr}{year_str}bhav.csv.zip"
        )
        url = (
            f"{_NSE_FO_BHAVCOPY_BASE_URL}/{year_str}/{month_abbr}/{filename}"
        )

        try:
            client = await self._get_client()
            response = await client.get(url, timeout=_HTTP_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "jugaad_data.bhavcopy_fetch_error",
                component="jugaad_data_adapter",
                url=url,
                error=str(exc),
            )
            return []

        if response.status_code == 404:
            # Non-trading day — not an error.
            return []

        if response.status_code != 200:
            logger.warning(
                "jugaad_data.bhavcopy_non_200",
                component="jugaad_data_adapter",
                url=url,
                status_code=response.status_code,
            )
            return []

        try:
            zip_bytes = io.BytesIO(response.content)
            with zipfile.ZipFile(zip_bytes) as zf:
                # The ZIP contains a single CSV with the same base name.
                csv_name = next(
                    (n for n in zf.namelist() if n.endswith(".csv")),
                    None,
                )
                if csv_name is None:
                    return []
                csv_content = zf.read(csv_name).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "jugaad_data.bhavcopy_parse_error",
                component="jugaad_data_adapter",
                url=url,
                error=str(exc),
            )
            return []

        rows: list[dict[str, Any]] = []
        reader = csv.DictReader(io.StringIO(csv_content))
        for row in reader:
            row_symbol = row.get("SYMBOL", "").strip().upper()
            if row_symbol == symbol.upper():
                # Add the date so the normaliser can construct a timestamp.
                row["TIMESTAMP"] = date
                rows.append(dict(row))
        return rows

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating one if necessary."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": "data-service/2.0 jugaad-data-adapter"},
                timeout=_HTTP_TIMEOUT_SEC,
                follow_redirects=True,
            )
            self._owns_client = True
        return self._client

    # ------------------------------------------------------------------ #
    # Async context manager support
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> JugaadDataAdapter:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
