"""
src/api/deribit.py

Deribit crypto options and futures REST API endpoints — Task 12.3.

Implements REST endpoints for Deribit market data (BTC, ETH, SOL):

``GET /v1/deribit/{currency}/overview``
    Options overview computed by :class:`~src.engines.options_overview.DeribitOptionsOverview`.
    Returns aggregated OI totals, put/call ratio, and per-contract summaries.
    Delegates to ``DeribitOptionsOverview.compute(currency, client)``.

``GET /v1/deribit/{currency}/instruments``
    Lists all Deribit instruments for the currency.  Optional ``?kind=option``
    query parameter (default ``"option"``).

``GET /v1/deribit/{currency}/index-price``
    Current index price for the currency (e.g. BTC → ``"btc_usd"``).

``GET /v1/deribit/ticker/{instrument_name}``
    Full ticker data for a single Deribit instrument.  Null fields (``mark_iv``,
    ``open_interest``, ``best_bid_price``, ``best_ask_price``) are preserved
    exactly as returned by Deribit — zero is never substituted.

``GET /v1/deribit/ohlcv/{instrument_name}``
    OHLCV candles from Deribit TradingView format.  Required query parameters:
    ``resolution`` (e.g. ``"1"``, ``"15"``, ``"60"``, ``"1D"``),
    ``start_ts`` (UTC epoch milliseconds), ``end_ts`` (UTC epoch milliseconds).

Null-semantics contract (Requirements 14.5, 6.4, 6.6)
-------------------------------------------------------
Every endpoint in this module preserves Deribit's ``null`` values for
``mark_iv``, ``open_interest``, ``best_bid_price``, ``best_ask_price``, and
``underlying_price`` verbatim.  Substituting zero for a missing value is
explicitly prohibited and would constitute a correctness bug.

Currency validation
-------------------
Only ``BTC``, ``ETH``, and ``SOL`` are supported.  Any other value returns
HTTP 400 with error code ``CURRENCY_NOT_SUPPORTED``.

Response envelope
-----------------
All responses use the canonical success envelope::

    {"data": <payload>, "metadata": {"requestedAt", "dataAsOf",
                                      "dataSourceType", "provider"}}

All error responses use the canonical error envelope::

    {"error": {"code": <str>, "message": <str>, "provider": <str | null>,
               "retryAfterMs": <int | null>, "requestId": <str>}}

Requirements: 14.1, 14.2, 14.4, 14.5, 14.6, 6.2, 6.4, 6.6
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from src.engines.options_overview import DeribitOptionsOverview
from src.providers.deribit_client import DERIBIT_CURRENCIES, DeribitClient

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Supported Deribit OHLCV resolutions
# ---------------------------------------------------------------------------

#: Deribit supports the following resolution strings.
#: Minutes: "1", "3", "5", "10", "15", "30", "60", "120", "180", "360", "720"
#: Other:   "1D"
DERIBIT_RESOLUTIONS: frozenset[str] = frozenset(
    ["1", "3", "5", "10", "15", "30", "60", "120", "180", "360", "720", "1D"]
)

# ---------------------------------------------------------------------------
# JSON response helpers (identical pattern to crypto.py / india.py)
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
    provider: str = "deribit",
    data_source_type: str = "LIVE",
) -> dict[str, Any]:
    """Build the canonical success response envelope."""
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
    provider: Optional[str] = "deribit",
    retry_after_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Build the canonical error response envelope."""
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
# Helper: resolve DeribitClient from app state or lazily create one
# ---------------------------------------------------------------------------


def _get_deribit_client(request: Request) -> DeribitClient:
    """Resolve the :class:`DeribitClient` from application state.

    If not yet attached (e.g. during testing or incremental development),
    a default instance is created on-demand and cached on ``app.state``.
    This avoids the need for a mandatory startup dependency while allowing
    production code to inject a pre-configured client.
    """
    client = getattr(request.app.state, "deribit_client", None)
    if client is None:
        client = DeribitClient()
        request.app.state.deribit_client = client
    return client


# ---------------------------------------------------------------------------
# Helper: validate supported currency
# ---------------------------------------------------------------------------

_SUPPORTED_CURRENCIES_MSG = ", ".join(sorted(DERIBIT_CURRENCIES))


def _validate_currency(currency: str, req_id: str) -> Optional[Response]:
    """Return a 400 error response if *currency* is not supported, else None."""
    if currency.upper() not in DERIBIT_CURRENCIES:
        return _json_response(
            _error_envelope(
                "CURRENCY_NOT_SUPPORTED",
                f"Currency '{currency}' is not supported. "
                f"Supported currencies: {_SUPPORTED_CURRENCIES_MSG}.",
                request_id=req_id,
            ),
            status_code=400,
        )
    return None


# ---------------------------------------------------------------------------
# GET /v1/deribit/{currency}/overview
# ---------------------------------------------------------------------------


@router.get(
    "/deribit/{currency}/overview",
    summary="Deribit options overview for a currency",
    description=(
        "Returns an aggregated options overview for BTC, ETH, or SOL. "
        "Includes per-contract summaries (mark price, IV, OI, bid/ask spread), "
        "total call/put OI, and put/call OI ratio. "
        "``pcrOi`` is ``null`` when total call OI is zero — never substituted with zero or infinity. "
        "``mark_iv``, ``open_interest``, ``best_bid``, ``best_ask`` are preserved as "
        "``null`` when Deribit does not supply them — zero is never substituted. "
        "(Requirements 14.4, 14.5, 14.6, 6.4, 6.6)"
    ),
    response_class=Response,
)
async def get_options_overview(
    request: Request,
    currency: str,
) -> Response:
    """Return aggregated Deribit options overview for *currency*.

    Delegates to :meth:`DeribitOptionsOverview.compute` which:

    1. Fetches all option instruments for the currency.
    2. Fetches the current index price.
    3. Fetches ticker data for each instrument (preserving null fields).
    4. Computes OI totals and put/call ratio.

    The ``put_call_oi_ratio`` is ``None`` (not zero) when there are no call
    contracts with non-null OI, signalling the ratio is genuinely unavailable.

    Args:
        currency: Three-letter code — ``BTC``, ``ETH``, or ``SOL``.

    Returns:
        200 with :class:`~src.engines.options_overview.OptionsOverviewResult`
        serialised inside the canonical envelope.

        400 if *currency* is not in ``{BTC, ETH, SOL}``.

        502 if the Deribit provider call fails.
    """
    req_id = _request_id()

    # ── Validate currency ──────────────────────────────────────────────────
    err = _validate_currency(currency, req_id)
    if err is not None:
        return err

    currency_upper = currency.upper()
    client = _get_deribit_client(request)
    engine = DeribitOptionsOverview()

    try:
        result = await engine.compute(currency_upper, client)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "deribit_api.overview_failed",
            component="deribit_api",
            currency=currency_upper,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to compute options overview for {currency_upper}: {exc}",
                request_id=req_id,
            ),
            status_code=502,
        )

    # Serialise Pydantic model to a plain dict (handles nested models too).
    data = result.model_dump()

    return _json_response(
        _success_envelope(
            data,
            data_as_of=result.computed_at,
            data_source_type="LIVE",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/deribit/{currency}/instruments
# ---------------------------------------------------------------------------


@router.get(
    "/deribit/{currency}/instruments",
    summary="List Deribit instruments for a currency",
    description=(
        "Returns all Deribit instruments for BTC, ETH, or SOL. "
        "Use the optional ``?kind=option`` (default) query parameter to filter by "
        "instrument kind (``option``, ``future``, ``spot``, etc.). "
        "(Requirement 14.1)"
    ),
    response_class=Response,
)
async def get_instruments(
    request: Request,
    currency: str,
    kind: Annotated[
        str,
        Query(
            description=(
                "Instrument kind to filter by. "
                "Valid values: ``option``, ``future``, ``spot``. "
                "Defaults to ``option``."
            )
        ),
    ] = "option",
) -> Response:
    """Return a list of Deribit instruments for *currency*.

    Args:
        currency: Three-letter code — ``BTC``, ``ETH``, or ``SOL``.
        kind:     Instrument kind filter.  Defaults to ``"option"``.

    Returns:
        200 with list of instrument dicts from Deribit.

        400 if *currency* is not supported.

        502 if the Deribit provider call fails.
    """
    req_id = _request_id()

    err = _validate_currency(currency, req_id)
    if err is not None:
        return err

    currency_upper = currency.upper()
    client = _get_deribit_client(request)

    try:
        instruments = await client.get_instruments(currency=currency_upper, kind=kind)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "deribit_api.instruments_failed",
            component="deribit_api",
            currency=currency_upper,
            kind=kind,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch instruments for {currency_upper}: {exc}",
                request_id=req_id,
            ),
            status_code=502,
        )

    return _json_response(
        _success_envelope(
            instruments,
            data_source_type="LIVE",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/deribit/{currency}/index-price
# ---------------------------------------------------------------------------


@router.get(
    "/deribit/{currency}/index-price",
    summary="Deribit index price for a currency",
    description=(
        "Returns the current index price for BTC, ETH, or SOL. "
        "BTC maps to ``btc_usd``, ETH to ``eth_usd``, SOL to ``sol_usd``. "
        "(Requirement 14.2)"
    ),
    response_class=Response,
)
async def get_index_price(
    request: Request,
    currency: str,
) -> Response:
    """Return the current Deribit index price for *currency*.

    Maps currency codes to Deribit index names:
    - ``BTC`` → ``btc_usd``
    - ``ETH`` → ``eth_usd``
    - ``SOL`` → ``sol_usd``

    Args:
        currency: Three-letter code — ``BTC``, ``ETH``, or ``SOL``.

    Returns:
        200 with ``{"index_name": str, "index_price": float}`` inside the
        canonical envelope.

        400 if *currency* is not supported.

        502 if the Deribit provider call fails.
    """
    req_id = _request_id()

    err = _validate_currency(currency, req_id)
    if err is not None:
        return err

    currency_upper = currency.upper()
    index_name = f"{currency_upper.lower()}_usd"
    client = _get_deribit_client(request)

    try:
        index_data = await client.get_index_price(index_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "deribit_api.index_price_failed",
            component="deribit_api",
            currency=currency_upper,
            index_name=index_name,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch index price for {currency_upper}: {exc}",
                request_id=req_id,
            ),
            status_code=502,
        )

    return _json_response(
        _success_envelope(
            index_data,
            data_source_type="LIVE",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/deribit/ticker/{instrument_name}
# ---------------------------------------------------------------------------


@router.get(
    "/deribit/ticker/{instrument_name:path}",
    summary="Deribit ticker data for a single instrument",
    description=(
        "Returns full ticker data for the given Deribit instrument. "
        "Null fields (``mark_iv``, ``open_interest``, ``best_bid_price``, "
        "``best_ask_price``) are **preserved as null** — zero is never substituted. "
        "This is a correctness requirement (Requirements 14.5, 6.4, 6.6). "
        "Example instrument names: ``BTC-27DEC24-100000-C``, ``BTC-PERPETUAL``."
    ),
    response_class=Response,
)
async def get_ticker(
    request: Request,
    instrument_name: str,
) -> Response:
    """Return Deribit ticker data for *instrument_name*.

    The ticker dict is passed through verbatim from Deribit.  This ensures
    that ``mark_iv``, ``open_interest``, ``best_bid_price``, and
    ``best_ask_price`` remain ``null`` when not supplied by the exchange.

    Args:
        instrument_name: A Deribit instrument name, e.g.
            ``"BTC-27DEC24-100000-C"`` or ``"BTC-PERPETUAL"``.
            The ``:path`` converter allows slashes in the name.

    Returns:
        200 with the raw Deribit ticker dict inside the canonical envelope.

        502 if the Deribit provider call fails.
    """
    req_id = _request_id()
    client = _get_deribit_client(request)

    try:
        ticker = await client.get_ticker(instrument_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "deribit_api.ticker_failed",
            component="deribit_api",
            instrument_name=instrument_name,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch ticker for '{instrument_name}': {exc}",
                request_id=req_id,
            ),
            status_code=502,
        )

    return _json_response(
        _success_envelope(
            ticker,
            data_source_type="LIVE",
        )
    )


# ---------------------------------------------------------------------------
# GET /v1/deribit/ohlcv/{instrument_name}
# ---------------------------------------------------------------------------


@router.get(
    "/deribit/ohlcv/{instrument_name:path}",
    summary="Deribit OHLCV candlestick data",
    description=(
        "Returns OHLCV candles for the given Deribit instrument in the canonical "
        "``{time, open, high, low, close, volume}`` format. "
        "Required query parameters: ``resolution``, ``start_ts`` (UTC epoch ms), "
        "``end_ts`` (UTC epoch ms). "
        "Supported resolutions: " + ", ".join(sorted(DERIBIT_RESOLUTIONS)) + ". "
        "(Requirement 14.1)"
    ),
    response_class=Response,
)
async def get_ohlcv(
    request: Request,
    instrument_name: str,
    resolution: Annotated[
        str,
        Query(
            description=(
                "Candle resolution. Minutes as string: "
                + ", ".join(r for r in sorted(DERIBIT_RESOLUTIONS) if r != "1D")
                + ". Or '1D' for daily candles."
            )
        ),
    ],
    start_ts: Annotated[
        int,
        Query(
            description="Start timestamp as UTC epoch milliseconds.",
            gt=0,
        ),
    ],
    end_ts: Annotated[
        int,
        Query(
            description="End timestamp as UTC epoch milliseconds.",
            gt=0,
        ),
    ],
) -> Response:
    """Return Deribit OHLCV candles for *instrument_name*.

    Deribit returns data in TradingView format (parallel arrays); the client
    normalises it to a list of canonical dicts with keys
    ``{time, open, high, low, close, volume}``.

    Args:
        instrument_name: Deribit instrument name, e.g. ``"BTC-PERPETUAL"``.
        resolution:      Candle resolution — minutes as string or ``"1D"``.
        start_ts:        Start timestamp in UTC epoch milliseconds.
        end_ts:          End timestamp in UTC epoch milliseconds.

    Returns:
        200 with a list of OHLCV dicts inside the canonical envelope.

        400 if *resolution* is not supported or if ``start_ts >= end_ts``.

        502 if the Deribit provider call fails.
    """
    req_id = _request_id()

    # ── Validate resolution ────────────────────────────────────────────────
    if resolution not in DERIBIT_RESOLUTIONS:
        return _json_response(
            _error_envelope(
                "RESOLUTION_NOT_SUPPORTED",
                f"Resolution '{resolution}' is not supported. "
                f"Supported resolutions: {', '.join(sorted(DERIBIT_RESOLUTIONS))}.",
                request_id=req_id,
            ),
            status_code=400,
        )

    # ── Validate timestamp ordering ────────────────────────────────────────
    if start_ts >= end_ts:
        return _json_response(
            _error_envelope(
                "INVALID_PARAMETER",
                "'start_ts' must be earlier than 'end_ts'.",
                request_id=req_id,
            ),
            status_code=400,
        )

    client = _get_deribit_client(request)

    try:
        candles = await client.get_ohlcv(
            instrument_name=instrument_name,
            resolution=resolution,
            start_ts=start_ts,
            end_ts=end_ts,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "deribit_api.ohlcv_failed",
            component="deribit_api",
            instrument_name=instrument_name,
            resolution=resolution,
            error=str(exc),
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Failed to fetch OHLCV data for '{instrument_name}': {exc}",
                request_id=req_id,
            ),
            status_code=502,
        )

    data_as_of = _utc_iso_now()
    if candles:
        # Use the time of the last candle as dataAsOf.
        data_as_of = datetime.fromtimestamp(
            candles[-1]["time"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    return _json_response(
        _success_envelope(
            candles,
            data_as_of=data_as_of,
            data_source_type="HISTORICAL",
        )
    )
