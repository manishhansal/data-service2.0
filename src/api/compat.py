"""
src/api/compat.py

AlphaForge ScraplingProvider compatibility routes.

The AlphaForge ScraplingProvider (TypeScript) calls:
    GET /scraping/historical?symbol=...&exchange=...&interval=...&from=...&to=...
    GET /scraping/quotes?symbols=SYM1,SYM2,...
    GET /scraping/option-chain?underlying=...&expiry=...
    GET /scraping/instruments?exchange=...&type=...

These endpoints transparently proxy to the canonical data-service2.0 endpoints
(/v1/india/historical, /v1/india/quotes, etc.) and re-shape the response to
match the exact JSON shapes the ScraplingProvider TypeScript code expects.

This module is intentionally a thin translation layer — no business logic lives
here.  All data acquisition, validation, and provenance is handled by the
underlying /v1/india/* handlers.

Response shapes expected by ScraplingProvider (from scrapling.ts inspection):

    /scraping/historical → { candles: OHLCVCandle[], count: number }
    /scraping/quotes     → { quotes: Array<MDQuote | null> }
    /scraping/option-chain → OptionChain (canonical shape, unwrapped)
    /scraping/instruments → { instruments: Instrument[], count: number, cached: bool, exchange: str }

Requirements: Phase 3 gap fix — P0 path compatibility.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from src.api.india import (
    _error_envelope,
    _json_response,
    _request_id,
    _get_historical_engine,
    _get_market_engine,
    _query_candles,
    _parse_iso_datetime,
    _utc_iso_now,
    _HISTORICAL_MAX_RECORDS,
    _SUPPORTED_TIMEFRAMES,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Blocked interval guard
# ---------------------------------------------------------------------------
_BLOCKED_INTERVALS = frozenset({"3m"})


def _check_interval(interval: str) -> Optional[Response]:
    """Return a 400 error response if the interval is permanently blocked."""
    if interval in _BLOCKED_INTERVALS:
        return _json_response(
            _error_envelope(
                "INTERVAL_NOT_SUPPORTED",
                f"Interval '3m' is permanently unsupported for Indian market data. "
                f"Supported intervals: {', '.join(_SUPPORTED_TIMEFRAMES)}",
                request_id=_request_id(),
            ),
            status_code=400,
        )
    if interval not in _SUPPORTED_TIMEFRAMES:
        return _json_response(
            _error_envelope(
                "INTERVAL_NOT_SUPPORTED",
                f"Unsupported interval '{interval}'. "
                f"Supported: {', '.join(_SUPPORTED_TIMEFRAMES)}",
                request_id=_request_id(),
            ),
            status_code=400,
        )
    return None


# ---------------------------------------------------------------------------
# GET /scraping/historical
# ---------------------------------------------------------------------------
@router.get(
    "/scraping/historical",
    summary="[Compat] AlphaForge ScraplingProvider — historical OHLCV",
    description=(
        "Compatibility shim for AlphaForge ScraplingProvider. "
        "Proxies to /v1/india/historical and reshapes the response to the "
        "shape ScraplingProvider expects: {candles, count}. "
        "The 3m interval is permanently unsupported and returns HTTP 400."
    ),
    include_in_schema=True,
    tags=["Compat"],
)
async def compat_historical(
    request: Request,
    symbol: Annotated[str, Query(description="NSE trading symbol")],
    exchange: Annotated[str, Query(description="Exchange (NSE, NFO, etc.)")] = "NSE",
    interval: Annotated[str, Query(description="Candle interval")] = "1d",
    from_date: Annotated[
        Optional[str],
        Query(alias="from", description="Start date/datetime (ISO-8601)"),
    ] = None,
    to_date: Annotated[
        Optional[str],
        Query(alias="to", description="End date/datetime (ISO-8601)"),
    ] = None,
) -> Response:
    """Compatibility endpoint: /scraping/historical → /v1/india/historical logic.

    Returns { candles: [...], count: N } without the metadata envelope so
    ScraplingProvider can consume it directly.
    """
    # Validate interval
    interval_err = _check_interval(interval)
    if interval_err is not None:
        return interval_err

    # Strip exchange prefix if present (e.g. "NSE:HDFCBANK" → "HDFCBANK")
    raw_symbol = symbol.upper()
    if ":" in raw_symbol:
        _exch_prefix, _, raw_symbol = raw_symbol.partition(":")
        # Re-derive exchange from prefix if not explicitly set by caller
        if exchange.upper() == "NSE" and _exch_prefix:
            exchange = _exch_prefix

    # Parse date range (default: last 90 days → today)
    from datetime import timedelta  # noqa: PLC0415
    now = datetime.now(timezone.utc)
    try:
        from_dt = _parse_iso_datetime(from_date) if from_date else (now - timedelta(days=90))
        to_dt = _parse_iso_datetime(to_date) if to_date else now
    except ValueError as exc:
        return _json_response(
            _error_envelope("INVALID_PARAMETER", str(exc), request_id=_request_id()),
            status_code=400,
        )

    # Resolve db_engine — may be None when DB is not connected
    db_engine = getattr(request.app.state, "db_engine", None)

    # Query from DB
    candles, truncated, provider_name = await _query_candles(
        db_engine=db_engine,
        symbol=raw_symbol,
        exchange=exchange.upper(),
        interval=interval,
        from_dt=from_dt,
        to_dt=to_dt,
        max_records=_HISTORICAL_MAX_RECORDS,
    )

    # If DB returned nothing (DB not available / no data), attempt live fetch
    # via historical engine which calls provider adapters.
    if not candles:
        try:
            hist_engine = _get_historical_engine(request)
            result = await hist_engine.run_backfill(
                symbol=raw_symbol,
                exchange=exchange.upper(),
                instrument_class="EQ",
                interval=interval,
                from_ts=from_dt,
                to_ts=to_dt,
                db_engine=db_engine,
                redis_client=getattr(request.app.state, "redis", None),
                is_indian_market=True,
            )
            # After backfill, try the DB query again
            if db_engine is not None:
                candles, truncated, provider_name = await _query_candles(
                    db_engine=db_engine,
                    symbol=raw_symbol,
                    exchange=exchange.upper(),
                    interval=interval,
                    from_dt=from_dt,
                    to_dt=to_dt,
                    max_records=_HISTORICAL_MAX_RECORDS,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "compat_historical_backfill_failed",
                component="compat",
                symbol=raw_symbol,
                error=str(exc),
            )

    # Return in the shape ScraplingProvider expects
    return _json_response(
        {
            "candles": candles,
            "count": len(candles),
            "truncated": truncated,
            "provider": provider_name,
            "symbol": raw_symbol,
            "exchange": exchange.upper(),
            "interval": interval,
        }
    )


# ---------------------------------------------------------------------------
# GET /scraping/quotes
# ---------------------------------------------------------------------------
@router.get(
    "/scraping/quotes",
    summary="[Compat] AlphaForge ScraplingProvider — batch quotes",
    description=(
        "Compatibility shim for AlphaForge ScraplingProvider. "
        "Returns { quotes: Array<MDQuote | null> } for a comma-separated "
        "list of symbols."
    ),
    include_in_schema=True,
    tags=["Compat"],
)
async def compat_quotes(
    request: Request,
    symbols: Annotated[
        str,
        Query(description="Comma-separated NSE symbols (e.g. RELIANCE,NIFTY)"),
    ],
) -> Response:
    """Compatibility endpoint: /scraping/quotes → MarketEngine batch quotes."""
    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        return _json_response(
            _error_envelope(
                "INVALID_PARAMETER",
                "symbols parameter is required",
                request_id=_request_id(),
            ),
            status_code=400,
        )

    market_engine = _get_market_engine(request)
    quotes: list[Optional[dict]] = []
    for sym in symbol_list:
        try:
            q = await market_engine.get_live_quote(sym, exchange="NSE")
            # Normalise to the MDQuote shape ScraplingProvider expects
            quote_out: Optional[dict] = {
                "symbol": sym,
                "token": None,
                "exchange": "NSE",
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
            # Return null if ltp is unavailable (provider not configured /
            # market closed with no cached data) — never return fabricated data
            if quote_out["ltp"] is None and q.get("marketStatus") in (None, "UNKNOWN"):
                quote_out = None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "compat_quote_failed",
                component="compat",
                symbol=sym,
                error=str(exc),
            )
            quote_out = None
        quotes.append(quote_out)

    return _json_response({"quotes": quotes})


# ---------------------------------------------------------------------------
# GET /scraping/option-chain
# ---------------------------------------------------------------------------
@router.get(
    "/scraping/option-chain",
    summary="[Compat] AlphaForge ScraplingProvider — option chain",
    description=(
        "Compatibility shim for AlphaForge ScraplingProvider. "
        "Returns the raw OptionChain object (unwrapped from envelope). "
        "Proxies to MarketEngine.get_option_chain()."
    ),
    include_in_schema=True,
    tags=["Compat"],
)
async def compat_option_chain(
    request: Request,
    underlying: Annotated[str, Query(description="Underlying symbol (e.g. NIFTY)")],
    expiry: Annotated[
        Optional[str],
        Query(description="Expiry date YYYY-MM-DD. Defaults to nearest."),
    ] = None,
    exchange: Annotated[str, Query(description="Exchange (default NSE)")] = "NSE",
) -> Response:
    """Compatibility endpoint: /scraping/option-chain → MarketEngine."""
    from datetime import date  # noqa: PLC0415

    req_id = _request_id()

    if expiry is not None:
        try:
            date.fromisoformat(expiry)
        except ValueError:
            return _json_response(
                _error_envelope(
                    "INVALID_PARAMETER",
                    f"Invalid expiry '{expiry}'. Expected YYYY-MM-DD.",
                    request_id=req_id,
                ),
                status_code=400,
            )

    market_engine = _get_market_engine(request)
    try:
        chain = await market_engine.get_option_chain(
            underlying=underlying.upper(),
            expiry=expiry,
            exchange=exchange.upper(),
        )
        # Return the chain dict directly (no envelope) — ScraplingProvider
        # parses this as the OptionChain shape directly.
        return _json_response(chain)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "compat_option_chain_failed",
            component="compat",
            underlying=underlying,
            error=str(exc),
        )
        # Return an empty chain rather than HTTP 5xx — ScraplingProvider
        # treats an empty rows array as "no data" and fails over to the
        # next registered provider (Angel One).
        return _json_response(
            {
                "underlying": underlying.upper(),
                "spot": None,
                "expiry": expiry,
                "expiries": [],
                "rows": [],
                "analytics": {},
                "marketStatus": "NO_DATA",
                "provider": "unavailable",
                "fetchedAt": _utc_iso_now(),
            }
        )


# ---------------------------------------------------------------------------
# GET /scraping/instruments
# ---------------------------------------------------------------------------
@router.get(
    "/scraping/instruments",
    summary="[Compat] AlphaForge ScraplingProvider — instrument master",
    description=(
        "Compatibility shim for AlphaForge ScraplingProvider. "
        "Returns { instruments: [...], count: N, cached: bool, exchange: str }. "
        "Proxies to /v1/instruments logic."
    ),
    include_in_schema=True,
    tags=["Compat"],
)
async def compat_instruments(
    request: Request,
    exchange: Annotated[str, Query(description="Exchange filter (NSE, NFO, BSE)")] = "NSE",
    type: Annotated[  # noqa: A002
        Optional[str],
        Query(description="Instrument type filter (EQ, IDX, OPTIDX, etc.)"),
    ] = None,
) -> Response:
    """Compatibility endpoint: /scraping/instruments → instrument master."""
    db_engine = getattr(request.app.state, "db_engine", None)

    # Query instrument_master table
    instruments: list[dict] = []
    cached = False

    if db_engine is not None:
        try:
            from sqlalchemy import text  # noqa: PLC0415
            async with db_engine.connect() as conn:
                if type:
                    rows = await conn.execute(
                        text(
                            "SELECT instrument_id, trading_symbol, display_symbol, isin, "
                            "exchange, segment, instrument_type, underlying, expiry, "
                            "strike, option_type, lot_size, tick_size, angel_token, upstox_key "
                            "FROM instrument_master "
                            "WHERE exchange = :exchange AND instrument_type = :itype "
                            "AND active_to IS NULL "
                            "ORDER BY trading_symbol LIMIT 5000"
                        ),
                        {"exchange": exchange.upper(), "itype": type.upper()},
                    )
                else:
                    rows = await conn.execute(
                        text(
                            "SELECT instrument_id, trading_symbol, display_symbol, isin, "
                            "exchange, segment, instrument_type, underlying, expiry, "
                            "strike, option_type, lot_size, tick_size, angel_token, upstox_key "
                            "FROM instrument_master "
                            "WHERE exchange = :exchange AND active_to IS NULL "
                            "ORDER BY trading_symbol LIMIT 5000"
                        ),
                        {"exchange": exchange.upper()},
                    )
                for row in rows.fetchall():
                    instruments.append(
                        {
                            "token": row.angel_token,
                            "tradingSymbol": row.trading_symbol,
                            "name": row.display_symbol or row.trading_symbol,
                            "exchange": row.exchange,
                            "segment": row.segment,
                            "instrumentType": row.instrument_type,
                            "isin": row.isin,
                            "expiry": row.expiry.isoformat() if row.expiry else None,
                            "strike": float(row.strike) if row.strike is not None else None,
                            "optionType": row.option_type,
                            "lotSize": row.lot_size,
                            "tickSize": float(row.tick_size) if row.tick_size is not None else 0.05,
                            "upstoxKey": row.upstox_key,
                        }
                    )
                cached = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "compat_instruments_db_failed",
                component="compat",
                exchange=exchange,
                error=str(exc),
            )

    # If DB returned nothing, log as a data gap (instrument_master needs backfill)
    if not instruments:
        logger.warning(
            "compat_instruments_empty",
            component="compat",
            exchange=exchange,
            note="instrument_master table is empty — backfill required via /v1/india/historical/backfill",
        )

    return _json_response(
        {
            "instruments": instruments,
            "count": len(instruments),
            "cached": cached,
            "exchange": exchange.upper(),
        }
    )


# ---------------------------------------------------------------------------
# POST /data/gate  — AlphaForge gate-client.ts compatibility
# ---------------------------------------------------------------------------

@router.post(
    "/data/gate",
    summary="[Compat] AlphaForge gate-client.ts — data quality gate",
    description=(
        "Compatibility shim for AlphaForge's gate-client.ts. "
        "Accepts the AlphaForge GateRequest schema and returns a GateResponse "
        "compatible with the signal engine's signalEngineAllowed check. "
        "Proxies to /v1/quality/evaluate internally."
    ),
    include_in_schema=True,
    tags=["Compat"],
)
async def compat_data_gate(request: Request) -> Response:
    """Compatibility endpoint: POST /data/gate → /v1/quality/evaluate logic."""
    try:
        body = await request.json()
    except Exception:
        return _json_response(
            _error_envelope("INVALID_BODY", "Request body must be valid JSON",
                            request_id=_request_id()),
            status_code=400,
        )

    symbol = body.get("symbol", "UNKNOWN")
    quote_age_ms = body.get("quoteAgeMs", 0)
    completeness_pct = body.get("completenessPercent", 100)
    timestamp_valid = body.get("timestampValid", True)
    cross_source_agreement = body.get("crossSourceAgreement", 1.0)
    sequence_integrity = body.get("sequenceIntegrity", True)
    max_quote_age_ms = body.get("maxQuoteAgeMs", 15_000)
    min_confidence_score = body.get("minConfidenceScore", 60)
    provider_available = body.get("providerAvailable", True)

    # Map AlphaForge gate request → DS2 quality observation
    obs = {
        "symbol": symbol,
        "quoteAgeMs": quote_age_ms,
        "completenessPercent": completeness_pct,
        "timestampValid": timestamp_valid,
        "providerAvailable": provider_available,
        "source": "scrapling",  # data-service2.0 is the source
        "provider": "data-service2.0",
    }

    # Evaluate using the quality engine
    try:
        from src.engines.quality_engine import QualityEngine  # noqa: PLC0415
        engine = QualityEngine()

        # Determine freshness status from quoteAgeMs vs maxQuoteAgeMs
        is_fresh = quote_age_ms <= max_quote_age_ms
        is_complete = completeness_pct >= 90
        has_provider = provider_available

        result = engine.evaluate_gate(
            symbol=symbol,
            freshness_status="FRESH" if is_fresh else "STALE",
            completeness_percent=float(completeness_pct),
            timestamp_valid=bool(timestamp_valid),
            provider_available=has_provider,
            cross_source_agreement=float(cross_source_agreement),
            sequence_integrity=bool(sequence_integrity),
            min_confidence_score=float(min_confidence_score),
        )

        # Map DS2 GateResult → AlphaForge GateResponse
        gate_conds = {
            "dataFresh":              result.conditions.data_fresh,
            "dataComplete":           result.conditions.data_complete,
            "dataTimestampValid":     result.conditions.data_timestamp_valid,
            "dataProviderHealthy":    result.conditions.data_provider_healthy,
            "dataSemanticallyValid":  result.conditions.data_semantically_valid,
        }
        signal_allowed = result.signal_engine_allowed
        block_reasons = result.block_reasons if not signal_allowed else None

        return _json_response({
            "signalEngineAllowed": signal_allowed,
            "confidenceScore":     result.confidence_score,
            "quality":             result.grade.value if hasattr(result.grade, "value") else str(result.grade),
            "quoteAgeMs":          quote_age_ms,
            "gates":               gate_conds,
            "blockReasons":        block_reasons,
            "circuitBreakers":     {},
            "evaluatedAt":         _utc_iso_now(),
        })
    except Exception as exc:  # noqa: BLE001
        # Fail closed — return a gate response that blocks signal engine
        return _json_response({
            "signalEngineAllowed": False,
            "confidenceScore":     0,
            "quality":             "UNKNOWN",
            "quoteAgeMs":          quote_age_ms,
            "gates": {
                "dataFresh":             False,
                "dataComplete":          False,
                "dataTimestampValid":    False,
                "dataProviderHealthy":   False,
                "dataSemanticallyValid": False,
            },
            "blockReasons":  ["data_service_error"],
            "circuitBreakers": {},
            "evaluatedAt":   _utc_iso_now(),
            "error":         str(exc),
        })
