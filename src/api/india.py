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
    """Return historical OHLCV candles from the candle_bar table.

    - ``interval=3m`` → HTTP 400 ``INTERVAL_NOT_SUPPORTED``
    - Any unrecognised interval → HTTP 400 ``INTERVAL_NOT_SUPPORTED``
    - Malformed ``from`` / ``to`` → HTTP 400 ``INVALID_PARAMETER``
    - Up to 10,000 records; ``metadata.truncated: true`` when limit reached.
    - Response ``metadata`` includes ``provider``, ``provenance``, ``quality``,
      ``gaps``, and ``dataAsOf`` (Requirement 4.9).
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

    # ── Query candle_bar ───────────────────────────────────────────────────
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
    """Query the candle_bar table and return normalised candle records.

    Returns a 3-tuple of ``(candles, truncated, provider_name)``:
    - ``candles``: list of OHLCV dicts ready for the API response.
    - ``truncated``: True when the result was capped at ``max_records``.
    - ``provider_name``: Provider string from the most recent row, or None.

    Queries ``max_records + 1`` rows to determine truncation without a
    separate COUNT query.
    """
    from sqlalchemy import text  # local import  # noqa: PLC0415

    instrument_id = f"{exchange.upper()}:{symbol.upper()}"

    select_sql = text(
        """
        SELECT
            EXTRACT(EPOCH FROM time)::bigint AS time_epoch,
            open, high, low, close, volume, oi,
            volume_unavailable, provider, poor_quality,
            normalisation_version, session_date
        FROM candle_bar
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
