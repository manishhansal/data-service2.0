"""
src/api/india.py

India Markets REST API endpoints for DATA-SERVICE 2.0.

Implemented endpoints (Tasks 6.3, 6.4, 6.5, 7.3):

``GET /v1/india/quotes/{symbol}``
    Live quote for a single instrument (Requirement 3.1).

``GET /v1/india/option-chain``
    Option chain snapshot for a given underlying and optional expiry
    (Requirement 3.7, 3.9).

``GET /v1/india/market/status``
    Current NSE session phase, next phase change, trading day information,
    upcoming holidays, and calendar status (Requirement 12.6).

``GET /v1/india/historical/gaps``
    List detected candle-sequence gaps with optional filters for symbol,
    interval, status, and limit (Requirements 4.7, 10.8).

All responses use the canonical success envelope:
    {"data": <payload>, "metadata": {...}}

All error responses use the canonical error envelope:
    {"error": {"code": <str>, "message": <str>, "requestId": <str>}}

Requirements: 3.1, 3.4, 3.7, 3.8, 3.9, 3.10, 12.5, 12.6, 4.7, 10.8
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Optional
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from src.core.schemas.instrument import SessionPhase

_IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# JSON response helpers (mirror the instruments.py pattern)
# ---------------------------------------------------------------------------


class _DateAwareEncoder(json.JSONEncoder):
    """Extend stdlib JSONEncoder to serialise ``date`` and ``datetime`` objects."""

    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if isinstance(o, date):
            return o.isoformat()
        return super().default(o)


def _json_response(content: Any, *, status_code: int = 200) -> Response:
    body = json.dumps(content, cls=_DateAwareEncoder)
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
    market_status: Optional[str] = None,
    quality: Optional[dict] = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "requestedAt": _utc_iso_now(),
        "dataAsOf": data_as_of or _utc_iso_now(),
        "dataSourceType": data_source_type,
        "provider": provider,
    }
    if market_status is not None:
        meta["marketStatus"] = market_status
    if quality is not None:
        meta["quality"] = quality
    return {"data": data, "metadata": meta}


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
_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helper: resolve MarketEngine from app state or create a default one
# ---------------------------------------------------------------------------


def _get_market_engine(request: Request):  # type: ignore[return]
    """Resolve the MarketEngine from application state.

    If not yet attached (e.g. during testing or incremental development),
    a default instance is created on-demand and cached on ``app.state``.
    """
    engine = getattr(request.app.state, "market_engine", None)
    if engine is None:
        from src.engines.market_engine import MarketEngine  # noqa: PLC0415

        engine = MarketEngine()
        request.app.state.market_engine = engine
    return engine


def _get_session_engine(request: Request):  # type: ignore[return]
    """Resolve the MarketSessionEngine.

    Prefers the one embedded in the MarketEngine; falls back to a default.
    """
    market_engine = getattr(request.app.state, "market_engine", None)
    if market_engine is not None and hasattr(market_engine, "_session_engine"):
        return market_engine._session_engine

    session_engine = getattr(request.app.state, "session_engine", None)
    if session_engine is None:
        from src.engines.market_session import MarketSessionEngine  # noqa: PLC0415

        session_engine = MarketSessionEngine()
        request.app.state.session_engine = session_engine
    return session_engine


def _get_holiday_calendar(request: Request):  # type: ignore[return]
    """Resolve the HolidayCalendar from app state or the global singleton."""
    calendar = getattr(request.app.state, "holiday_calendar", None)
    if calendar is None:
        try:
            from src.engines.holiday_calendar import HolidayCalendar  # noqa: PLC0415

            calendar = HolidayCalendar.get_instance()
        except Exception:  # noqa: BLE001
            calendar = None
    return calendar


# ---------------------------------------------------------------------------
# GET /v1/india/quotes/batch — batch quote for multiple symbols
# NOTE: Must be registered BEFORE /india/quotes/{symbol} so FastAPI does not
#       match the literal path segment "batch" as the {symbol} path parameter.
# ---------------------------------------------------------------------------


@router.get(
    "/india/quotes/batch",
    summary="Live quotes for multiple instruments",
    description=(
        "Returns live quotes for a comma-separated list of symbols. "
        "Symbols with no data return null in the corresponding slot. "
        "Maximum 200 symbols per request."
    ),
    response_class=Response,
)
async def get_batch_quotes(
    request: Request,
    symbols: Annotated[
        str,
        Query(description="Comma-separated NSE symbols (e.g. RELIANCE,NIFTY,HDFCBANK)"),
    ],
    exchange: Annotated[
        str,
        Query(description="Exchange identifier (default: NSE)"),
    ] = "NSE",
) -> Response:
    """Return live quotes for multiple symbols in one call."""
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        return _json_response(
            _error_envelope("INVALID_PARAMETER", "symbols is required", request_id=_request_id()),
            status_code=400,
        )
    if len(symbol_list) > 200:
        return _json_response(
            _error_envelope(
                "INVALID_PARAMETER",
                f"Too many symbols: {len(symbol_list)}. Maximum is 200.",
                request_id=_request_id(),
            ),
            status_code=400,
        )

    import asyncio  # noqa: PLC0415
    market_engine = _get_market_engine(request)

    async def _quote_one(sym: str) -> Optional[dict]:
        try:
            q = await market_engine.get_live_quote(sym, exchange=exchange)
            # Map to canonical MDQuote shape
            return {
                "symbol": sym,
                "token": None,
                "exchange": exchange.upper(),
                "name": q.get("name"),
                "ltp": q.get("ltp"),
                "change": q.get("change"),
                "changePct": q.get("changePct"),
                "prevClose": q.get("prevClose"),
                "open": q.get("open"),
                "high": q.get("high"),
                "low": q.get("low"),
                "volume": q.get("volume"),
                "oi": q.get("oi"),
                "weekHigh52": q.get("weekHigh52"),
                "weekLow52": q.get("weekLow52"),
                "upperCircuit": q.get("upperCircuit"),
                "lowerCircuit": q.get("lowerCircuit"),
                "totalBuyQty": q.get("totalBuyQty"),
                "totalSellQty": q.get("totalSellQty"),
                "lastTradeTime": q.get("lastTradeTime"),
                "provider": q.get("provenance", {}).get("source") or "angel_one",
                "fetchedAt": _utc_iso_now(),
                "marketStatus": q.get("marketStatus"),
            }
        except Exception:  # noqa: BLE001
            return None

    results = await asyncio.gather(*[_quote_one(s) for s in symbol_list])
    quotes = list(results)

    market_status = "UNKNOWN"
    for q in quotes:
        if q and q.get("marketStatus"):
            market_status = q["marketStatus"]
            break

    return _json_response(
        _success_envelope(
            {"quotes": quotes, "count": len(quotes)},
            data_source_type="LIVE" if market_status == SessionPhase.REGULAR.value else "CACHED",
            market_status=market_status,
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/india/quotes/{symbol}
# ---------------------------------------------------------------------------


@router.get(
    "/india/quotes/{symbol}",
    summary="Live quote for a single instrument",
    description=(
        "Returns a normalised live quote for the given symbol. "
        "When the market is not in REGULAR session, returns the last available "
        "quote with the current marketStatus. "
        "Never increments circuit-breaker counters for CLOSED/PRE_OPEN/POST_MARKET "
        "(Requirements 3.1, 3.2, 3.3, 3.4, 12.5)."
    ),
    response_class=Response,
)
async def get_live_quote(
    request: Request,
    symbol: str,
    exchange: Annotated[
        str,
        Query(description="Exchange identifier (default: NSE)"),
    ] = "NSE",
) -> Response:
    """Return a normalised live quote for *symbol* on *exchange*.

    - REGULAR session: fresh quote from provider.
    - Non-REGULAR session: last available quote (no older than 24 hours)
      with ``marketStatus`` reflecting the actual phase.
    """
    market_engine = _get_market_engine(request)
    quote = await market_engine.get_live_quote(symbol, exchange=exchange)

    market_status = quote.get("marketStatus", "UNKNOWN")
    data_source_type = "LIVE" if market_status == SessionPhase.REGULAR.value else "CACHED"

    return _json_response(
        _success_envelope(
            quote,
            provider=quote.get("provider"),
            data_source_type=data_source_type,
            market_status=market_status,
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/india/option-chain
# ---------------------------------------------------------------------------


@router.get(
    "/india/option-chain",
    summary="Option chain snapshot",
    description=(
        "Returns the option chain snapshot for the given underlying and optional expiry. "
        "When market is CLOSED: rows:[], marketStatus:'CLOSED' (not HTTP 5xx). "
        "When no contracts found: rows:[], marketStatus:'NO_DATA'. "
        "When spot price > 60s old: chainQuality:'DEGRADED' with spotAgeMs. "
        "(Requirements 3.7, 3.8, 3.9, 3.10)"
    ),
    response_class=Response,
)
async def get_option_chain(
    request: Request,
    underlying: Annotated[
        str,
        Query(description="Underlying symbol (e.g. NIFTY, BANKNIFTY)."),
    ],
    expiry: Annotated[
        Optional[str],
        Query(description="Expiry date as YYYY-MM-DD. Defaults to nearest expiry."),
    ] = None,
    exchange: Annotated[
        str,
        Query(description="Exchange identifier (default: NSE)."),
    ] = "NSE",
) -> Response:
    """Return a normalised option chain snapshot.

    Validates ``expiry`` as an ISO-8601 date when provided.
    Returns ``rows: []`` with appropriate ``marketStatus`` for CLOSED
    session or no-data conditions — never HTTP 5xx for these cases.
    """
    req_id = _request_id()

    # Validate expiry date if provided
    if expiry is not None:
        try:
            date.fromisoformat(expiry)
        except ValueError:
            return _json_response(
                _error_envelope(
                    "INVALID_PARAMETER",
                    f"Invalid expiry date '{expiry}'. Expected ISO-8601 format YYYY-MM-DD.",
                    request_id=req_id,
                ),
                status_code=400,
            )

    market_engine = _get_market_engine(request)
    chain = await market_engine.get_option_chain(
        underlying, expiry=expiry, exchange=exchange
    )

    market_status = chain.get("marketStatus", "UNKNOWN")
    data_source_type = "LIVE" if market_status not in (
        SessionPhase.CLOSED.value, "NO_DATA"
    ) else "CACHED"

    return _json_response(
        _success_envelope(
            chain,
            data_source_type=data_source_type,
            market_status=market_status,
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/india/market/status
# ---------------------------------------------------------------------------


@router.get(
    "/india/market/status",
    summary="NSE market session status",
    description=(
        "Returns the current NSE session phase, next phase change time, "
        "trading day information, remaining holidays in current month, "
        "and calendar status (Requirement 12.6)."
    ),
    response_class=Response,
)
async def get_market_status(request: Request) -> Response:
    """Return current NSE market status.

    Response fields:
    - ``sessionPhase``: Current session phase string.
    - ``nextSessionChange``: UTC ISO-8601 datetime of the next phase boundary.
    - ``tradingDay``: True if today (IST) has a REGULAR session.
    - ``nextTradingDay``: Next IST trading date as ``YYYY-MM-DD``.
    - ``holidays``: Remaining NSE holiday dates in the current IST calendar
      month as a list of ``YYYY-MM-DD`` strings.
    - ``calendarStatus``: Date of last successful holiday calendar refresh
      as ``YYYY-MM-DD``, or ``null`` if never refreshed.
    """
    session_engine = _get_session_engine(request)
    calendar = _get_holiday_calendar(request)

    now_utc = datetime.now(timezone.utc)

    # ── Current phase ─────────────────────────────────────────────────────
    current_phase: SessionPhase = session_engine.get_current_phase(now_utc)

    # ── Next phase change ─────────────────────────────────────────────────
    next_change_utc = session_engine.get_next_phase_change(now_utc)
    next_change_str = (
        next_change_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )

    # ── Trading day ───────────────────────────────────────────────────────
    is_trading_day = session_engine.is_trading_day(now_utc)

    # ── Next trading day ──────────────────────────────────────────────────
    next_td: date = session_engine.next_trading_day(now_utc)
    next_trading_day_str = next_td.isoformat()

    # ── Holidays in current IST month ─────────────────────────────────────
    ist_now = now_utc.astimezone(_IST)
    ist_today = ist_now.date()

    holidays_this_month: list[str] = []
    if calendar is not None:
        month_holidays = calendar.get_holidays_for_month(ist_today.year, ist_today.month)
        # Only upcoming/today holidays (inclusive of today)
        holidays_this_month = [
            h.isoformat() for h in month_holidays if h >= ist_today
        ]

    # ── Calendar status ───────────────────────────────────────────────────
    calendar_status: Optional[str] = None
    if calendar is not None:
        calendar_status = calendar.calendar_status

    status_data = {
        "sessionPhase": current_phase.value,
        "nextSessionChange": next_change_str,
        "tradingDay": is_trading_day,
        "nextTradingDay": next_trading_day_str,
        "holidays": holidays_this_month,
        "calendarStatus": calendar_status,
    }

    return _json_response(
        _success_envelope(
            status_data,
            data_source_type="LIVE",
            market_status=current_phase.value,
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/india/historical/reconciliation
# ---------------------------------------------------------------------------


@router.get(
    "/india/historical/reconciliation",
    summary="Cross-provider OHLCV reconciliation statistics",
    description=(
        "Returns aggregated reconciliation statistics for all cross-provider "
        "OHLCV comparisons: totalCompared, matched (CONFIRMED), matchRatePct, "
        "distribution (counts per status), and byProviderPair breakdown. "
        "(Requirements 10.5, 10.6, 10.7)"
    ),
    response_class=Response,
)
async def get_reconciliation_stats(request: Request) -> Response:
    """Return cross-provider OHLCV reconciliation statistics.

    Statistics are sourced from the in-process reconciliation store attached
    to the HistoricalEngine.  When no results have been recorded yet, all
    counters are zero.

    Response shape::

        {
          "data": {
            "totalCompared": int,
            "matched": int,           // CONFIRMED count
            "matchRatePct": float,    // 0.0–100.0
            "distribution": {
              "CONFIRMED": int,
              "MINOR_DISCREPANCY": int,
              "MAJOR_DISCREPANCY": int
            },
            "byProviderPair": {
              "<provider_a>/<provider_b>": {
                "totalCompared": int,
                "matched": int,
                "matchRatePct": float,
                "distribution": { ... }
              },
              ...
            }
          },
          "metadata": { ... }
        }
    """
    historical_engine = getattr(request.app.state, "historical_engine", None)
    if historical_engine is None:
        from src.engines.historical_engine import HistoricalEngine  # noqa: PLC0415
        historical_engine = HistoricalEngine()
        request.app.state.historical_engine = historical_engine

    stats = historical_engine.get_reconciliation_stats()
    return _json_response(
        _success_envelope(stats, data_source_type="DERIVED")
    )


# ---------------------------------------------------------------------------
# Helper: resolve GapRecoveryEngine from app state
# ---------------------------------------------------------------------------


def _get_gap_recovery_engine(request: Request):  # type: ignore[return]
    """Resolve the GapRecoveryEngine from application state.

    If not yet attached, a default instance is created and cached on
    ``app.state``.
    """
    engine = getattr(request.app.state, "gap_recovery_engine", None)
    if engine is None:
        from src.engines.gap_recovery import GapRecoveryEngine  # noqa: PLC0415

        engine = GapRecoveryEngine()
        request.app.state.gap_recovery_engine = engine
    return engine


# ---------------------------------------------------------------------------
# GET /v1/india/historical/gaps
# ---------------------------------------------------------------------------

_GAP_MAX_LIMIT = 1000
_GAP_DEFAULT_LIMIT = 100
_VALID_GAP_STATUSES = frozenset({"PENDING", "RECOVERING", "RECOVERED", "EXHAUSTED"})


@router.get(
    "/india/historical/gaps",
    summary="List detected candle-sequence gaps",
    description=(
        "Returns persisted gap records with optional filters. "
        "Query params: symbol (optional), interval (optional), "
        "status (optional — PENDING|RECOVERING|RECOVERED|EXHAUSTED), "
        "limit (default 100, max 1000). "
        "Returns gapStart, gapEnd, durationSec, recoveryStatus, "
        "recoveryAttempts, expectedProvider, recoveryProvider. "
        "(Requirements 4.7, 10.8)"
    ),
    response_class=Response,
)
async def get_historical_gaps(
    request: Request,
    symbol: Annotated[
        Optional[str],
        Query(description="Filter by instrument trading symbol (e.g. RELIANCE)."),
    ] = None,
    interval: Annotated[
        Optional[str],
        Query(description="Filter by candle interval (e.g. 1m, 1d)."),
    ] = None,
    status: Annotated[
        Optional[str],
        Query(
            description=(
                "Filter by recovery status: PENDING | RECOVERING | "
                "RECOVERED | EXHAUSTED. Omit to return all statuses."
            )
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            description=f"Maximum number of records to return (1–{_GAP_MAX_LIMIT}). "
            f"Default: {_GAP_DEFAULT_LIMIT}.",
            ge=1,
            le=_GAP_MAX_LIMIT,
        ),
    ] = _GAP_DEFAULT_LIMIT,
    exchange: Annotated[
        Optional[str],
        Query(description="Filter by exchange (e.g. NSE, NFO)."),
    ] = None,
) -> Response:
    """Return detected candle-sequence gaps with optional filters.

    - ``symbol``: if provided, only gaps whose ``instrumentId`` contains this
      symbol string are returned.
    - ``interval``: if provided, only gaps for this interval are returned.
    - ``status``: if provided, must be one of the valid status values.
    - ``limit``: capped at 1000; defaults to 100.
    - ``exchange``: if provided, only gaps for this exchange are returned.
    """
    req_id = _request_id()

    # Validate status if provided.
    if status is not None:
        status_upper = status.upper()
        if status_upper not in _VALID_GAP_STATUSES:
            return _json_response(
                _error_envelope(
                    "INVALID_PARAMETER",
                    f"Invalid status '{status}'. "
                    f"Valid values: {sorted(_VALID_GAP_STATUSES)}.",
                    request_id=req_id,
                ),
                status_code=400,
            )
        status = status_upper

    gap_engine = _get_gap_recovery_engine(request)

    # Fetch from in-memory store (rehydrated from DB on startup).
    if status is not None:
        gaps = gap_engine.get_gaps_by_status(status)
    else:
        gaps = gap_engine.all_gaps()

    # Apply optional filters.
    if symbol is not None:
        symbol_upper = symbol.upper()
        gaps = [g for g in gaps if symbol_upper in g.instrumentId.upper()]

    if interval is not None:
        gaps = [g for g in gaps if g.intervalStr == interval]

    if exchange is not None:
        exchange_upper = exchange.upper()
        gaps = [g for g in gaps if g.exchange.upper() == exchange_upper]

    # Sort by gapStart descending (most recent first), then apply limit.
    gaps.sort(key=lambda g: g.gapStart, reverse=True)
    gaps = gaps[:limit]

    # Serialise to response-safe dicts — expose the fields defined in Req 10.8.
    gap_records = [
        {
            "gapId": g.gapId,
            "instrumentId": g.instrumentId,
            "exchange": g.exchange,
            "intervalStr": g.intervalStr,
            "gapStart": g.gapStart,
            "gapEnd": g.gapEnd,
            "durationSec": g.durationSec,
            "recoveryStatus": g.recoveryStatus,
            "recoveryAttempts": g.recoveryAttempts,
            "expectedProvider": g.expectedProvider,
            "recoveryProvider": g.recoveryProvider,
        }
        for g in gaps
    ]

    return _json_response(
        _success_envelope(
            gap_records,
            data_source_type="HISTORICAL",
            quality={"count": len(gap_records)},
        )
    )


# ===========================================================================
# Task 7.4 — Historical OHLCV endpoints
# ===========================================================================
#
# Implements:
#   GET  /v1/india/historical                        — OHLCV data query
#   GET  /v1/india/historical/status                 — coverage + gap summary
#   POST /v1/india/historical/backfill               — trigger async backfill
#   GET  /v1/india/historical/backfill/{job_id}      — poll backfill status
#
# Requirements: 4.1, 4.2, 4.9, 4.10, 10.1, 10.2, 10.8, 10.9, 16.10
# ===========================================================================

import asyncio
from datetime import timedelta

from src.core.schemas.provider import CANONICAL_INDIAN_TIMEFRAMES

# ---------------------------------------------------------------------------
# In-process backfill job registry
# ---------------------------------------------------------------------------
# Maps job_id (str) → BackfillJobRecord dict.
# Kept in module scope so it survives across requests within a single process.
# For multi-process / multi-replica deployments this should move to Redis;
# the current single-dict approach is correct for the development baseline.
_backfill_jobs: dict[str, dict] = {}

# Maximum number of OHLCV records returned per historical query (Req 4.9).
_HISTORICAL_MAX_RECORDS = 10_000

# Human-readable list of supported Indian-market timeframes (excludes 3m).
_SUPPORTED_TIMEFRAMES = list(CANONICAL_INDIAN_TIMEFRAMES)


# ---------------------------------------------------------------------------
# Helper: resolve HistoricalEngine from app state
# ---------------------------------------------------------------------------


def _get_historical_engine(request: Request):  # type: ignore[return]
    """Resolve (or lazily create) the HistoricalEngine on app state."""
    engine = getattr(request.app.state, "historical_engine", None)
    if engine is None:
        from src.engines.historical_engine import HistoricalEngine  # noqa: PLC0415

        engine = HistoricalEngine()
        request.app.state.historical_engine = engine
    return engine


# ---------------------------------------------------------------------------
# GET /v1/india/historical
# ---------------------------------------------------------------------------


@router.get(
    "/india/historical",
    summary="Historical OHLCV candles",
    description=(
        "Returns historical OHLCV candles for the given symbol and interval. "
        "Maximum 10,000 records per response; metadata.truncated is true when "
        "the limit is reached. "
        "The 3m interval is permanently unsupported and returns HTTP 400. "
        "(Requirements 4.9, 4.2, 16.10)"
    ),
    response_class=Response,
)
async def get_historical_ohlcv(
    request: Request,
    symbol: Annotated[
        str,
        Query(description="Instrument trading symbol (e.g. RELIANCE, NIFTY)."),
    ],
    exchange: Annotated[
        str,
        Query(description="Exchange identifier (e.g. NSE, NFO). Default: NSE."),
    ] = "NSE",
    interval: Annotated[
        str,
        Query(
            description=(
                "Candle interval. Supported: "
                + ", ".join(_SUPPORTED_TIMEFRAMES)
                + ". 3m is permanently unsupported."
            )
        ),
    ] = "1d",
    from_date: Annotated[
        Optional[str],
        Query(
            alias="from",
            description=(
                "Start of date range as UTC ISO-8601 string "
                "(e.g. 2024-01-01 or 2024-01-01T00:00:00Z). "
                "Defaults to the minimum history depth for the given interval."
            ),
        ),
    ] = None,
    to_date: Annotated[
        Optional[str],
        Query(
            alias="to",
            description=(
                "End of date range as UTC ISO-8601 string (exclusive). "
                "Defaults to now."
            ),
        ),
    ] = None,
) -> Response:
    """Return historical OHLCV candles from the canonical equity_candle table.

    - ``interval=3m`` → HTTP 400 ``INTERVAL_NOT_SUPPORTED``
    - Any unrecognised interval → HTTP 400 ``INTERVAL_NOT_SUPPORTED``
    - Malformed ``from`` / ``to`` → HTTP 400 ``INVALID_PARAMETER``
    - Up to 10,000 records; ``metadata.truncated: true`` when limit reached.
    - Response ``metadata`` includes ``provider``, ``provenance``, ``quality``,
      ``gaps``, and ``dataAsOf`` (Requirement 4.9).
    - NSE/BSE equities and indices: reads from equity_candle (TimescaleDB).
    - NFO/BFO futures: reads from futures_candle.
    - candle_bar is NOT read by this endpoint.
    """
    req_id = _request_id()

    # ── 3m hard block (Req 4.2, 16.10) ────────────────────────────────────
    if interval == "3m":
        return _json_response(
            _error_envelope(
                "INTERVAL_NOT_SUPPORTED",
                "interval 3m is permanently unsupported",
                request_id=req_id,
            ),
            status_code=400,
        )

    # ── Validate interval ──────────────────────────────────────────────────
    if interval not in _SUPPORTED_TIMEFRAMES:
        return _json_response(
            _error_envelope(
                "INTERVAL_NOT_SUPPORTED",
                f"interval '{interval}' is not supported. "
                f"Supported intervals: {', '.join(_SUPPORTED_TIMEFRAMES)}.",
                request_id=req_id,
            ),
            status_code=400,
        )

    # ── Parse from_date ────────────────────────────────────────────────────
    if from_date is not None:
        try:
            from_dt = _parse_iso_datetime(from_date)
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
    else:
        # Default from_date: minimum history depth for the given interval.
        from src.engines.historical_engine import MINIMUM_HISTORY_DAYS  # noqa: PLC0415
        days_back = MINIMUM_HISTORY_DAYS.get(interval, 365)
        from_dt = datetime.now(timezone.utc) - timedelta(days=days_back)

    # ── Parse to_date ──────────────────────────────────────────────────────
    if to_date is not None:
        try:
            to_dt = _parse_iso_datetime(to_date)
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
    else:
        to_dt = datetime.now(timezone.utc)

    # ── Validate range ordering ────────────────────────────────────────────
    if from_dt >= to_dt:
        return _json_response(
            _error_envelope(
                "INVALID_PARAMETER",
                "'from' must be earlier than 'to'.",
                request_id=req_id,
            ),
            status_code=400,
        )

    # ── Query canonical table (equity_candle / futures_candle) ────────────
    db_engine = getattr(request.app.state, "db_engine", None)
    candles: list[dict] = []
    truncated = False
    provider_name: Optional[str] = None
    data_as_of: Optional[str] = None

    if db_engine is not None:
        candles, truncated, provider_name = await _query_candles(
            db_engine=db_engine,
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            from_dt=from_dt,
            to_dt=to_dt,
            max_records=_HISTORICAL_MAX_RECORDS,
        )
        if candles:
            data_as_of = candles[-1].get("time")

    # ── Gap summary for this query window ─────────────────────────────────
    gap_engine = _get_gap_recovery_engine(request)
    all_gaps = gap_engine.all_gaps()
    symbol_upper = symbol.upper()
    from_ms = int(from_dt.timestamp() * 1000)
    to_ms = int(to_dt.timestamp() * 1000)
    relevant_gaps = [
        {
            "gapId": g.gapId,
            "gapStart": g.gapStart,
            "gapEnd": g.gapEnd,
            "durationSec": g.durationSec,
            "recoveryStatus": g.recoveryStatus,
        }
        for g in all_gaps
        if symbol_upper in g.instrumentId.upper()
        and g.intervalStr == interval
        and g.gapStart >= from_ms
        and g.gapEnd <= to_ms
    ]

    # ── Build response ─────────────────────────────────────────────────────
    envelope = _success_envelope(
        candles,
        data_source_type="HISTORICAL",
        provider=provider_name,
        data_as_of=data_as_of or _utc_iso_now(),
    )
    # Augment metadata with historical-specific fields.
    envelope["metadata"]["truncated"] = truncated
    envelope["metadata"]["gaps"] = relevant_gaps
    envelope["metadata"]["quality"] = {
        "candleCount": len(candles),
        "truncated": truncated,
    }

    return _json_response(envelope)


# ---------------------------------------------------------------------------
# GET /v1/india/historical/status
# ---------------------------------------------------------------------------


@router.get(
    "/india/historical/status",
    summary="Historical data coverage status",
    description=(
        "Returns timeframe coverage statistics, gap summary, provider activity, "
        "reconciliation status, and the list of supported timeframes. "
        "(Requirement 4.10)"
    ),
    response_class=Response,
)
async def get_historical_status(request: Request) -> Response:
    """Return a summary of the historical data layer's current state.

    Response fields:
    - ``supportedTimeframes``: List of canonical Indian-market intervals.
    - ``gapSummary``: Per-status gap counts from the in-memory gap store.
    - ``reconciliationStatus``: Aggregated reconciliation statistics.
    - ``backfillJobs``: Counts of in-process backfill jobs by status.
    - ``providerActivity``: Last-seen provider names from the gap store.
    """
    # ── Gap summary ────────────────────────────────────────────────────────
    gap_engine = _get_gap_recovery_engine(request)
    all_gaps = gap_engine.all_gaps()
    gap_counts: dict[str, int] = {
        "PENDING": 0,
        "RECOVERING": 0,
        "RECOVERED": 0,
        "EXHAUSTED": 0,
    }
    for g in all_gaps:
        status = g.recoveryStatus
        if status in gap_counts:
            gap_counts[status] += 1

    gap_summary = {
        "total": len(all_gaps),
        "byStatus": gap_counts,
    }

    # ── Reconciliation summary ─────────────────────────────────────────────
    hist_engine = _get_historical_engine(request)
    recon_stats = hist_engine.get_reconciliation_stats()

    # ── Backfill job counts ────────────────────────────────────────────────
    job_counts: dict[str, int] = {
        "PENDING": 0,
        "RUNNING": 0,
        "COMPLETED": 0,
        "FAILED": 0,
    }
    for job in _backfill_jobs.values():
        s = job.get("status", "UNKNOWN")
        if s in job_counts:
            job_counts[s] += 1

    # ── Provider activity (unique providers seen in gap store) ─────────────
    provider_set: set[str] = set()
    for g in all_gaps:
        if g.expectedProvider:
            provider_set.add(g.expectedProvider)
        if g.recoveryProvider:
            provider_set.add(g.recoveryProvider)

    status_data = {
        "supportedTimeframes": _SUPPORTED_TIMEFRAMES,
        "gapSummary": gap_summary,
        "reconciliationStatus": {
            "totalCompared": recon_stats["totalCompared"],
            "matched": recon_stats["matched"],
            "matchRatePct": recon_stats["matchRatePct"],
        },
        "backfillJobs": job_counts,
        "providerActivity": sorted(provider_set),
    }

    return _json_response(
        _success_envelope(status_data, data_source_type="DERIVED")
    )


# ---------------------------------------------------------------------------
# POST /v1/india/historical/backfill
# ---------------------------------------------------------------------------

# Pydantic v2 request body for the backfill trigger.
from pydantic import BaseModel as _BaseModel, field_validator as _field_validator


class BackfillRequest(_BaseModel):
    """Request body for triggering a historical backfill job.

    Attributes:
        symbol:           Instrument trading symbol (e.g. RELIANCE).
        exchange:         Exchange identifier (default: NSE).
        interval:         Candle interval — must be a supported Indian-market
                          timeframe (3m is permanently blocked).
        from_date:        UTC ISO-8601 start date for the backfill range.
        to_date:          UTC ISO-8601 end date for the backfill range
                          (optional; defaults to now).
        instrument_class: Instrument class for provider routing
                          (EQ | FO | IDX; default: EQ).
    """

    symbol: str
    exchange: str = "NSE"
    interval: str = "1d"
    from_date: str
    to_date: Optional[str] = None
    instrument_class: str = "EQ"

    @_field_validator("interval")
    @classmethod
    def validate_interval(cls, v: str) -> str:
        if v == "3m":
            raise ValueError("interval 3m is permanently unsupported")
        if v not in CANONICAL_INDIAN_TIMEFRAMES:
            raise ValueError(
                f"interval '{v}' is not supported. "
                f"Supported: {', '.join(CANONICAL_INDIAN_TIMEFRAMES)}"
            )
        return v

    @_field_validator("instrument_class")
    @classmethod
    def validate_instrument_class(cls, v: str) -> str:
        allowed = {"EQ", "FO", "IDX"}
        if v.upper() not in allowed:
            raise ValueError(
                f"instrument_class '{v}' is not supported. "
                f"Allowed values: {', '.join(sorted(allowed))}"
            )
        return v.upper()


@router.post(
    "/india/historical/backfill",
    summary="Trigger historical backfill",
    description=(
        "Triggers an asynchronous backfill job for the given symbol / interval "
        "range. Returns a job_id which can be polled at "
        "GET /v1/india/historical/backfill/{job_id}. "
        "(Requirements 4.1, 4.2, 10.1, 10.2)"
    ),
    response_class=Response,
)
async def trigger_backfill(
    request: Request,
    body: BackfillRequest,
) -> Response:
    """Trigger an asynchronous backfill job.

    The job runs in the background (``asyncio.create_task``).  The response
    is returned immediately with HTTP 202 and the ``job_id``.

    The job_id can be polled at ``GET /v1/india/historical/backfill/{job_id}``.
    """
    req_id = _request_id()

    # ── Parse from_date (already validated by Pydantic, but may still be bad
    #    format) ──────────────────────────────────────────────────────────────
    try:
        from_dt = _parse_iso_datetime(body.from_date)
    except ValueError:
        return _json_response(
            _error_envelope(
                "INVALID_PARAMETER",
                f"Invalid 'from_date': '{body.from_date}'. "
                "Expected ISO-8601 format.",
                request_id=req_id,
            ),
            status_code=400,
        )

    # ── Parse to_date (optional) ───────────────────────────────────────────
    if body.to_date is not None:
        try:
            to_dt = _parse_iso_datetime(body.to_date)
        except ValueError:
            return _json_response(
                _error_envelope(
                    "INVALID_PARAMETER",
                    f"Invalid 'to_date': '{body.to_date}'. "
                    "Expected ISO-8601 format.",
                    request_id=req_id,
                ),
                status_code=400,
            )
    else:
        to_dt = datetime.now(timezone.utc)

    if from_dt >= to_dt:
        return _json_response(
            _error_envelope(
                "INVALID_PARAMETER",
                "'from_date' must be earlier than 'to_date'.",
                request_id=req_id,
            ),
            status_code=400,
        )

    # ── Create job record ──────────────────────────────────────────────────
    job_id = str(uuid.uuid4())
    job_record: dict = {
        "jobId": job_id,
        "symbol": body.symbol.upper(),
        "exchange": body.exchange.upper(),
        "interval": body.interval,
        "instrumentClass": body.instrument_class,
        "fromDate": from_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "toDate": to_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "status": "PENDING",
        "createdAt": _utc_iso_now(),
        "startedAt": None,
        "completedAt": None,
        "result": None,
        "error": None,
    }
    _backfill_jobs[job_id] = job_record

    # ── Launch background task ─────────────────────────────────────────────
    db_engine = getattr(request.app.state, "db_engine", None)
    redis_client = getattr(request.app.state, "redis", None)
    hist_engine = _get_historical_engine(request)

    asyncio.create_task(
        _run_backfill_job(
            job_id=job_id,
            hist_engine=hist_engine,
            symbol=body.symbol.upper(),
            exchange=body.exchange.upper(),
            instrument_class=body.instrument_class,
            interval=body.interval,
            from_dt=from_dt,
            to_dt=to_dt,
            db_engine=db_engine,
            redis_client=redis_client,
        )
    )

    _log.info(
        "backfill_job_created",
        component="india_api",
        job_id=job_id,
        symbol=body.symbol,
        exchange=body.exchange,
        interval=body.interval,
    )

    return _json_response(
        _success_envelope(
            job_record,
            data_source_type="DERIVED",
        ),
        status_code=202,
    )


# ---------------------------------------------------------------------------
# GET /v1/india/historical/backfill/{job_id}
# ---------------------------------------------------------------------------


@router.get(
    "/india/historical/backfill/{job_id}",
    summary="Poll backfill job status",
    description=(
        "Returns the current status of a previously triggered backfill job. "
        "Status values: PENDING | RUNNING | COMPLETED | FAILED. "
        "(Requirements 4.1, 10.1, 10.2)"
    ),
    response_class=Response,
)
async def get_backfill_status(
    request: Request,
    job_id: str,
) -> Response:
    """Return the status of a backfill job by its ``job_id``.

    - ``PENDING``   — job has been accepted but not yet started.
    - ``RUNNING``   — job is currently executing.
    - ``COMPLETED`` — job finished successfully; ``result`` contains the
                      summary dict from ``HistoricalEngine.run_backfill``.
    - ``FAILED``    — job encountered an unhandled error; ``error`` contains
                      the error message string.

    HTTP 404 is returned when the ``job_id`` is not found.
    """
    req_id = _request_id()

    job_record = _backfill_jobs.get(job_id)
    if job_record is None:
        return _json_response(
            _error_envelope(
                "NOT_FOUND",
                f"Backfill job '{job_id}' not found.",
                request_id=req_id,
            ),
            status_code=404,
        )

    return _json_response(
        _success_envelope(job_record, data_source_type="DERIVED")
    )


# ===========================================================================
# Private helpers (task 7.4)
# ===========================================================================


async def _run_backfill_job(
    *,
    job_id: str,
    hist_engine: Any,
    symbol: str,
    exchange: str,
    instrument_class: str,
    interval: str,
    from_dt: datetime,
    to_dt: datetime,
    db_engine: Any,
    redis_client: Any,
) -> None:
    """Execute the backfill job and update the job record on completion.

    This coroutine is run as a background asyncio task via
    ``asyncio.create_task``.  It updates the ``_backfill_jobs`` registry
    in-place so that ``GET /backfill/{job_id}`` can observe progress.

    A missing ``db_engine`` or ``redis_client`` (i.e. running without
    external dependencies) will cause the underlying ``run_backfill`` to
    return zero candles — the job is still marked ``COMPLETED`` with that
    summary.
    """
    job = _backfill_jobs.get(job_id)
    if job is None:
        return  # Defensive — should never happen.

    job["status"] = "RUNNING"
    job["startedAt"] = _utc_iso_now()

    try:
        result = await hist_engine.run_backfill(
            symbol=symbol,
            exchange=exchange,
            instrument_class=instrument_class,
            interval=interval,
            from_ts=from_dt,
            to_ts=to_dt,
            db_engine=db_engine,
            redis_client=redis_client,
            is_indian_market=True,
        )
        job["status"] = "COMPLETED"
        job["result"] = result
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "backfill_job_failed",
            component="india_api",
            job_id=job_id,
            error=str(exc),
        )
        job["status"] = "FAILED"
        job["error"] = str(exc)
    finally:
        job["completedAt"] = _utc_iso_now()


async def _query_candles(
    *,
    db_engine: Any,
    symbol: str,
    exchange: str,
    interval: str,
    from_dt: datetime,
    to_dt: datetime,
    max_records: int,
) -> tuple[list[dict], bool, Optional[str]]:
    """Query the canonical equity_candle table and return normalised records.

    Routing (candle_bar is archive-only — never read by production queries):
      NSE equities and indices → equity_candle
      NFO futures              → futures_candle  (future: when F&O data exists)
      NFO options              → options_candle  (future: when F&O data exists)

    Returns a 3-tuple of ``(candles, truncated, provider_name)``:
    - ``candles``: list of OHLCV dicts ready for the API response.
    - ``truncated``: True when the result was capped at ``max_records``.
    - ``provider_name``: Provider string from the most recent row, or None.

    Queries ``max_records + 1`` rows to determine truncation without a
    separate COUNT query.
    """
    from sqlalchemy import text  # local import  # noqa: PLC0415

    instrument_id = f"{exchange.upper()}:{symbol.upper()}"

    # Route to the correct canonical table.
    # Currently all queries are against equity_candle (EQ + IDX segments).
    # When futures_candle and options_candle are populated, the router will
    # use exchange/instrument_class to select the right table.
    if exchange.upper() in ("NFO", "BFO"):
        # F&O instruments — query futures_candle first
        select_sql = text(
            """
            SELECT
                EXTRACT(EPOCH FROM time)::bigint AS time_epoch,
                open, high, low, close, volume,
                open_interest AS oi,
                FALSE AS volume_unavailable,
                provider, source_type, poor_quality,
                normalisation_version, session_date
            FROM futures_candle
            WHERE instrument_id = :instrument_id
              AND exchange       = :exchange
              AND interval_str   = :interval_str
              AND time           >= :from_ts
              AND time            < :to_ts
              AND poor_quality    = FALSE
            ORDER BY time ASC
            LIMIT :limit
            """
        )
    else:
        # NSE/BSE equities and indices → equity_candle
        # NOTE: equity_candle has no open_interest column (equities have no OI).
        # Index instruments (NIFTY, BANKNIFTY) also have no OI in candle data.
        select_sql = text(
            """
            SELECT
                EXTRACT(EPOCH FROM time)::bigint AS time_epoch,
                open, high, low, close, volume,
                NULL::bigint AS oi,
                volume_unavailable, provider, source_type, poor_quality,
                normalisation_version, session_date
            FROM equity_candle
            WHERE instrument_id = :instrument_id
              AND exchange       = :exchange
              AND interval_str   = :interval_str
              AND time           >= :from_ts
              AND time            < :to_ts
              AND poor_quality    = FALSE
            ORDER BY time ASC
            LIMIT :limit
            """
        )

    try:
        async with db_engine.connect() as conn:
            result = await conn.execute(
                select_sql,
                {
                    "instrument_id": instrument_id,
                    "exchange": exchange.upper(),
                    "interval_str": interval,
                    "from_ts": from_dt,
                    "to_ts": to_dt,
                    "limit": max_records + 1,   # fetch one extra to detect truncation
                },
            )
            rows = result.fetchall()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "historical_query_failed",
            component="india_api",
            instrument_id=instrument_id,
            interval=interval,
            error=str(exc),
        )
        return [], False, None

    truncated = len(rows) > max_records
    rows = rows[:max_records]  # trim to limit

    candles: list[dict] = []
    provider_name: Optional[str] = None

    for row in rows:
        provider_name = str(row.provider)
        candles.append(
            {
                "time": row.time_epoch,
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": int(row.volume),
                "oi": int(row.oi) if row.oi is not None else None,
                "volumeUnavailable": bool(row.volume_unavailable),
                "provider": str(row.provider),
                "sourceType": str(row.source_type) if row.source_type else None,
            }
        )

    return candles, truncated, provider_name


def _parse_iso_datetime(value: str) -> datetime:
    """Parse a user-supplied ISO-8601 date or datetime string to a UTC datetime.

    Accepts:
      - ``YYYY-MM-DD``                    → interpreted as midnight UTC
      - ``YYYY-MM-DDTHH:MM:SS``           → interpreted as UTC
      - ``YYYY-MM-DDTHH:MM:SSZ``          → UTC explicit
      - ``YYYY-MM-DDTHH:MM:SS+05:30``     → converted to UTC

    Raises ``ValueError`` for any unrecognised format.
    """
    value = value.strip()

    # Pure date: YYYY-MM-DD
    if len(value) == 10 and "T" not in value:
        try:
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"Cannot parse date: {value!r}")

    # Datetime string — try stdlib fromisoformat (handles most ISO-8601).
    # Python 3.11+ handles trailing Z natively; for older compatibility
    # we normalise the Z suffix manually first.
    normalised = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        raise ValueError(f"Cannot parse datetime: {value!r}")


# ---------------------------------------------------------------------------
# Persistence helpers — option_greeks_snapshot
# ---------------------------------------------------------------------------


async def _persist_option_greeks(
    greeks_map: dict[str, Any],
    *,
    db_engine: Any,
    received_at: str,
) -> None:
    """Persist a batch of normalised option Greeks to ``option_greeks_snapshot``.

    Called fire-and-forget from the Greeks REST endpoint.
    Silently logs and returns on any DB error.

    Args:
        greeks_map:   Dict of {instrument_key → normalized_greeks_dict}.
        db_engine:    Async SQLAlchemy engine.
        received_at:  UTC ISO-8601 timestamp of the fetch.
    """
    if not db_engine or not greeks_map:
        return

    from sqlalchemy import text as _text  # noqa: PLC0415
    import datetime as _dt  # noqa: PLC0415

    try:
        ts = _dt.datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
    except (ValueError, AttributeError):
        ts = _dt.datetime.now(_dt.timezone.utc)

    session_date = ts.astimezone(
        _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    ).date()

    upsert_sql = _text(
        """
        INSERT INTO option_greeks_snapshot (
            instrument_id, instrument_key, timestamp, session_date,
            ltp, option_price, prev_close,
            oi, volume, ltq,
            iv, delta, gamma, theta, vega, rho,
            calculation_method, provider, received_at, quality_status
        ) VALUES (
            :instrument_id, :instrument_key, :timestamp, :session_date,
            :ltp, :ltp, :prev_close,
            :oi, :volume, :ltq,
            :iv, :delta, :gamma, :theta, :vega, :rho,
            'PROVIDER', :provider, :received_at, 'TRUSTED'
        )
        ON CONFLICT DO NOTHING
        """
    )

    rows = []
    for key, g in greeks_map.items():
        if not g:
            continue
        oi_val = g.get("oi") if not g.get("oiMissing", True) else None
        rows.append({
            "instrument_id":   g.get("instrumentId") or key,
            "instrument_key":  key,
            "timestamp":       ts,
            "session_date":    session_date,
            "ltp":             g.get("ltp"),
            "prev_close":      g.get("prevClose"),
            "oi":              oi_val,
            "volume":          g.get("volume"),
            "ltq":             g.get("lastTradeQty"),
            "iv":              g.get("iv"),
            "delta":           g.get("delta"),
            "gamma":           g.get("gamma"),
            "theta":           g.get("theta"),
            "vega":            g.get("vega"),
            "rho":             g.get("rho"),
            "provider":        g.get("provider") or "upstox",
            "received_at":     ts,
        })

    if not rows:
        return

    try:
        async with db_engine.begin() as conn:
            for row in rows:
                await conn.execute(upsert_sql, row)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "option_greeks_persist_failed",
            component="india_api",
            count=len(rows),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Helpers for DualProviderEngine
# ---------------------------------------------------------------------------


def _get_dual_engine(request: Request):  # type: ignore[return]
    """Resolve the DualProviderEngine from app state, or create a stub."""
    engine = getattr(request.app.state, "dual_provider_engine", None)
    if engine is None:
        from src.engines.dual_provider_engine import DualProviderEngine  # noqa: PLC0415
        engine = DualProviderEngine()
        request.app.state.dual_provider_engine = engine
    return engine


# ---------------------------------------------------------------------------
# GET /v1/india/options/greeks — batch option Greeks from Upstox V3
# ---------------------------------------------------------------------------


@router.get(
    "/india/options/greeks",
    summary="Option Greeks for a batch of instruments",
    description=(
        "Fetch IV, delta, gamma, theta, vega and OI for up to 50 F&O "
        "instruments via Upstox V3 /v3/market-quote/option-greek. "
        "Zero IV is never returned — it is treated as missing. "
        "Greeks are tagged with greekSource=PROVIDER to distinguish them "
        "from internally calculated values."
    ),
    response_class=Response,
)
async def get_option_greeks(
    request: Request,
    instrument_keys: Annotated[
        str,
        Query(
            description=(
                "Comma-separated Upstox instrument keys for option contracts. "
                "Maximum 50 per request. "
                "Example: NSE_FO|43985,NSE_FO|43986"
            )
        ),
    ] = "",
) -> Response:
    """Fetch option Greeks for a batch of instruments.

    Returns IV, delta, gamma, theta, vega, OI and volume.
    greekSource=PROVIDER means the values came directly from the provider,
    not from any internal calculation.
    """
    if not instrument_keys.strip():
        return _json_response(
            _error_envelope("MISSING_PARAMETER", "instrument_keys is required"),
            status_code=400,
        )

    keys = [k.strip() for k in instrument_keys.split(",") if k.strip()]
    if len(keys) > 50:
        return _json_response(
            _error_envelope(
                "PARAMETER_ERROR",
                f"Maximum 50 instrument_keys per request; got {len(keys)}",
            ),
            status_code=400,
        )

    upstox_adapter = getattr(request.app.state, "upstox_adapter", None)
    if upstox_adapter is None:
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                "Upstox adapter not configured; option Greeks unavailable",
                provider="upstox",
            ),
            status_code=503,
        )

    from src.core.normalizers.upstox import UpstoxNormalizer  # noqa: PLC0415
    from src.core.normalizers.freshness import FreshnessClassifier  # noqa: PLC0415

    norm = UpstoxNormalizer()
    freshness = FreshnessClassifier()
    received_at = _utc_iso_now()

    try:
        raw_greeks = await upstox_adapter.fetch_option_greeks_batched(
            instrument_keys=keys, batch_size=50
        )
    except Exception as exc:
        return _json_response(
            _error_envelope(
                "PROVIDER_ERROR",
                f"Failed to fetch option Greeks: {type(exc).__name__}",
                provider="upstox",
            ),
            status_code=502,
        )

    results: dict[str, Any] = {}
    for key, raw in raw_greeks.items():
        normalized = norm.normalize_option_greek(
            instrument_key=key,
            raw=raw,
            instrument_id=key,
            received_at=received_at,
        )
        if normalized:
            freshness.classify(normalized)
            results[key] = normalized

    # Persist Greeks to option_greeks_snapshot (fire-and-forget)
    db_engine = getattr(request.app.state, "db_engine", None)
    if db_engine is not None and results:
        import asyncio as _asyncio  # noqa: PLC0415
        _asyncio.ensure_future(
            _persist_option_greeks(results, db_engine=db_engine, received_at=received_at)
        )

    return _json_response(
        _success_envelope(
            data=results,
            data_as_of=received_at,
            provider="upstox",
            data_source_type="LIVE",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/india/market/exchange-status — exchange trading status
# ---------------------------------------------------------------------------


@router.get(
    "/india/market/exchange-status",
    summary="Current exchange trading status",
    description=(
        "Returns the current trading status for NSE, BSE, or MCX from "
        "Upstox GET /v2/market/status/{exchange}. "
        "Status values: NORMAL_OPEN, PRE_OPEN, CLOSED, HOLIDAY, etc."
    ),
    response_class=Response,
)
async def get_exchange_status(
    request: Request,
    exchange: Annotated[
        str,
        Query(description="Exchange code: NSE, BSE, or MCX"),
    ] = "NSE",
) -> Response:
    """Return the current trading status for an exchange from Upstox."""
    valid_exchanges = {"NSE", "BSE", "MCX"}
    if exchange.upper() not in valid_exchanges:
        return _json_response(
            _error_envelope(
                "INVALID_EXCHANGE",
                f"exchange must be one of {sorted(valid_exchanges)}; got {exchange!r}",
            ),
            status_code=400,
        )

    upstox_adapter = getattr(request.app.state, "upstox_adapter", None)
    if upstox_adapter is None:
        # Fall back to MarketSessionEngine-derived status
        session_engine = _get_session_engine(request)
        phase = session_engine.get_current_phase()
        return _json_response(
            _success_envelope(
                data={
                    "exchange": exchange.upper(),
                    "status": phase.value,
                    "source": "session_engine_fallback",
                },
                data_source_type="DERIVED",
            )
        )

    try:
        status_data = await upstox_adapter.fetch_exchange_status(exchange.upper())
    except Exception as exc:
        return _json_response(
            _error_envelope(
                "PROVIDER_ERROR",
                f"Exchange status fetch failed: {type(exc).__name__}",
                provider="upstox",
            ),
            status_code=502,
        )

    return _json_response(
        _success_envelope(
            data=status_data,
            data_as_of=_utc_iso_now(),
            provider="upstox",
            data_source_type="LIVE",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/india/market/holidays — market holiday schedule
# ---------------------------------------------------------------------------


@router.get(
    "/india/market/holidays",
    summary="Market holiday schedule",
    description=(
        "Returns NSE/BSE/MCX market holiday schedule from "
        "Upstox GET /v2/market/holidays."
    ),
    response_class=Response,
)
async def get_market_holidays(request: Request) -> Response:
    """Return the market holiday schedule from Upstox."""
    upstox_adapter = getattr(request.app.state, "upstox_adapter", None)
    if upstox_adapter is None:
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                "Upstox adapter not configured; holiday data unavailable",
                provider="upstox",
            ),
            status_code=503,
        )

    try:
        holidays = await upstox_adapter.fetch_market_holidays()
    except Exception as exc:
        return _json_response(
            _error_envelope(
                "PROVIDER_ERROR",
                f"Market holidays fetch failed: {type(exc).__name__}",
                provider="upstox",
            ),
            status_code=502,
        )

    return _json_response(
        _success_envelope(
            data=holidays,
            data_as_of=_utc_iso_now(),
            provider="upstox",
            data_source_type="REFERENCE",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/india/historical/oi — historical open interest time-series
# ---------------------------------------------------------------------------


@router.get(
    "/india/historical/oi",
    summary="Historical Open Interest time-series",
    description=(
        "Fetch historical OI time-series for a derivative instrument "
        "from Angel One SmartAPI getOIData endpoint. "
        "OI is NEVER populated from tradedValue — only from the dedicated "
        "OI field. Missing OI is represented as null, not zero."
    ),
    response_class=Response,
)
async def get_historical_oi(
    request: Request,
    symbol: Annotated[str, Query(description="Angel One trading symbol")] = "",
    token: Annotated[str, Query(description="Angel One instrument token")] = "",
    from_date: Annotated[
        str, Query(alias="from", description="Start datetime YYYY-MM-DD HH:MM (IST)")
    ] = "",
    to_date: Annotated[
        str, Query(alias="to", description="End datetime YYYY-MM-DD HH:MM (IST)")
    ] = "",
    interval: Annotated[
        str, Query(description="Canonical interval: 1m,5m,10m,15m,30m,1h,1d")
    ] = "1d",
    exchange: Annotated[str, Query(description="Exchange: NFO or MCX")] = "NFO",
) -> Response:
    """Fetch historical OI from Angel One for a derivative instrument."""
    if not symbol or not token or not from_date or not to_date:
        return _json_response(
            _error_envelope(
                "MISSING_PARAMETER",
                "symbol, token, from, and to are required",
            ),
            status_code=400,
        )

    angel_adapter = getattr(request.app.state, "angel_one_adapter", None)
    if angel_adapter is None:
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                "Angel One adapter not configured; historical OI unavailable",
                provider="angel_one",
            ),
            status_code=503,
        )

    from src.core.normalizers.angel_one import AngelOneNormalizer  # noqa: PLC0415
    norm = AngelOneNormalizer()

    try:
        raw_records = await angel_adapter.fetch_historical_oi(
            symbol=symbol,
            token=token,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
            exchange=exchange,
        )
    except Exception as exc:
        return _json_response(
            _error_envelope(
                "PROVIDER_ERROR",
                f"Historical OI fetch failed: {type(exc).__name__}",
                provider="angel_one",
            ),
            status_code=502,
        )

    normalized: list[dict[str, Any]] = []
    for record in raw_records:
        n = norm.normalize_oi_record(
            raw_record=record,
            instrument_id=f"{exchange}:{symbol}",
            exchange=exchange,
            interval=interval,
        )
        if n:
            normalized.append(n)

    return _json_response(
        _success_envelope(
            data={
                "records": normalized,
                "count": len(normalized),
                "symbol": symbol,
                "exchange": exchange,
                "interval": interval,
                "from": from_date,
                "to": to_date,
                "oiSemantic": "NULL_WHEN_MISSING_NEVER_ZERO",
            },
            provider="angel_one",
            data_source_type="HISTORICAL",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/india/historical/intraday — intraday candles (current session)
# ---------------------------------------------------------------------------


@router.get(
    "/india/historical/intraday",
    summary="Intraday candles for current session",
    description=(
        "Fetch OHLCV candles for the current trading session only from "
        "Upstox V3 /v3/historical-candle/intraday endpoint. "
        "The last candle may be incomplete (ongoing candle). "
        "OI is included for derivative instruments (index 6 in V3 response)."
    ),
    response_class=Response,
)
async def get_intraday_candles(
    request: Request,
    instrument_key: Annotated[
        str,
        Query(description="Upstox instrument key, e.g. NSE_EQ|INE002A01018"),
    ] = "",
    interval: Annotated[
        str,
        Query(description="Canonical interval: 1m, 5m, 10m, 15m, 30m, 1h"),
    ] = "1m",
    exchange: Annotated[str, Query(description="Exchange code")] = "NSE",
) -> Response:
    """Fetch intraday OHLCV candles for the current session from Upstox V3."""
    if not instrument_key.strip():
        return _json_response(
            _error_envelope("MISSING_PARAMETER", "instrument_key is required"),
            status_code=400,
        )

    valid_intervals = {"1m", "5m", "10m", "15m", "30m", "1h"}
    if interval not in valid_intervals:
        return _json_response(
            _error_envelope(
                "INVALID_INTERVAL",
                f"interval must be one of {sorted(valid_intervals)} for intraday; got {interval!r}",
            ),
            status_code=400,
        )

    upstox_adapter = getattr(request.app.state, "upstox_adapter", None)
    if upstox_adapter is None:
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                "Upstox adapter not configured; intraday candles unavailable",
                provider="upstox",
            ),
            status_code=503,
        )

    from src.core.normalizers.upstox import UpstoxNormalizer  # noqa: PLC0415
    norm = UpstoxNormalizer()

    try:
        raw_candles = await upstox_adapter.fetch_intraday_candles(
            instrument_key=instrument_key.strip(),
            interval=interval,
        )
    except Exception as exc:
        return _json_response(
            _error_envelope(
                "PROVIDER_ERROR",
                f"Intraday candles fetch failed: {type(exc).__name__}",
                provider="upstox",
            ),
            status_code=502,
        )

    normalized: list[dict[str, Any]] = []
    for raw_candle in raw_candles:
        n = norm.normalize_candle(
            raw_candle=raw_candle,
            instrument_id=instrument_key.strip(),
            exchange=exchange,
            interval=interval,
        )
        if n:
            # Preserve is_complete flag from adapter
            n["isComplete"] = raw_candle.get("is_complete", True)
            normalized.append(n)

    return _json_response(
        _success_envelope(
            data={
                "candles": normalized,
                "count": len(normalized),
                "instrumentKey": instrument_key,
                "interval": interval,
                "exchange": exchange,
                "sessionType": "INTRADAY",
                "oiIncluded": True,
                "apiVersion": "upstox_v3",
            },
            data_as_of=_utc_iso_now(),
            provider="upstox",
            data_source_type="LIVE",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/india/providers/capabilities — current capability matrix
# ---------------------------------------------------------------------------


@router.get(
    "/india/providers/capabilities",
    summary="Current provider capability matrix",
    description=(
        "Returns the current provider capability matrix showing which data "
        "types each provider supports, their rate limits, and routing priority."
    ),
    response_class=Response,
)
async def get_provider_capabilities(request: Request) -> Response:
    """Return the current provider capability matrix."""
    from src.providers.capability_matrix import _MATRIX  # noqa: PLC0415
    caps = [
        {
            "provider":           c.provider.value,
            "dataType":           c.dataType.value,
            "instrumentClass":    c.instrumentClass,
            "supported":          c.supported,
            "liveSupported":      c.liveSupported,
            "historySupported":   c.historySupported,
            "maxChunkDays":       c.maxChunkDays,
            "requestsPerSecond":  c.requestsPerSecond,
            "intervalSupport":    c.intervalSupport,
            "sourceType":         c.sourceType.value,
            "priority":           c.priority,
        }
        for c in _MATRIX
    ]
    return _json_response(
        _success_envelope(data={"capabilities": caps, "count": len(caps)})
    )
