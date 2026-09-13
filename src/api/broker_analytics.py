"""
src/api/broker_analytics.py

Broker Analytics endpoints for DATA-SERVICE 2.0 — DS2-RCA-018 fix.

Exposes Angel One SmartAPI broker analytics data through DATA-SERVICE so
AlphaForge never needs to call Angel One directly.

Endpoints
---------
``GET /v1/india/broker-analytics/pcr``
    Put-Call Ratio sourced from Angel One SmartAPI.

``GET /v1/india/broker-analytics/oi-buildup``
    OI buildup (long buildup, short buildup, covering, unwinding).

``GET /v1/india/broker-analytics/gainers-losers``
    Top OI/price gainers and losers.

All responses use the canonical ``{"data": ..., "metadata": {...}}`` envelope.

Authentication / credential flow
---------------------------------
The adapter is resolved from ``request.app.state.angel_one_adapter``.
When not configured (credentials absent or adapter not initialised),
the endpoint returns HTTP 503 with ``PROVIDER_NOT_CONFIGURED`` — never a 500.

When Angel One returns 401 (token expired), the adapter's built-in
re-authentication runs once and retries automatically.

Requirements: DS2-RCA-018 (broker analytics routes), Req 21
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import Response

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response helpers (same pattern as india.py)
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _request_id() -> str:
    return str(uuid.uuid4())


def _json_response(content: Any, *, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(content),
        status_code=status_code,
        media_type="application/json",
    )


def _success_envelope(
    data: Any,
    *,
    provider: str = "angel_one",
    data_source_type: str = "LIVE",
) -> dict[str, Any]:
    return {
        "data": data,
        "metadata": {
            "requestedAt": _utc_iso_now(),
            "dataAsOf": _utc_iso_now(),
            "dataSourceType": data_source_type,
            "provider": provider,
        },
    }


def _error_envelope(
    code: str,
    message: str,
    *,
    request_id: Optional[str] = None,
    provider: Optional[str] = "angel_one",
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
# Adapter resolver
# ---------------------------------------------------------------------------


def _get_angel_adapter(request: Request) -> Any:
    """Resolve the AngelOneAdapter from application state.

    Returns ``None`` when not configured — callers check and return 503.
    """
    return getattr(request.app.state, "angel_one_adapter", None)


# ---------------------------------------------------------------------------
# GET /v1/india/broker-analytics/pcr
# ---------------------------------------------------------------------------


@router.get(
    "/india/broker-analytics/pcr",
    summary="Put-Call Ratio (Angel One SmartAPI)",
    description=(
        "Returns the Put-Call Ratio for tracked index derivatives, sourced "
        "from Angel One SmartAPI. Requires Angel One credentials to be "
        "configured. Returns HTTP 503 when the adapter is not initialised. "
        "(DS2-RCA-018, Requirement 21)"
    ),
    response_class=Response,
)
async def get_pcr(request: Request) -> Response:
    """Return Put-Call Ratio from Angel One SmartAPI.

    Response data shape::

        {
          "putCallRatio": float | null,
          "provider": "angel_one",
          "sourceType": "BROKER_AUTHENTICATED",
          "fetchedAt": "2026-09-13T10:00:00.000Z"
        }

    Returns HTTP 503 with ``PROVIDER_NOT_CONFIGURED`` when Angel One
    credentials are absent from the adapter's configuration.
    Returns HTTP 502 with ``PROVIDER_UNAVAILABLE`` on transient adapter errors.
    """
    req_id = _request_id()
    adapter = _get_angel_adapter(request)

    if adapter is None:
        return _json_response(
            _error_envelope(
                "PROVIDER_NOT_CONFIGURED",
                "Angel One adapter is not initialised. "
                "Configure ANGEL_ONE_API_KEY, ANGEL_ONE_CLIENT_ID, and "
                "ANGEL_ONE_TOTP_SECRET environment variables.",
                request_id=req_id,
            ),
            status_code=503,
        )

    try:
        data = await adapter.fetch_pcr()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "broker_analytics.pcr_failed",
            component="broker_analytics",
            error=str(exc)[:120],
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Angel One PCR fetch failed: {exc}",
                request_id=req_id,
            ),
            status_code=502,
        )

    return _json_response(_success_envelope(data))


# ---------------------------------------------------------------------------
# GET /v1/india/broker-analytics/oi-buildup
# ---------------------------------------------------------------------------


@router.get(
    "/india/broker-analytics/oi-buildup",
    summary="OI Buildup (Angel One SmartAPI)",
    description=(
        "Returns Open Interest buildup data (long buildup, short buildup, "
        "long covering, short covering) from Angel One SmartAPI. "
        "(DS2-RCA-018, Requirement 21)"
    ),
    response_class=Response,
)
async def get_oi_buildup(request: Request) -> Response:
    """Return OI buildup records from Angel One SmartAPI.

    Response ``data`` is a list of OI buildup records::

        [
          {
            "symbol": "...",
            "oi": int | null,
            "oiChange": int | null,
            "buildupType": "LONG_BUILDUP" | "SHORT_BUILDUP" | ...,
            "provider": "angel_one",
            "sourceType": "BROKER_AUTHENTICATED",
            "fetchedAt": "...",
            ...
          },
          ...
        ]
    """
    req_id = _request_id()
    adapter = _get_angel_adapter(request)

    if adapter is None:
        return _json_response(
            _error_envelope(
                "PROVIDER_NOT_CONFIGURED",
                "Angel One adapter is not initialised. "
                "Configure ANGEL_ONE_API_KEY, ANGEL_ONE_CLIENT_ID, and "
                "ANGEL_ONE_TOTP_SECRET environment variables.",
                request_id=req_id,
            ),
            status_code=503,
        )

    try:
        records = await adapter.fetch_oi_buildup()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "broker_analytics.oi_buildup_failed",
            component="broker_analytics",
            error=str(exc)[:120],
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Angel One OI buildup fetch failed: {exc}",
                request_id=req_id,
            ),
            status_code=502,
        )

    return _json_response(_success_envelope(records))


# ---------------------------------------------------------------------------
# GET /v1/india/broker-analytics/gainers-losers
# ---------------------------------------------------------------------------


@router.get(
    "/india/broker-analytics/gainers-losers",
    summary="Top OI/price gainers and losers (Angel One SmartAPI)",
    description=(
        "Returns top OI gainers, OI losers, price gainers, and price losers "
        "from Angel One SmartAPI. "
        "(DS2-RCA-018, Requirement 21)"
    ),
    response_class=Response,
)
async def get_gainers_losers(request: Request) -> Response:
    """Return top gainers and losers from Angel One SmartAPI.

    Response ``data`` shape::

        {
          "oiGainers":    [...],
          "oiLosers":     [...],
          "priceGainers": [...],
          "priceLosers":  [...],
          "provider":     "angel_one",
          "sourceType":   "BROKER_AUTHENTICATED",
          "fetchedAt":    "..."
        }
    """
    req_id = _request_id()
    adapter = _get_angel_adapter(request)

    if adapter is None:
        return _json_response(
            _error_envelope(
                "PROVIDER_NOT_CONFIGURED",
                "Angel One adapter is not initialised. "
                "Configure ANGEL_ONE_API_KEY, ANGEL_ONE_CLIENT_ID, and "
                "ANGEL_ONE_TOTP_SECRET environment variables.",
                request_id=req_id,
            ),
            status_code=503,
        )

    try:
        data = await adapter.fetch_gainers_losers()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "broker_analytics.gainers_losers_failed",
            component="broker_analytics",
            error=str(exc)[:120],
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_UNAVAILABLE",
                f"Angel One gainers/losers fetch failed: {exc}",
                request_id=req_id,
            ),
            status_code=502,
        )

    return _json_response(_success_envelope(data))
