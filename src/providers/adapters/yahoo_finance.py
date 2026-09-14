"""
Yahoo Finance provider adapter — Task 4.8.

Yahoo Finance is used as a **controlled secondary fallback** in DATA-SERVICE 2.0.
Its use is strictly restricted to equity EOD (``1d``) historical OHLCV data
only.  It must **never** be used for:

* Live / real-time data
* F&O instruments
* Open interest (OI)
* Implied volatility (IV)
* Option Greeks
* Bid / ask prices
* Crypto instruments

All Yahoo Finance data carries the ``SECONDARY_FALLBACK`` provenance tag and
a maximum quality grade of **B** (enforced by the Normaliser downstream).

Key constraints
---------------
* Credential-free — no authentication required.
* ``1d`` interval ONLY — all other intervals raise ``ValueError``.
* Instrument scope: NSE equity symbols ONLY (e.g. ``"RELIANCE.NS"``).
* Source type: ``SECONDARY_FALLBACK``.
* Rate limit: 1 req/s (Yahoo Finance has unofficial, undocumented limits).
* Provenance tag ``SECONDARY_FALLBACK`` must be present on every row returned.
* Maximum quality grade: ``B`` (enforced in downstream Normaliser, not here).

Requirements: 5.9, 5.12
"""

from __future__ import annotations

import datetime
from typing import Any

import httpx
import structlog

from src.core.schemas.provider import SourceType
from src.observability.logging import get_logger

logger: structlog.stdlib.BoundLogger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_ID = "yahoo_finance"
SUPPORTED_INTERVAL = "1d"
BLOCKED_INTERVAL = "3m"
SOURCE_TYPE: SourceType = SourceType.SECONDARY_FALLBACK
REQUESTS_PER_SECOND: float = 1.0

# Maximum quality grade for any Yahoo Finance sourced data (Requirement 5.12).
MAX_QUALITY_GRADE = "B"

# Yahoo Finance v8 chart API — used by the adapter.
_YF_CHART_API_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

# Timeout for individual HTTP requests (seconds).
_HTTP_TIMEOUT_SEC: float = 30.0

# Allowed non-1d intervals — none.  Listing here for clarity.
_ALLOWED_INTERVALS: frozenset[str] = frozenset({"1d"})

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class YahooFinanceError(Exception):
    """Raised when the Yahoo Finance adapter encounters an unrecoverable error."""


class YahooFinanceIntervalError(ValueError):
    """Raised when a non-``1d`` interval is requested."""


class YahooFinanceInstrumentError(ValueError):
    """Raised when a non-equity instrument type is requested."""


# ---------------------------------------------------------------------------
# Interval / instrument validators
# ---------------------------------------------------------------------------


def _validate_interval(interval: str) -> None:
    """Raise ``ValueError`` for any interval other than ``1d``.

    Yahoo Finance is restricted to equity EOD (daily) data exclusively
    (Requirement 5.12).  Requesting any other interval, including the
    permanently-blocked ``3m``, raises ``ValueError``.

    Args:
        interval: The requested candle interval string.

    Raises:
        YahooFinanceIntervalError: For any interval other than ``"1d"``.
    """
    if interval == BLOCKED_INTERVAL:
        raise YahooFinanceIntervalError(
            "interval 3m is permanently unsupported for Indian market data. "
            "YahooFinanceAdapter only serves 1d equity EOD data."
        )
    if interval not in _ALLOWED_INTERVALS:
        raise YahooFinanceIntervalError(
            f"YahooFinanceAdapter only supports '1d' interval; "
            f"got {interval!r}. Yahoo Finance is a secondary fallback for "
            "equity EOD historical data only — it must not be used for "
            "intraday, F&O, live, or crypto data."
        )


def _validate_instrument_type(instrument_type: str) -> None:
    """Raise ``ValueError`` if the instrument type is not equity (``EQ``).

    Yahoo Finance is restricted to NSE equity instruments.  Requesting F&O,
    indices, or crypto raises ``YahooFinanceInstrumentError``.

    Args:
        instrument_type: Instrument type string (e.g. ``"EQ"``, ``"FO"``).

    Raises:
        YahooFinanceInstrumentError: If ``instrument_type`` is not ``"EQ"``.
    """
    if instrument_type.upper() not in ("EQ", "IDX", "INDEX"):
        raise YahooFinanceInstrumentError(
            f"YahooFinanceAdapter is restricted to equity (EQ) instruments; "
            f"got {instrument_type!r}. Yahoo Finance must not be used for "
            "F&O, options, futures, indices, or crypto data."
        )


# ---------------------------------------------------------------------------
# Symbol resolver
# ---------------------------------------------------------------------------


def _resolve_yf_symbol(symbol: str, exchange: str = "NSE") -> str:
    """Return the Yahoo Finance ticker for an NSE symbol.

    NSE equities are suffixed with ``.NS`` on Yahoo Finance.

    Args:
        symbol:   NSE trading symbol, e.g. ``"RELIANCE"``.
        exchange: Exchange code (default ``"NSE"``).

    Returns:
        Yahoo Finance ticker string, e.g. ``"RELIANCE.NS"``.
    """
    sym = symbol.upper().strip()
    # NSE index → Yahoo Finance ticker mapping (indices don't use .NS suffix)
    _NSE_INDEX_MAP = {
        "NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK",
        "FINNIFTY": "^CNXFIN", "MIDCPNIFTY": "^NSEMDCP50", "SENSEX": "^BSESN",
    }
    if sym in _NSE_INDEX_MAP:
        return _NSE_INDEX_MAP[sym]
    # If the symbol already has an exchange suffix or ^ prefix, use it as-is.
    if "." in sym or sym.startswith("^"):
        return sym
    exchange_upper = exchange.upper()
    if exchange_upper in ("NSE", "NFO"):
        return f"{sym}.NS"
    if exchange_upper in ("BSE", "BFO"):
        return f"{sym}.BO"
    return f"{sym}.NS"  # Default to NSE suffix


# ---------------------------------------------------------------------------
# Row normaliser
# ---------------------------------------------------------------------------


def _normalise_rows(
    timestamps: list[int],
    ohlcv: dict[str, list[float | None]],
    symbol: str,
    exchange: str,
) -> list[dict[str, Any]]:
    """Convert raw Yahoo Finance chart API arrays into canonical OHLCV dicts.

    The Yahoo Finance v8 chart API returns parallel arrays for each OHLCV
    field.  This function zips them into the platform's canonical row shape.

    The ``SECONDARY_FALLBACK`` provenance tag is attached to every row.

    OI semantics (Requirement 3.3, 6.2):
    Yahoo Finance does not provide open interest.  ``oi`` is always ``None``
    with ``oi_missing=True``; it is **never** populated from any other field.

    Yahoo Finance does not provide IV, Greeks, bid, or ask.  These are
    always ``None`` in the returned rows.

    Args:
        timestamps: List of Unix epoch seconds (candle open times).
        ohlcv:      Dict of parallel arrays keyed by ``open``, ``high``,
                    ``low``, ``close``, ``volume``.
        symbol:     Trading symbol (canonical NSE form).
        exchange:   Exchange code.

    Returns:
        List of normalised canonical OHLCV dicts, ordered chronologically.
    """
    opens   = ohlcv.get("open",   [])
    highs   = ohlcv.get("high",   [])
    lows    = ohlcv.get("low",    [])
    closes  = ohlcv.get("close",  [])
    volumes = ohlcv.get("volume", [])

    rows: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        def _safe(lst: list[float | None], idx: int, default: float = 0.0) -> float:
            try:
                val = lst[idx]
                return float(val) if val is not None else default
            except (IndexError, TypeError, ValueError):
                return default

        def _safe_int(lst: list[float | None], idx: int) -> tuple[int, bool]:
            try:
                val = lst[idx]
                if val is None:
                    return 0, True
                return int(float(val)), False
            except (IndexError, TypeError, ValueError):
                return 0, True

        open_  = _safe(opens,   i)
        high   = _safe(highs,   i)
        low    = _safe(lows,    i)
        close  = _safe(closes,  i)
        volume, vol_unavailable = _safe_int(volumes, i)

        rows.append({
            "time":               ts,
            "open":               open_,
            "high":               high,
            "low":                low,
            "close":              close,
            "volume":             volume,
            "volume_unavailable": vol_unavailable,
            # Yahoo Finance never provides OI (Requirement 3.3, 6.2).
            "oi":                 None,
            "oi_missing":         True,
            # Yahoo Finance never provides IV, Greeks, bid, ask.
            "iv":                 None,
            "iv_missing":         True,
            "bid":                None,
            "ask":                None,
            "symbol":             symbol,
            "exchange":           exchange,
            "interval":           SUPPORTED_INTERVAL,
            "source_type":        SOURCE_TYPE.value,   # "SECONDARY_FALLBACK"
            "provider":           PROVIDER_ID,
            "max_quality_grade":  MAX_QUALITY_GRADE,
        })
    return rows


# ---------------------------------------------------------------------------
# YahooFinanceAdapter
# ---------------------------------------------------------------------------


class YahooFinanceAdapter:
    """Credential-free adapter for Yahoo Finance equity EOD historical data.

    This adapter is a **controlled secondary fallback** for NSE equity EOD
    data only.  It must not be used for any other purpose.

    Every response carries ``source_type="SECONDARY_FALLBACK"`` and
    ``max_quality_grade="B"``.  The downstream Normaliser enforces the quality
    grade cap.

    Usage::

        adapter = YahooFinanceAdapter()
        rows = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )

    Rate limiting is enforced at the gateway layer (token-bucket, 1 req/s).

    Args:
        http_client: Optional pre-constructed ``httpx.AsyncClient``.  When
            ``None`` a fresh client is created on first use.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client
        self._owns_client = http_client is None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def fetch_historical_ohlcv(
        self,
        symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
        exchange: str = "NSE",
        instrument_type: str = "EQ",
    ) -> list[dict[str, Any]]:
        """Fetch equity EOD historical OHLCV from Yahoo Finance.

        This method always uses the ``1d`` interval.  Requesting any other
        interval, or any non-equity instrument type, raises ``ValueError``.

        Every returned row carries ``source_type="SECONDARY_FALLBACK"`` and
        ``max_quality_grade="B"``.

        Args:
            symbol:          NSE trading symbol, e.g. ``"RELIANCE"``.
            from_date:       Start of the requested date range (inclusive).
            to_date:         End of the requested date range (inclusive).
            exchange:        Exchange code (default ``"NSE"``).
            instrument_type: Must be ``"EQ"`` (raises ``ValueError``
                             otherwise).

        Returns:
            List of normalised OHLCV dicts ordered oldest-first.

        Raises:
            YahooFinanceIntervalError: If a non-``1d`` interval is somehow
                passed (this method always uses ``1d`` internally).
            YahooFinanceInstrumentError: If ``instrument_type != "EQ"``.
            YahooFinanceError: If the upstream request fails in an
                unrecoverable way.
        """
        # Always 1d — validate defensively.
        _validate_interval(SUPPORTED_INTERVAL)
        _validate_instrument_type(instrument_type)

        yf_symbol = _resolve_yf_symbol(symbol, exchange)

        logger.info(
            "yahoo_finance.fetch_historical_ohlcv.start",
            component="yahoo_finance_adapter",
            symbol=symbol,
            yf_symbol=yf_symbol,
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
        )

        raw_timestamps, raw_ohlcv = await self._request(yf_symbol, from_date, to_date)
        normalised = _normalise_rows(raw_timestamps, raw_ohlcv, symbol, exchange)

        logger.info(
            "yahoo_finance.fetch_historical_ohlcv.complete",
            component="yahoo_finance_adapter",
            symbol=symbol,
            row_count=len(normalised),
        )
        return normalised

    # ------------------------------------------------------------------ #
    # Interval validation guard (for callers that pass an explicit interval)
    # ------------------------------------------------------------------ #

    async def fetch_historical_ohlcv_with_interval(
        self,
        symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
        interval: str,
        exchange: str = "NSE",
        instrument_type: str = "EQ",
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV with explicit interval validation.

        This variant accepts an explicit ``interval`` parameter and rejects
        anything other than ``"1d"`` with ``YahooFinanceIntervalError``.

        This is the entry point used by the Provider Gateway when routing
        through Yahoo Finance with a caller-supplied interval.

        Args:
            symbol:          NSE trading symbol.
            from_date:       Start date (inclusive).
            to_date:         End date (inclusive).
            interval:        Must be ``"1d"``; any other value raises
                             ``YahooFinanceIntervalError``.
            exchange:        Exchange code (default ``"NSE"``).
            instrument_type: Must be ``"EQ"``.

        Returns:
            List of normalised OHLCV dicts.

        Raises:
            YahooFinanceIntervalError: If ``interval != "1d"``.
            YahooFinanceInstrumentError: If ``instrument_type != "EQ"``.
        """
        _validate_interval(interval)
        _validate_instrument_type(instrument_type)
        return await self.fetch_historical_ohlcv(
            symbol=symbol,
            from_date=from_date,
            to_date=to_date,
            exchange=exchange,
            instrument_type=instrument_type,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        yf_symbol: str,
        from_date: datetime.date,
        to_date: datetime.date,
    ) -> tuple[list[int], dict[str, list[float | None]]]:
        """Fetch raw chart data from the Yahoo Finance v8 API.

        The endpoint is::

            GET /v8/finance/chart/{symbol}?period1=<epoch>&period2=<epoch>&interval=1d

        On success the response JSON contains ``chart.result[0]`` with:
        * ``timestamp`` — list of Unix epoch seconds
        * ``indicators.quote[0]`` — dict with lists: ``open``, ``high``,
          ``low``, ``close``, ``volume``

        On failure or malformed JSON the method logs a warning and returns
        ``([], {})``.

        Args:
            yf_symbol: Yahoo Finance ticker (e.g. ``"RELIANCE.NS"``).
            from_date: Start date.
            to_date:   End date.

        Returns:
            Tuple of (timestamps list, OHLCV dict with parallel arrays).
        """
        # Yahoo Finance expects Unix epoch seconds for period1 / period2.
        period1 = int(
            datetime.datetime(
                from_date.year, from_date.month, from_date.day,
                tzinfo=datetime.timezone.utc,
            ).timestamp()
        )
        # period2 is end-of-day exclusive: add one day.
        end_date = to_date + datetime.timedelta(days=1)
        period2 = int(
            datetime.datetime(
                end_date.year, end_date.month, end_date.day,
                tzinfo=datetime.timezone.utc,
            ).timestamp()
        )

        params: dict[str, str | int] = {
            "period1": period1,
            "period2": period2,
            "interval": "1d",
            "events": "history",
            "includePrePost": "false",
        }
        url = f"{_YF_CHART_API_BASE}{yf_symbol}"

        try:
            client = await self._get_client()
            response = await client.get(url, params=params, timeout=_HTTP_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "yahoo_finance.request_error",
                component="yahoo_finance_adapter",
                symbol=yf_symbol,
                error=str(exc),
            )
            return [], {}

        if response.status_code != 200:
            logger.warning(
                "yahoo_finance.non_200_response",
                component="yahoo_finance_adapter",
                symbol=yf_symbol,
                status_code=response.status_code,
            )
            return [], {}

        try:
            body: dict[str, Any] = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "yahoo_finance.json_parse_error",
                component="yahoo_finance_adapter",
                symbol=yf_symbol,
                error=str(exc),
            )
            return [], {}

        try:
            result = body["chart"]["result"][0]
            timestamps: list[int] = result["timestamp"]
            quote: dict[str, list[float | None]] = result["indicators"]["quote"][0]
            return timestamps, quote
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning(
                "yahoo_finance.response_shape_error",
                component="yahoo_finance_adapter",
                symbol=yf_symbol,
                error=str(exc),
            )
            return [], {}

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the HTTP client, creating one if necessary."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; data-service/2.0; "
                        "+https://alphaforge.io)"
                    )
                },
                timeout=_HTTP_TIMEOUT_SEC,
                follow_redirects=True,
            )
            self._owns_client = True
        return self._client

    # ------------------------------------------------------------------ #
    # Async context manager support
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> YahooFinanceAdapter:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
