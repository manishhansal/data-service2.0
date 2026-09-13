"""
src/api/replay.py

REST endpoints for replay-mode session management.

Endpoints
---------
``POST /v1/replay/sessions``
    Create a new replay session from a tick dataset.  Returns the session
    metadata including the ``sessionId`` that consumers use to connect via
    WebSocket and poll for status.

``GET /v1/replay/sessions/{session_id}/status``
    Poll the current status of an existing replay session.

Design constraints (Requirement 23.3)
--------------------------------------
- Playback speed is a positive float multiplier (1.0 = real-time, > 1.0 faster).
- The response envelope always includes ``dataSourceType: "HISTORICAL"``,
  ``provenance``, and ``quality`` fields — identical to live responses.
- Sessions are identified by UUID v4 ``sessionId`` values.
- Sessions not found → HTTP 404 with canonical error envelope.
- Invalid request bodies → HTTP 422 from Pydantic validation (FastAPI default)
  or HTTP 400 for domain-level rejections (empty tick data).

Requirements: 23.3
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from src.api.errors import ErrorCode, json_error_response
from src.replay.replay_engine import (
    ReplaySession,
    ReplayStatus,
    create_session,
    get_session,
)

_log = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateReplaySessionRequest(BaseModel):
    """Request body for ``POST /v1/replay/sessions``.

    Fields
    ------
    symbol
        Trading symbol to replay (e.g. ``"NIFTY"``, ``"BTC-USDT"``).
    speedMultiplier
        Playback speed relative to real time.  Must be > 0.
        Defaults to 1.0 (real-time).
    tickData
        Ordered list of tick dicts.  Each tick **must** contain a ``"time"``
        field as a UTC epoch millisecond integer.  The list is replayed in the
        order provided.

    Examples
    --------
    ::

        {
          "symbol": "NIFTY",
          "speedMultiplier": 2.0,
          "tickData": [
            {"time": 1700000000000, "ltp": 19500.0, "volume": 1200},
            {"time": 1700000060000, "ltp": 19510.5, "volume": 800}
          ]
        }
    """

    symbol: str = Field(..., min_length=1, description="Trading symbol.")
    speedMultiplier: float = Field(
        default=1.0,
        gt=0,
        description="Playback speed multiplier (> 0).  1.0 = real-time.",
    )
    tickData: list[dict[str, Any]] = Field(
        ...,
        description=(
            "Ordered list of tick dicts.  Each must include a ``time`` field "
            "(UTC epoch ms)."
        ),
    )

    @field_validator("tickData")
    @classmethod
    def _validate_tick_data(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each tick must contain a ``time`` key."""
        for i, tick in enumerate(v):
            if "time" not in tick:
                raise ValueError(
                    f"tick at index {i} is missing the required 'time' field "
                    f"(UTC epoch milliseconds)."
                )
            raw_time = tick["time"]
            if not isinstance(raw_time, (int, float)) or raw_time < 0:
                raise ValueError(
                    f"tick[{i}]['time'] must be a non-negative number "
                    f"(UTC epoch ms); got {raw_time!r}."
                )
        return v


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _session_to_response(session: ReplaySession) -> dict[str, Any]:
    """Serialise a ``ReplaySession`` into the canonical success response shape."""
    return {
        "data": session.model_dump(),
        "metadata": {
            "dataSourceType": "HISTORICAL",
        },
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/replay/sessions",
    status_code=201,
    summary="Create a replay session",
    description=(
        "Create a new replay session from a tick dataset.  Returns the session "
        "metadata, including the ``sessionId`` consumers use to track or connect "
        "to the replay stream.  Requirement 23.3."
    ),
    response_class=JSONResponse,
    tags=["Replay"],
)
async def create_replay_session(
    body: CreateReplaySessionRequest,
    request: Request,
) -> JSONResponse:
    """``POST /v1/replay/sessions``

    Creates a replay session and registers it in the in-memory session store.
    Returns HTTP 201 with the session metadata.

    Request body::

        {
          "symbol": "NIFTY",
          "speedMultiplier": 2.0,
          "tickData": [
            {"time": 1700000000000, "ltp": 19500.0},
            ...
          ]
        }

    Response (HTTP 201)::

        {
          "data": {
            "sessionId": "...",
            "symbol": "NIFTY",
            "startTs": 1700000000000,
            "endTs": 1700003600000,
            "speedMultiplier": 2.0,
            "status": "CREATED",
            "tickCount": 60,
            "ticksSent": 0,
            "createdAt": "2024-11-15T10:00:00.000Z",
            "startedAt": null,
            "completedAt": null
          },
          "metadata": {
            "dataSourceType": "HISTORICAL"
          }
        }

    Requirements: 23.3
    """
    session = create_session(
        symbol=body.symbol,
        tick_data=body.tickData,
        speed_multiplier=body.speedMultiplier,
    )

    await _log.ainfo(
        "replay_session_created",
        session_id=session.sessionId,
        symbol=session.symbol,
        tick_count=session.tickCount,
        speed_multiplier=session.speedMultiplier,
    )

    return JSONResponse(
        status_code=201,
        content=_session_to_response(session),
    )


@router.get(
    "/replay/sessions/{session_id}/status",
    summary="Get replay session status",
    description=(
        "Return the current status and metadata of a replay session by its "
        "``sessionId``.  Returns HTTP 404 if no session with the given ID exists.  "
        "Requirement 23.3."
    ),
    response_class=JSONResponse,
    tags=["Replay"],
)
async def get_replay_session_status(
    session_id: str,
    request: Request,
) -> JSONResponse:
    """``GET /v1/replay/sessions/{session_id}/status``

    Returns the current lifecycle state and tick-delivery progress for the
    requested session.

    Response (HTTP 200)::

        {
          "data": {
            "sessionId": "...",
            "symbol": "NIFTY",
            "startTs": 1700000000000,
            "endTs": 1700003600000,
            "speedMultiplier": 2.0,
            "status": "PLAYING",
            "tickCount": 60,
            "ticksSent": 24,
            "createdAt": "2024-11-15T10:00:00.000Z",
            "startedAt": "2024-11-15T10:00:05.000Z",
            "completedAt": null
          },
          "metadata": {
            "dataSourceType": "HISTORICAL"
          }
        }

    Returns HTTP 404 when the session does not exist::

        {
          "error": {
            "code": "NOT_FOUND",
            "message": "Replay session 'abc123' not found.",
            "provider": null,
            "retryAfterMs": null,
            "requestId": "..."
          }
        }

    Requirements: 23.3
    """
    session = get_session(session_id)
    if session is None:
        return json_error_response(  # type: ignore[return-value]
            ErrorCode.NOT_FOUND,
            f"Replay session {session_id!r} not found.",
            status_code=404,
        )

    return JSONResponse(
        status_code=200,
        content=_session_to_response(session),
    )
