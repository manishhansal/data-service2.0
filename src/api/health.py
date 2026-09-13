"""
Health check endpoints for DATA-SERVICE 2.0.

Task 1.5 — Requirements 16.5, 16.6, 16.7, 20.3, 20.4

Three endpoints:

``GET /v1/health/live``
    Liveness probe.  Always returns HTTP 200 within 200 ms.  Never blocks on
    any external dependency (Requirement 20.3, 16.5).

``GET /v1/health/ready``
    Readiness probe.  Returns HTTP 200 when both Redis and PostgreSQL respond
    within 2 000 ms.  Returns HTTP 503 with a ``capabilities`` map when any
    critical dependency is unavailable (Requirement 20.4, 16.6).

``GET /v1/health/data``
    Operational health snapshot.  Returns session state, freshness stats,
    gap counts, duplicate rates, circuit-breaker states, and clock-skew
    flag (Requirement 16.7).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

# Module-level logger (structlog).
_log = structlog.get_logger(__name__)

# Service version — kept in sync with pyproject.toml.
_VERSION = "2.0.0"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _uptime_ms(request: Request) -> float:
    """Return platform uptime in milliseconds using the start-time stored in
    ``app.state.start_time_ms`` at startup."""
    start: float = getattr(request.app.state, "start_time_ms", time.monotonic() * 1000)
    return (time.monotonic() * 1000) - start


async def _ping_redis(request: Request, timeout_sec: float = 2.0) -> bool:
    """Return ``True`` when Redis responds to PING within *timeout_sec*."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return False
    try:
        await asyncio.wait_for(redis.ping(), timeout=timeout_sec)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _ping_postgres(request: Request, timeout_sec: float = 2.0) -> bool:
    """Return ``True`` when PostgreSQL responds to ``SELECT 1`` within
    *timeout_sec*."""
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return False
    try:
        from sqlalchemy import text  # noqa: PLC0415

        async def _check() -> None:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        await asyncio.wait_for(_check(), timeout=timeout_sec)
        return True
    except Exception:  # noqa: BLE001
        return False


def _utc_iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _circuit_breaker_states(request: Request) -> dict[str, Any]:
    """Collect current circuit-breaker states from app state if available.

    Returns a dict keyed by ``"{provider}:{capability}"`` with values being
    one of ``"CLOSED"``, ``"OPEN"``, or ``"HALF_OPEN"``.

    This is a best-effort read; if the circuit-breaker registry is not yet
    initialised, an empty dict is returned.
    """
    registry: Any = getattr(request.app.state, "circuit_breakers", None)
    if registry is None:
        return {}
    try:
        # Registry is expected to expose a dict-like interface once the
        # Provider Gateway (Task 4.2) is implemented.
        if callable(getattr(registry, "get_all_states", None)):
            return registry.get_all_states()
        # Fallback: if registry is already a plain dict, return it directly.
        if isinstance(registry, dict):
            return registry
    except Exception:  # noqa: BLE001
        pass
    return {}


def _freshness_stats(request: Request) -> dict[str, Any]:
    """Return rolling freshness statistics stored in app state.

    Populated by the Market Engine (Task 6.3) when quotes are published.
    Returns a zeroed structure when not yet available.
    """
    stats: Any = getattr(request.app.state, "freshness_stats", None)
    if stats is not None and isinstance(stats, dict):
        return {
            "p50Ms": stats.get("p50_ms", 0),
            "p99Ms": stats.get("p99_ms", 0),
            "successRate": stats.get("success_rate", 1.0),
        }
    return {"p50Ms": 0, "p99Ms": 0, "successRate": 1.0}


def _gap_counts(request: Request) -> dict[str, int]:
    """Return current data-gap counts by severity from app state.

    Populated by the Historical Engine (Task 7.3).
    Returns zeroed structure when not yet available.
    """
    counts: Any = getattr(request.app.state, "gap_counts", None)
    if counts is not None and isinstance(counts, dict):
        return {
            "low": int(counts.get("LOW", 0)),
            "medium": int(counts.get("MEDIUM", 0)),
            "high": int(counts.get("HIGH", 0)),
        }
    return {"low": 0, "medium": 0, "high": 0}


def _duplicate_rate(request: Request) -> float:
    """Return the current duplicate-tick rate from app state.

    Populated by the Streaming Engine (Task 8.5).
    Returns 0.0 when not yet available.
    """
    return float(getattr(request.app.state, "duplicate_rate", 0.0))


def _session_phase(request: Request) -> str:
    """Return the current NSE session phase from app state.

    Populated by the Market Session Engine (Task 6.1).
    Returns ``"UNKNOWN"`` when not yet available.
    """
    return str(getattr(request.app.state, "session_phase", "UNKNOWN"))


def _clock_degraded(request: Request) -> bool:
    """Return whether NTP clock skew exceeds 500 ms.

    Set by the clock-skew monitor (Task 9.8).
    Returns ``False`` when the monitor is not yet running.
    """
    return bool(getattr(request.app.state, "clock_degraded", False))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/v1/health/live",
    summary="Liveness probe",
    description=(
        "Always returns HTTP 200 within 200 ms. "
        "MUST NOT block on any external dependency (Requirements 16.5, 20.3)."
    ),
    response_class=JSONResponse,
    tags=["Health"],
)
async def health_live(request: Request) -> JSONResponse:
    """Liveness probe — always HTTP 200, never blocks on external deps.

    Response body::

        {
            "status": "alive",
            "version": "2.0.0",
            "uptimeMs": 12345,
            "timestamp": "2026-01-15T09:15:00.000Z"
        }
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "alive",
            "version": _VERSION,
            "uptimeMs": round(_uptime_ms(request)),
            "timestamp": _utc_iso_now(),
        },
    )


@router.get(
    "/v1/health/ready",
    summary="Readiness probe",
    description=(
        "Returns HTTP 200 when Redis and PostgreSQL both respond within 2 000 ms. "
        "Returns HTTP 503 with a ``capabilities`` map when any critical dependency "
        "is unavailable (Requirements 16.6, 20.4)."
    ),
    response_class=JSONResponse,
    tags=["Health"],
)
async def health_ready(request: Request) -> JSONResponse:
    """Readiness probe — checks Redis + PostgreSQL concurrently.

    Success response (HTTP 200)::

        {
            "status": "ready",
            "version": "2.0.0",
            "timestamp": "2026-01-15T09:15:00.000Z",
            "capabilities": {
                "redis": true,
                "postgres": true
            }
        }

    Failure response (HTTP 503)::

        {
            "status": "degraded",
            "version": "2.0.0",
            "timestamp": "2026-01-15T09:15:00.000Z",
            "capabilities": {
                "redis": false,
                "postgres": true
            }
        }
    """
    # Run both dependency checks concurrently within the 2 000 ms budget.
    redis_ok, pg_ok = await asyncio.gather(
        _ping_redis(request, timeout_sec=2.0),
        _ping_postgres(request, timeout_sec=2.0),
    )

    capabilities: dict[str, bool] = {
        "redis": redis_ok,
        "postgres": pg_ok,
    }
    all_ok = redis_ok and pg_ok
    status_code = 200 if all_ok else 503
    overall_status = "ready" if all_ok else "degraded"

    body: dict[str, Any] = {
        "status": overall_status,
        "version": _VERSION,
        "timestamp": _utc_iso_now(),
        "capabilities": capabilities,
    }

    if not all_ok:
        await _log.awarning(
            "readiness_check_failed",
            redis=redis_ok,
            postgres=pg_ok,
        )

    return JSONResponse(status_code=status_code, content=body)


@router.get(
    "/v1/health/data",
    summary="Data health snapshot",
    description=(
        "Returns current market session state, freshness statistics, gap counts, "
        "duplicate rates, circuit-breaker states, and NTP clock-skew flag "
        "(Requirement 16.7)."
    ),
    response_class=JSONResponse,
    tags=["Health"],
)
async def health_data(request: Request) -> JSONResponse:
    """Operational health snapshot for dashboards and monitoring.

    Response body::

        {
            "status": "ok",
            "version": "2.0.0",
            "timestamp": "2026-01-15T09:15:00.000Z",
            "uptimeMs": 12345,
            "marketSession": {
                "sessionPhase": "REGULAR"
            },
            "freshness": {
                "p50Ms": 80,
                "p99Ms": 450,
                "successRate": 0.998
            },
            "gaps": {
                "low": 2,
                "medium": 0,
                "high": 0
            },
            "duplicateRate": 0.001,
            "circuitBreakers": {
                "angel_one:historical_ohlcv": "CLOSED",
                ...
            },
            "clockDegraded": false
        }
    """
    now = _utc_iso_now()

    body: dict[str, Any] = {
        "status": "ok",
        "version": _VERSION,
        "timestamp": now,
        "uptimeMs": round(_uptime_ms(request)),
        "marketSession": {
            "sessionPhase": _session_phase(request),
        },
        "freshness": _freshness_stats(request),
        "gaps": _gap_counts(request),
        "duplicateRate": _duplicate_rate(request),
        "circuitBreakers": _circuit_breaker_states(request),
        "clockDegraded": _clock_degraded(request),
    }

    return JSONResponse(status_code=200, content=body)
