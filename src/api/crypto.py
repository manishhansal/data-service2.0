"""
src/api/crypto.py

Binance Crypto REST API endpoints for DATA-SERVICE 2.0.

Task 11.3 — Requirements 13.1, 13.7, 13.8

Implemented endpoints:

``GET /v1/crypto/{symbol}/ohlcv``
    OHLCV candlestick data for a Binance symbol.  Accepts ``interval``
    (required), ``limit`` (1–1000, default 500), ``from`` and ``to``
    (optional ISO-8601 datetime strings).  Validates interval against
    ``BINANCE_INTERVALS``.  Returns a list of normalised
    ``BinanceCandleRecord`` objects.  The ``3m`` interval is valid for
    crypto — it is only banned for Indian market data.

``GET /v1/crypto/{symbol}/ticker``
    Current ticker price for a Binance symbol.  Returns
    ``{"symbol", "price", "metadata"}``.

``GET /v1/crypto/{symbol}/stats``
    24-hour rolling statistics for a Binance symbol as returned by the
    Binance ``/api/v3/ticker/24hr`` endpoint.

``GET /v1/crypto/exchange-info``
    Binance exchange info.  Optional ``?symbol=BTCUSDT`` query parameter
    to filter the (large) response to a single symbol.

``GET /v1/crypto/futures/overview``
    Perpetual futures overview for all tracked symbols (BTC, ETH, SOL):
    mark price, funding rate, funding rate annualised, next funding time,
    open interest, open interest notional USD, OI change pct 1h,
    long/short ratio, long account, short account
    (Requirement 13.7).

All responses use the canonical success envelope:
    {"data": <payload>, "metadata": {...}}

All error responses use the canonical error envelope:
    {"error": {"code": <str>, "message": <str>, "requestId": <str>}}

Note on the ``3m`` interval
---------------------------
The ``3m`` interval is **NOT** rejected here.  The ban on ``3m`` applies
exclusively to Indian market (NSE/NSE-F&O) data.  Binance natively supports
3m candlesticks and this adapter explicitly allows it (Requirement 13.1).

Requirements: 13.1, 13.7, 13.8
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from src.providers.adapters.binance_rest import BINANCE_INTERVALS, BinanceClient
from src.providers.binance_normaliser import BinanceOHLCVNormaliser

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Tracked crypto symbols for the futures overview
# ---------------------------------------------------------------------------

_TRACKED_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# ---------------------------------------------------------------------------
# JSON response helpers (mirrors the india.py / instruments.py pattern)
# ---------------------------------------------------------------------------


def _json_response(content: Any, *, status_code: int = 200) -> Response:
    body = json.dumps(content)
    return Response(content=body, status_code=status_code, media_type="application/json")


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _request_id() -> str:
    return str(uuid.uuid4())


def _success_envelope(
    data: Any,
    *,
    data_as_of: Optional[str] = None,
    provider: Optional[str] = None,
    data_source_type: str = "LIVE",
) -> dict[str, Any]:
    return {
        "data": data,
        "metadata": {
            "requestedAt": _utc_iso_now(),
            "dataAsOf": data_as_of or _utc_iso_now(),
            "dataSourceType": data_source_type,
            "provider": provider,
        },
    }


def _error_envelope(
    code: str,
    message: str,
    *,
    request_id: Optional[str] = None,
    provider: Optional[str] = None,
    retry_after_ms: Optional[int] = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "provider": provider,
            "retryAfterMs": retry_after_ms,
            "requestId": request_id or _request_id(),
        }
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()

# ---------------------------------------------------------------------------
# Helper: resolve BinanceClient from app state or lazily create one
# ---------------------------------------------------------------------------


def _get_binance_client(request: Request) -> BinanceClient:
    """Resolve the BinanceClient from application state.

    If not yet attached (e.g. during testing or incremental development),
    a default instance is created on-demand and cached on ``app.state``.
    """
    client = getattr(request.app.state, "binance_client", None)
    if client is None:
        client = BinanceClient()
        request.app.state.binance_client = client
    return client


# ---------------------------------------------------------------------------
# Helper: parse ISO-8601 datetime string (date-only or full datetime)
# ---------------------------------------------------------------------------


def _parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware UTC ``datetime``.

    Accepts both date-only strings (``"2024-01-01"``) and full ISO-8601
    datetimes (``"2024-01-01T00:00:00Z"``).

    Raises:
        ValueError: When ``value`` cannot be parsed.
    """
    # Try full datetime with timezone first.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # Try date-only.
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    raise ValueError(f"Cannot parse ISO-8601 datetime: {value!r}")


# ---------------------------------------------------------------------------
# Normaliser singleton (stateless — safe to share across requests)
# ---------------------------------------------------------------------------

_normaliser = BinanceOHLCVNormaliser()

# ---------------------------------------------------------------------------
# GET /v1/crypto/{symbol}/ohlcv
# ---------------------------------------------------------------------------


@router.get(
    "/crypto/{symbol}/ohlcv",
    summary="Binance OHLCV candlestick data",
    description=(
        "Returns normalised OHLCV candles for the given Binance symbol. "
        "The ``3m`` interval is valid for crypto (Binance natively supports it). "
        "Parameters: interval (required), limit (1–1000, default 500), "
        "from/to as optional ISO-8601 datetime strings. "
        "Returns normalised BinanceCandleRecord objects in the canonical envelope. "
        "HTTP 400 is returned for intervals not in BINANCE_INTERVALS. "
        "(Requirements 13.1, 13.8)"
    ),
    response_class=Response,
)
async def get_crypto_ohlcv(
    request: Request,
    symbol: str,
    interval: Annotated[
        str,
        Query(
            description=(
                "Kline interval. Supported values: "
                + ", ".join(sorted(BINANCE_INTERVALS))
                + ". Note: 3m is valid for Binance crypto data."
            )
        ),
    ],
    limit: Annotated[
        int,
        Query(
            description="Number of candles to return (1–1000). Default: 500.",
            ge=1,
            le=1000,
        ),
    ] = 500,
    from_date: Annotated[
        Optional[str],
        Query(
            alias="from",
            description=(
                "Start of date range as UTC ISO-8601 string "
                "(e.g. 2024-01-01 or 2024-01-01T00:00:00Z)."
            ),
        ),
    ] = None,
    to_date: Annotated[
        Optional[str],
        Query(
            alias="to",
            description=(
                "End of date range as UTC ISO-8601 string (e.g. 2024-01-31)."
            ),
        ),
    ] = None,
) -> Response:
    """Return normalised Binance OHLCV candles for *symbol*.

    - Validates ``interval`` against ``BINANCE_INTERVALS``; returns HTTP 400
      for unsupported intervals.
    - The ``3m`` interval is **not** rejected here — it is only banned for
      Indian market data.
    - Optional ``from``/``to`` parameters are converted to UTC epoch
      milliseconds and forwarded to the Binance klines endpoint.
    - Returns normalised ``BinanceCandleRecord`` objects via the canonical
      success envelope.
    """
    req_id = _request_id()

    # ── Validate interval ──────────────────────────────────────────────────
    # 3m is explicitly allowed for Binance crypto (Requirement 13.1).
    if interval not in BINANCE_INTERVALS:
        return _json_response(
            _error_envelope(
                "INTERVAL_NOT_SUPPORTED",
                f"interval '{interval}' is not a supported Binance interval. "
                f"Supported intervals: {', '.join(sorted(BINANCE_INTERVALS))}.",
                request_id=req_id,
                provider="binance",
            ),
            status_code=400,
        )

    # ── Parse optional from/to timestamps ─────────────────────────────────
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None

    if from_date is not None:
        try:
            from_dt = _parse_iso_datetime(from_date)
            start_ms = int(from_dt.timestamp() * 1000)
        except ValueError:
            return _json_response(
                _error_envelope(
                    "INVALID_PARAMETER",
                    f"Invalid 'from' date: '{from_date}'. "
                    "Expected ISO-8601 format (e.g. 2024-01-01 or 2024-01-01T00:00:00Z).",
                    request_id=req_id,
                ),
                status_code=400,
            )

    if to_date is not None:
        try:
            to_dt = _parse_iso_datetime(to_date)
            end_ms = int(to_dt.timestamp() * 1000)
        except ValueError:
            return _json_response(
                _error_envelope(
                    "INVALID_PARAMETER",
                    f"Invalid 'to' date: '{to_date}'. "
                    "Expected ISO-8601 format (e.g. 2024-03-01 or 2024-03-01T00:00:00Z).",
                    request_id=req_id,
                ),
                status_code=400,
            )

    # ── Validate range ordering when both are supplied ────────────────────
    if start_ms is not None and end_ms is not None and start_ms >= end_ms:
        return _json_response(
            _error_envelope(
                "INVALID_PARAMETER",
                "'from' must be earlier than 'to'.",
                request_id=req_id,
            ),
            status_code=400,
        )

    # ── Fetch from Binance ─────────────────────────────────────────────────
    client = _get_binance_client(request)
    symbol_upper = _normalize_binance_symbol(symbol)

    try:
        raw_candles = await client.get_klines(
            symbol=symbol_upper,
            interval=interval,
            limit=limit,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "crypto_api.ohlcv_fetch_failed",
            component="crypto_api",
            symbol=symbol_upper,
            interval=interval,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch OHLCV data for {symbol_upper}: {exc}",
                request_id=req_id,
                provider="binance",
            ),
            status_code=502,
        )

    # ── Normalise batch ────────────────────────────────────────────────────
    records = _normaliser.normalise_batch(raw_candles, symbol=symbol_upper, interval=interval)

    # Serialise BinanceCandleRecord objects to dicts.
    data = [r.model_dump() for r in records]
    data_as_of = _utc_iso_now()
    if data:
        # Use the closeTime of the last candle as dataAsOf.
        data_as_of = datetime.fromtimestamp(
            records[-1].closeTime / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    return _json_response(
        _success_envelope(
            data,
            provider="binance",
            data_source_type="HISTORICAL" if (start_ms or end_ms) else "LIVE",
            data_as_of=data_as_of,
        )
    )


# ---------------------------------------------------------------------------
# Symbol normalisation helper
# ---------------------------------------------------------------------------

# Known Binance quote currencies in priority order.  When a caller supplies a
# bare base asset (e.g. "BTC" or "sol") we append the default quote currency
# so that the Binance REST API receives a valid trading-pair symbol.
_KNOWN_QUOTE_CURRENCIES: tuple[str, ...] = (
    "USDT", "USDC", "BUSD", "BTC", "ETH", "BNB",
)
_DEFAULT_QUOTE_CURRENCY = "USDT"


def _normalize_binance_symbol(symbol: str) -> str:
    """Return a valid Binance trading-pair symbol from *symbol*.

    If *symbol* already ends with a known quote currency it is returned as-is
    (uppercased).  Otherwise ``USDT`` is appended so that a caller passing
    ``"BTC"`` or ``"sol"`` gets ``"BTCUSDT"``/``"SOLUSDT"`` respectively.

    Examples::

        _normalize_binance_symbol("BTC")     -> "BTCUSDT"
        _normalize_binance_symbol("btc")     -> "BTCUSDT"
        _normalize_binance_symbol("BTCUSDT") -> "BTCUSDT"
        _normalize_binance_symbol("ETHBTC")  -> "ETHBTC"
    """
    upper = symbol.upper()
    # Require at least one character before the quote suffix so that bare
    # quote-currency names like "BTC" or "ETH" are not mistakenly treated as
    # already-complete trading pairs (they would be, e.g., "ETHBTC" or "XRPETH"
    # where the quote portion is preceded by a base asset).
    if any(upper.endswith(q) and len(upper) > len(q) for q in _KNOWN_QUOTE_CURRENCIES):
        return upper
    return upper + _DEFAULT_QUOTE_CURRENCY


# ---------------------------------------------------------------------------
# GET /v1/crypto/{symbol}/ticker
# ---------------------------------------------------------------------------


@router.get(
    "/crypto/{symbol}/ticker",
    summary="Current Binance ticker price",
    description=(
        "Returns the current ticker price for the given Binance symbol. "
        "Response shape: {symbol, price, metadata}. "
        "(Requirement 13.1)"
    ),
    response_class=Response,
)
async def get_crypto_ticker(
    request: Request,
    symbol: str,
) -> Response:
    """Return the current Binance spot price for *symbol*.

    Response ``data`` shape::

        {"symbol": "BTCUSDT", "price": "65123.45000000"}
    """
    req_id = _request_id()
    client = _get_binance_client(request)
    symbol_upper = _normalize_binance_symbol(symbol)

    try:
        ticker = await client.get_ticker_price(symbol_upper)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "crypto_api.ticker_fetch_failed",
            component="crypto_api",
            symbol=symbol_upper,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch ticker for {symbol_upper}: {exc}",
                request_id=req_id,
                provider="binance",
            ),
            status_code=502,
        )

    return _json_response(
        _success_envelope(ticker, provider="binance", data_source_type="LIVE")
    )


# ---------------------------------------------------------------------------
# GET /v1/crypto/{symbol}/stats
# ---------------------------------------------------------------------------


@router.get(
    "/crypto/{symbol}/stats",
    summary="Binance 24-hour rolling statistics",
    description=(
        "Returns the 24-hour rolling window statistics for the given Binance "
        "symbol as returned by /api/v3/ticker/24hr. "
        "(Requirement 13.1)"
    ),
    response_class=Response,
)
async def get_crypto_stats(
    request: Request,
    symbol: str,
) -> Response:
    """Return the 24-hour rolling statistics for *symbol*.

    The full Binance 24hr stats dict is returned inside the canonical
    envelope, including ``openPrice``, ``highPrice``, ``lowPrice``,
    ``lastPrice``, ``volume``, ``priceChange``, ``priceChangePercent``, etc.
    """
    req_id = _request_id()
    client = _get_binance_client(request)
    symbol_upper = _normalize_binance_symbol(symbol)

    try:
        stats = await client.get_24hr_stats(symbol_upper)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "crypto_api.stats_fetch_failed",
            component="crypto_api",
            symbol=symbol_upper,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch 24hr stats for {symbol_upper}: {exc}",
                request_id=req_id,
                provider="binance",
            ),
            status_code=502,
        )

    return _json_response(
        _success_envelope(stats, provider="binance", data_source_type="LIVE")
    )


# ---------------------------------------------------------------------------
# GET /v1/crypto/exchange-info
# ---------------------------------------------------------------------------


@router.get(
    "/crypto/exchange-info",
    summary="Binance exchange information",
    description=(
        "Returns Binance exchange metadata. "
        "Use the optional `?symbol=BTCUSDT` query parameter to filter to a "
        "single symbol (avoids the large full-exchange payload). "
        "(Requirement 13.1)"
    ),
    response_class=Response,
)
async def get_exchange_info(
    request: Request,
    symbol: Annotated[
        Optional[str],
        Query(description="Optional symbol filter (e.g. BTCUSDT)."),
    ] = None,
) -> Response:
    """Return Binance exchange information.

    When ``symbol`` is omitted the full exchange info is returned, which
    includes metadata for all listed trading pairs.  This is a large
    response; supply ``?symbol=`` to narrow it to one symbol.
    """
    req_id = _request_id()
    client = _get_binance_client(request)
    symbol_upper = symbol.upper() if symbol else None

    try:
        info = await client.get_exchange_info(symbol=symbol_upper)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "crypto_api.exchange_info_fetch_failed",
            component="crypto_api",
            symbol=symbol_upper,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch exchange info: {exc}",
                request_id=req_id,
                provider="binance",
            ),
            status_code=502,
        )

    return _json_response(
        _success_envelope(info, provider="binance", data_source_type="LIVE")
    )


# ---------------------------------------------------------------------------
# GET /v1/crypto/futures/overview
# ---------------------------------------------------------------------------


@router.get(
    "/crypto/futures/overview",
    summary="Binance perpetual futures overview",
    description=(
        "Returns a combined perpetual-futures snapshot for all tracked "
        "symbols (BTCUSDT, ETHUSDT, SOLUSDT): mark price, funding rate, "
        "funding rate annualised, next funding time, open interest (contracts), "
        "open interest notional USD, OI change pct 1h, long/short ratio, "
        "long account, short account. "
        "(Requirement 13.7)"
    ),
    response_class=Response,
)
async def get_futures_overview(
    request: Request,
) -> Response:
    """Return perpetual futures overview for all tracked symbols.

    Combines data from:
    - ``/fapi/v1/premiumIndex`` (mark price, funding rate, next funding time)
    - ``/fapi/v1/openInterest`` (open interest in contracts)
    - ``/futures/data/globalLongShortAccountRatio`` (long/short ratio)
    - ``/futures/data/openInterestHist`` at 1h period (OI change)

    If any individual sub-fetch fails for a symbol, that symbol is included
    in the response with the available fields and any unavailable fields set
    to ``null`` to prevent a single failure from blocking the whole response.

    Per Requirement 13.9: if the Binance REST API fails after 3 consecutive
    attempts the client raises an exception; we catch it, set the symbol's
    data to a degraded state, and continue.
    """
    req_id = _request_id()
    client = _get_binance_client(request)
    overview: list[dict[str, Any]] = []

    for symbol in _TRACKED_SYMBOLS:
        symbol_data: dict[str, Any] = {
            "symbol": symbol,
            "markPrice": None,
            "indexPrice": None,
            "fundingRate": None,
            "fundingRateAnnualized": None,
            "nextFundingTime": None,
            "openInterest": None,
            "openInterestNotionalUsd": None,
            "oiChangePct1h": None,
            "longShortRatio": None,
            "longAccount": None,
            "shortAccount": None,
        }

        # ── Mark price + funding rate ─────────────────────────────────────
        try:
            mark_data = await client.get_futures_mark_price(symbol)
            symbol_data["markPrice"] = mark_data.get("markPrice")
            symbol_data["indexPrice"] = mark_data.get("indexPrice")
            funding_rate_raw = mark_data.get("lastFundingRate")
            if funding_rate_raw is not None:
                try:
                    rate = float(funding_rate_raw)
                    symbol_data["fundingRate"] = rate
                    # Annualise: 3 funding periods per day × 365 days
                    symbol_data["fundingRateAnnualized"] = rate * 3 * 365
                except (TypeError, ValueError):
                    symbol_data["fundingRate"] = funding_rate_raw
            next_funding_time = mark_data.get("nextFundingTime")
            if next_funding_time is not None:
                try:
                    symbol_data["nextFundingTime"] = datetime.fromtimestamp(
                        int(next_funding_time) / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                except (TypeError, ValueError, OSError):
                    symbol_data["nextFundingTime"] = next_funding_time
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "crypto_api.futures_mark_price_failed",
                component="crypto_api",
                symbol=symbol,
                error=str(exc),
            )

        # ── Open interest ─────────────────────────────────────────────────
        try:
            oi_data = await client.get_futures_open_interest(symbol)
            symbol_data["openInterest"] = oi_data.get("openInterest")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "crypto_api.futures_oi_failed",
                component="crypto_api",
                symbol=symbol,
                error=str(exc),
            )

        # ── Open interest history (for oiChangePct1h) ─────────────────────
        try:
            oi_hist = await client.get_futures_oi_history(symbol, period="1h", limit=2)
            if oi_hist and len(oi_hist) >= 2:
                oi_now_raw = oi_hist[-1].get("sumOpenInterest")
                oi_prev_raw = oi_hist[-2].get("sumOpenInterest")
                if oi_now_raw is not None and oi_prev_raw is not None:
                    oi_now = float(oi_now_raw)
                    oi_prev = float(oi_prev_raw)
                    if oi_prev != 0:
                        symbol_data["oiChangePct1h"] = round(
                            (oi_now - oi_prev) / abs(oi_prev) * 100, 4
                        )
                # Use the latest notional USD if not already set
                if symbol_data.get("openInterestNotionalUsd") is None:
                    notional = oi_hist[-1].get("sumOpenInterestValue")
                    if notional is not None:
                        symbol_data["openInterestNotionalUsd"] = notional
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "crypto_api.futures_oi_history_failed",
                component="crypto_api",
                symbol=symbol,
                error=str(exc),
            )

        # ── Long/short account ratio ───────────────────────────────────────
        try:
            ls_data = await client.get_long_short_ratio(symbol, period="5m", limit=1)
            if ls_data:
                latest = ls_data[-1]
                symbol_data["longShortRatio"] = latest.get("longShortRatio")
                symbol_data["longAccount"] = latest.get("longAccount")
                symbol_data["shortAccount"] = latest.get("shortAccount")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "crypto_api.futures_long_short_failed",
                component="crypto_api",
                symbol=symbol,
                error=str(exc),
            )

        overview.append(symbol_data)

    return _json_response(
        _success_envelope(overview, provider="binance", data_source_type="LIVE")
    )


# ---------------------------------------------------------------------------
# Delta Exchange endpoints  (DS2-RCA-001 fix)
# ---------------------------------------------------------------------------
# AlphaForge's default broker is Delta Exchange India.  These endpoints allow
# AlphaForge to retrieve Delta data through DATA-SERVICE instead of calling
# the Delta API directly.
#
# Tracked Delta symbols (INR-settled perpetuals):
#   BTCUSD  ETHUSD  SOLUSD   (NOT USDT-quoted — Delta India uses USD)
# ---------------------------------------------------------------------------

_DELTA_TRACKED_SYMBOLS: tuple[str, ...] = ("BTCUSD", "ETHUSD", "SOLUSD")

from src.providers.adapters.delta_exchange import DeltaClient, DELTA_INTERVALS  # noqa: E402
from src.providers.delta_normaliser import DeltaOHLCVNormaliser  # noqa: E402

_delta_normaliser = DeltaOHLCVNormaliser()


def _get_delta_client(request: Request) -> DeltaClient:
    """Resolve the DeltaClient from application state (lazy-created)."""
    client = getattr(request.app.state, "delta_client", None)
    if client is None:
        client = DeltaClient()
        request.app.state.delta_client = client
    return client


# ---------------------------------------------------------------------------
# GET /v1/delta/{symbol}/ohlcv
# ---------------------------------------------------------------------------


@router.get(
    "/delta/{symbol}/ohlcv",
    summary="Delta Exchange OHLCV candles",
    description=(
        "Returns normalised OHLCV candles for the given Delta Exchange symbol. "
        "Use Delta-style symbols: BTCUSD, ETHUSD, SOLUSD (not USDT-quoted). "
        "The ``3m`` interval is valid for Delta crypto. "
        "Parameters: interval (required), limit (1–2000, default 100), "
        "from/to as optional ISO-8601 datetime strings. "
        "(DS2-RCA-001 fix)"
    ),
    response_class=Response,
)
async def get_delta_ohlcv(
    request: Request,
    symbol: str,
    interval: Annotated[
        str,
        Query(
            description=(
                "Candle interval. Supported: "
                + ", ".join(sorted(DELTA_INTERVALS))
                + ". 3m is valid for crypto."
            )
        ),
    ],
    limit: Annotated[
        int,
        Query(description="Number of candles (1–2000). Default: 100.", ge=1, le=2000),
    ] = 100,
    from_date: Annotated[
        Optional[str],
        Query(alias="from", description="Start as UTC ISO-8601 (e.g. 2024-01-01)."),
    ] = None,
    to_date: Annotated[
        Optional[str],
        Query(alias="to", description="End as UTC ISO-8601 (e.g. 2024-03-01)."),
    ] = None,
) -> Response:
    """Return normalised Delta Exchange OHLCV candles for *symbol*."""
    req_id = _request_id()

    if interval not in DELTA_INTERVALS:
        return _json_response(
            _error_envelope(
                "INTERVAL_NOT_SUPPORTED",
                f"interval '{interval}' is not a supported Delta interval. "
                f"Supported: {', '.join(sorted(DELTA_INTERVALS))}.",
                request_id=req_id,
                provider="delta",
            ),
            status_code=400,
        )

    import time as _t  # noqa: PLC0415
    from src.providers.adapters.delta_exchange import _INTERVAL_SECONDS  # noqa: PLC0415

    # Parse from/to or derive from limit.
    end_sec = int(_t.time())
    interval_sec = _INTERVAL_SECONDS.get(interval, 60)
    start_sec = end_sec - (limit + 2) * interval_sec  # default window

    if from_date is not None:
        try:
            from_dt = _parse_iso_datetime(from_date)
            start_sec = int(from_dt.timestamp())
        except ValueError:
            return _json_response(
                _error_envelope("INVALID_PARAMETER", f"Invalid 'from': '{from_date}'", request_id=req_id),
                status_code=400,
            )

    if to_date is not None:
        try:
            to_dt = _parse_iso_datetime(to_date)
            end_sec = int(to_dt.timestamp())
        except ValueError:
            return _json_response(
                _error_envelope("INVALID_PARAMETER", f"Invalid 'to': '{to_date}'", request_id=req_id),
                status_code=400,
            )

    if start_sec >= end_sec:
        return _json_response(
            _error_envelope("INVALID_PARAMETER", "'from' must be earlier than 'to'.", request_id=req_id),
            status_code=400,
        )

    client = _get_delta_client(request)
    symbol_upper = symbol.upper()

    try:
        raw_candles = await client.get_candles(
            symbol=symbol_upper,
            interval=interval,
            start_sec=start_sec,
            end_sec=end_sec,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "delta_api.ohlcv_fetch_failed",
            component="delta_api",
            symbol=symbol_upper,
            interval=interval,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch Delta candles for {symbol_upper}: {exc}",
                request_id=req_id,
                provider="delta",
            ),
            status_code=502,
        )

    records = _delta_normaliser.normalise_batch(raw_candles, symbol=symbol_upper, interval=interval)
    # Trim to requested limit (take most recent).
    records = records[-limit:] if len(records) > limit else records
    data = [r.model_dump() for r in records]

    data_as_of = _utc_iso_now()
    if data:
        data_as_of = datetime.fromtimestamp(
            records[-1].closeTime / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    return _json_response(
        _success_envelope(
            data,
            provider="delta",
            data_source_type="HISTORICAL" if from_date else "LIVE",
            data_as_of=data_as_of,
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/delta/{symbol}/ticker
# ---------------------------------------------------------------------------


@router.get(
    "/delta/{symbol}/ticker",
    summary="Delta Exchange ticker",
    description=(
        "Returns the current normalised ticker for the given Delta Exchange symbol. "
        "Use Delta-style symbols: BTCUSD, ETHUSD, SOLUSD. "
        "(DS2-RCA-001 fix)"
    ),
    response_class=Response,
)
async def get_delta_ticker(request: Request, symbol: str) -> Response:
    """Return a normalised Delta Exchange ticker for *symbol*."""
    req_id = _request_id()
    client = _get_delta_client(request)
    symbol_upper = symbol.upper()

    try:
        ticker = await client.get_ticker(symbol_upper)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "delta_api.ticker_fetch_failed",
            component="delta_api",
            symbol=symbol_upper,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch Delta ticker for {symbol_upper}: {exc}",
                request_id=req_id,
                provider="delta",
            ),
            status_code=502,
        )

    return _json_response(
        _success_envelope(ticker, provider="delta", data_source_type="LIVE")
    )


# ---------------------------------------------------------------------------
# GET /v1/delta/futures/overview
# ---------------------------------------------------------------------------


@router.get(
    "/delta/futures/overview",
    summary="Delta Exchange futures overview",
    description=(
        "Returns perpetual futures overview for BTCUSD, ETHUSD, SOLUSD on "
        "Delta Exchange India: mark price, funding rate, OI, OI change 1h. "
        "Note: Delta India does NOT provide a long/short ratio endpoint — "
        "longShortRatio / longAccount / shortAccount will be null. "
        "(DS2-RCA-001 fix)"
    ),
    response_class=Response,
)
async def get_delta_futures_overview(request: Request) -> Response:
    """Return Delta Exchange perpetual futures overview for tracked symbols."""
    client = _get_delta_client(request)
    overview = []

    for symbol in _DELTA_TRACKED_SYMBOLS:
        symbol_data: dict[str, Any] = {
            "symbol":                   symbol,
            "exchange":                 "DELTA",
            "markPrice":                None,
            "indexPrice":               None,
            "fundingRate":              None,
            "fundingRateAnnualized":    None,
            "nextFundingTime":          0,
            "openInterest":             None,
            "openInterestNotionalUsd":  None,
            "oiChangePct1h":            None,
            "longShortRatio":           None,   # Unavailable on Delta India
            "longAccount":              None,   # Unavailable on Delta India
            "shortAccount":             None,   # Unavailable on Delta India
        }

        # Mark price + funding rate.
        try:
            premium = await client.get_premium_index(symbol)
            symbol_data.update({
                "markPrice":             premium.get("markPrice"),
                "indexPrice":            premium.get("indexPrice"),
                "fundingRate":           premium.get("fundingRate"),
                "fundingRateAnnualized": premium.get("fundingRateAnnualized"),
                "nextFundingTime":       premium.get("nextFundingTime", 0),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("delta_api.premium_index_failed", symbol=symbol, error=str(exc))

        # OI from ticker.
        try:
            ticker = await client.get_ticker(symbol)
            symbol_data["openInterest"] = ticker.get("openInterest")
            mark = symbol_data.get("markPrice") or ticker.get("markPrice") or 0
            oi = symbol_data.get("openInterest") or 0
            if mark and oi:
                symbol_data["openInterestNotionalUsd"] = float(oi) * float(mark)
        except Exception as exc:  # noqa: BLE001
            logger.warning("delta_api.oi_fetch_failed", symbol=symbol, error=str(exc))

        # OI history for 1h change.
        try:
            oi_hist = await client.get_oi_history(symbol, interval="1h", limit=2)
            if len(oi_hist) >= 2:
                oi_now = float(oi_hist[-1]["openInterest"])
                oi_prev = float(oi_hist[-2]["openInterest"])
                if oi_prev != 0:
                    symbol_data["oiChangePct1h"] = round(
                        (oi_now - oi_prev) / abs(oi_prev) * 100, 4
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("delta_api.oi_history_failed", symbol=symbol, error=str(exc))

        overview.append(symbol_data)

    return _json_response(
        _success_envelope(overview, provider="delta", data_source_type="LIVE")
    )
