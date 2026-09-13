"""
src/api/internal.py

Internal service-to-service API endpoints for DATA-SERVICE 2.0.

These endpoints are intended for use by internal services (worker, scheduler,
backfill jobs) and are NOT part of the consumer-facing public API.  They
should be firewalled from external access in production deployments.

Implemented endpoints (Task 8.7 / Requirement 15.3):

``POST /v1/internal/dataset-ready``
    Allows an internal service (e.g. the backfill worker or gap-recovery
    scheduler) to trigger a ``DatasetReady`` event on the Redis Streams
    Event Bus.  The event notifies downstream consumers that a complete
    dataset is available for consumption.

    Request body (JSON):
        ``market``       — market identifier, e.g. ``"NSE"``, ``"CRYPTO"``
        ``symbol``       — trading symbol, e.g. ``"RELIANCE"``
        ``interval``     — canonical interval string, e.g. ``"1m"``, ``"1d"``
        ``date``         — session / reference date as ``"YYYY-MM-DD"``
        ``record_count`` — number of records in the completed dataset

    Response (200):
        Canonical success envelope with ``{"msgId": "<redis-stream-entry-id>"}``

    Error responses follow the canonical error envelope.

All responses use the canonical success envelope:
    {"data": <payload>, "metadata": {...}}

All error responses use the canonical error envelope:
    {"error": {"code": <str>, "message": <str>, "requestId": <str>}}

Requirements: 15.3, 8.7
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from src.engines.streaming_engine import StreamingEngine
from src.publishers.dataset_publisher import DatasetPublisher

router = APIRouter()
_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# JSON response helpers (consistent with other api modules)
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utc_iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _request_id() -> str:
    return str(uuid.uuid4())


def _json_response(content: Any, *, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(content),
        status_code=status_code,
        media_type="application/json",
    )


def _success_envelope(data: Any) -> dict[str, Any]:
    return {
        "data": data,
        "metadata": {
            "requestedAt": _utc_iso_now(),
            "dataSourceType": "INTERNAL",
        },
    }


def _error_envelope(
    code: str,
    message: str,
    *,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "provider": None,
            "retryAfterMs": None,
            "requestId": request_id or _request_id(),
        }
    }


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DatasetReadyRequest(BaseModel):
    """Request body for ``POST /v1/internal/dataset-ready``."""

    market: str = Field(..., min_length=1, max_length=16, description="Market identifier, e.g. 'NSE'")
    symbol: str = Field(..., min_length=1, max_length=64, description="Trading symbol, e.g. 'RELIANCE'")
    interval: str = Field(..., min_length=1, max_length=8, description="Canonical interval, e.g. '1m', '1d'")
    date: str = Field(..., description="Session/reference date as 'YYYY-MM-DD'")
    record_count: int = Field(..., ge=0, description="Number of records in the completed dataset")

    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        if not _DATE_RE.match(v):
            raise ValueError(f"date must be in YYYY-MM-DD format; got '{v}'")
        return v

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, v: str) -> str:
        # Reject 3m for Indian markets — enforced broadly; the internal endpoint
        # does not know the market type upfront so we only reject a bare "3m"
        # for safety.  The StreamingEngine will reject it at a lower layer too.
        if v == "3m":
            raise ValueError(
                "interval '3m' is permanently unsupported for Indian market data"
            )
        return v


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/internal/dataset-ready", tags=["Internal"])
async def trigger_dataset_ready(
    body: DatasetReadyRequest,
    request: Request,
) -> Response:
    """Trigger a ``DatasetReady`` event on the Redis Streams Event Bus.

    Called by internal services (backfill worker, gap-recovery scheduler,
    option-chain snapshot job) when a complete dataset becomes available.

    Returns the Redis stream entry ID on success so callers can trace the event.

    **This endpoint is for internal service-to-service use only.**
    It must not be exposed on the public-facing port in production.

    Body fields:
    - ``market``       — e.g. ``"NSE"``, ``"CRYPTO"``
    - ``symbol``       — e.g. ``"RELIANCE"``
    - ``interval``     — e.g. ``"1m"``, ``"1d"``
    - ``date``         — ``"YYYY-MM-DD"``
    - ``record_count`` — number of records in the dataset

    Returns HTTP 200 with ``{"msgId": "<stream-entry-id>"}`` on success.
    Returns HTTP 400 on validation failures.
    Returns HTTP 503 when the streaming engine is not available.
    """
    req_id = _request_id()

    # ── Resolve the StreamingEngine from app state ──────────────────────
    streaming_engine: Optional[StreamingEngine] = getattr(
        getattr(request, "app", None),
        "state",
        None,
    )
    if streaming_engine is not None:
        streaming_engine = getattr(streaming_engine, "streaming_engine", None)

    if streaming_engine is None:
        # Streaming engine not wired into app state — return 503 so callers
        # can implement retry logic on their side.
        await _log.awarning(
            "dataset_ready_endpoint_no_engine",
            market=body.market,
            symbol=body.symbol,
            request_id=req_id,
        )
        return _json_response(
            _error_envelope(
                "STREAMING_ENGINE_UNAVAILABLE",
                "The streaming engine is not available. "
                "The service may be starting up or in degraded mode.",
                request_id=req_id,
            ),
            status_code=503,
        )

    # ── Publish via DatasetPublisher (retries on transient failures) ────
    publisher = DatasetPublisher(engine=streaming_engine)
    try:
        msg_id = await publisher.publish(
            market=body.market,
            symbol=body.symbol,
            interval=body.interval,
            date=body.date,
            record_count=body.record_count,
        )
    except Exception as exc:  # noqa: BLE001
        await _log.aerror(
            "dataset_ready_endpoint_publish_failed",
            market=body.market,
            symbol=body.symbol,
            interval=body.interval,
            date=body.date,
            record_count=body.record_count,
            error=str(exc),
            request_id=req_id,
        )
        return _json_response(
            _error_envelope(
                "EVENT_PUBLISH_FAILED",
                f"Failed to publish dataset-ready event after retries: {exc}",
                request_id=req_id,
            ),
            status_code=502,
        )

    await _log.ainfo(
        "dataset_ready_event_published",
        market=body.market,
        symbol=body.symbol,
        interval=body.interval,
        date=body.date,
        record_count=body.record_count,
        msg_id=msg_id,
        request_id=req_id,
    )

    return _json_response(
        _success_envelope(
            {
                "msgId": msg_id,
                "market": body.market,
                "symbol": body.symbol,
                "interval": body.interval,
                "date": body.date,
                "record_count": body.record_count,
            }
        )
    )
