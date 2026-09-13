"""
src/api/analytics.py

Broker / Platform analytics endpoints for DATA-SERVICE 2.0.

Endpoints
---------
``GET /v1/analytics/providers``
    List all registered providers with their health status, data freshness,
    and circuit-breaker state.

``GET /v1/analytics/providers/{provider_id}``
    Detailed health metrics for a single provider:
    providerHealthy, circuitBreakerState, failureRate, lastSuccessAt,
    lastFailureAt, avgResponseTimeMs, totalRequests, successfulRequests,
    failedRequests.

``GET /v1/analytics/quality``
    Aggregate quality metrics across all active symbols:
    averageScore, blockedCount, lowCount, mediumCount, highCount,
    stalemaskCount.

``GET /v1/analytics/health``
    Overall platform health check — HTTP 200 when healthy,
    HTTP 503 when degraded.

All responses use the canonical ``{"data": ..., "metadata": {...}}`` envelope
(Requirement 16.1, 16.2).

Provider health data is sourced from ``request.app.state``:
* ``request.app.state.provider_registry``   — dict[str, dict]  (optional)
* ``request.app.state.quality_scores``      — dict[str, int]   (optional)

When the registry or quality store is absent, safe defaults are returned so
the endpoints never raise 500.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()
_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Envelope helpers  (same pattern as india.py / providers.py)
# ---------------------------------------------------------------------------


def _utc_iso_now() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _request_id() -> str:
    return str(uuid.uuid4())


def _success_envelope(
    data: Any,
    *,
    data_as_of: Optional[str] = None,
    data_source_type: str = "LIVE",
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Wrap *data* in the canonical success envelope."""
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
    """Wrap an error in the canonical error envelope."""
    return {
        "error": {
            "code": code,
            "message": message,
            "provider": provider,
            "retryAfterMs": retry_after_ms,
            "requestId": request_id or _request_id(),
        }
    }


import json as _json


def _json_response(content: Any, *, status_code: int = 200) -> Response:
    body = _json.dumps(content)
    return Response(content=body, status_code=status_code, media_type="application/json")


# ---------------------------------------------------------------------------
# Internal helpers — provider registry
# ---------------------------------------------------------------------------

# Well-known provider IDs mirroring the ProviderId enum in schemas/provider.py.
_KNOWN_PROVIDERS: list[str] = [
    "angel_one",
    "upstox",
    "scrapling_nse",
    "jugaad_data",
    "openchart",
    "yahoo_finance",
    "binance",
    "deribit",
    "delta",   # DS2-RCA-001 fix — Delta Exchange India
]

# Valid circuit-breaker state strings.
_CIRCUIT_STATES = frozenset({"CLOSED", "OPEN", "HALF_OPEN"})


def _default_provider_record(provider_id: str) -> dict[str, Any]:
    """Return a safe zero-value health record for *provider_id*."""
    return {
        "providerId": provider_id,
        "providerHealthy": True,
        "circuitBreakerState": "CLOSED",
        "failureRate": 0.0,
        "lastSuccessAt": None,
        "lastFailureAt": None,
        "avgResponseTimeMs": None,
        "totalRequests": 0,
        "successfulRequests": 0,
        "failedRequests": 0,
    }


def _get_provider_registry(request: Request) -> dict[str, dict[str, Any]]:
    """Resolve the provider registry from ``app.state``.

    Falls back to a registry built from the Capability_Matrix when
    ``app.state.provider_registry`` is absent.

    Returns:
        dict keyed by provider_id (str) → health record dict.
    """
    registry: Any = getattr(request.app.state, "provider_registry", None)

    # If a live registry is attached, use it directly.
    if isinstance(registry, dict) and registry:
        return registry  # type: ignore[return-value]

    # Build from Capability_Matrix defaults.
    result: dict[str, dict[str, Any]] = {}
    try:
        from src.providers.capability_matrix import _MATRIX  # noqa: PLC0415

        seen: set[str] = set()
        for cap in _MATRIX:
            pid = cap.provider.value
            if pid not in seen:
                seen.add(pid)
                result[pid] = _default_provider_record(pid)
    except Exception:  # noqa: BLE001
        # Capability_Matrix not available yet — fall back to well-known list.
        for pid in _KNOWN_PROVIDERS:
            result[pid] = _default_provider_record(pid)

    return result


def _enrich_with_circuit_state(
    record: dict[str, Any],
    circuit_breakers: dict[str, Any],
    provider_id: str,
) -> dict[str, Any]:
    """Merge live circuit-breaker data into *record* (non-mutating copy)."""
    enriched = dict(record)

    # Flatten any nested per-capability circuit-breaker states into a single
    # representative value: prefer OPEN > HALF_OPEN > CLOSED.
    state_priority = {"OPEN": 2, "HALF_OPEN": 1, "CLOSED": 0}
    worst_state = "CLOSED"

    for key, state_val in circuit_breakers.items():
        # Keys can be "{provider}:{capability}" or bare "{provider}".
        if key.startswith(provider_id):
            state_str = str(state_val).upper() if isinstance(state_val, str) else "CLOSED"
            if state_str not in _CIRCUIT_STATES:
                state_str = "CLOSED"
            if state_priority.get(state_str, 0) > state_priority.get(worst_state, 0):
                worst_state = state_str

    enriched["circuitBreakerState"] = worst_state
    if worst_state in ("OPEN", "HALF_OPEN"):
        enriched["providerHealthy"] = False

    return enriched


# ---------------------------------------------------------------------------
# Internal helpers — quality scores
# ---------------------------------------------------------------------------


def _get_quality_scores(request: Request) -> dict[str, int]:
    """Resolve per-symbol quality scores from ``app.state.quality_scores``.

    Returns an empty dict when not yet populated.
    """
    scores: Any = getattr(request.app.state, "quality_scores", None)
    if isinstance(scores, dict):
        return {k: int(v) for k, v in scores.items()}
    return {}


def _aggregate_quality(scores: dict[str, int]) -> dict[str, Any]:
    """Compute aggregate quality distribution from a symbol → score mapping.

    Score bands (mirrors QualityEngine.grade_score logic):
      * BLOCKED  : score < 30
      * LOW      : 30 ≤ score < 50
      * MEDIUM   : 50 ≤ score < 80
      * HIGH     : 80 ≤ score ≤ 95
      * STALE    : represented as stalemaskCount — score == 0 with no value

    Args:
        scores: dict mapping symbol → DataConfidenceScore integer.

    Returns:
        Aggregate quality metrics dict.
    """
    if not scores:
        return {
            "averageScore": None,
            "blockedCount": 0,
            "lowCount": 0,
            "mediumCount": 0,
            "highCount": 0,
            "stalemaskCount": 0,
            "totalSymbols": 0,
        }

    blocked = low = medium = high = stale = 0
    total_score = 0

    for score in scores.values():
        total_score += score
        if score == 0:
            stale += 1
        elif score < 30:
            blocked += 1
        elif score < 50:
            low += 1
        elif score < 80:
            medium += 1
        else:
            high += 1

    n = len(scores)
    return {
        "averageScore": round(total_score / n, 2) if n else None,
        "blockedCount": blocked,
        "lowCount": low,
        "mediumCount": medium,
        "highCount": high,
        "stalemaskCount": stale,
        "totalSymbols": n,
    }


# ---------------------------------------------------------------------------
# Platform degradation check
# ---------------------------------------------------------------------------


def _is_platform_degraded(request: Request) -> tuple[bool, list[str]]:
    """Return ``(degraded, reasons)`` by inspecting ``app.state``.

    Checks:
    1. Redis unavailable  (``app.state.redis is None``)
    2. PostgreSQL unavailable  (``app.state.db_engine is None``)
    3. Any circuit breaker in OPEN state
    4. Clock skew flag  (``app.state.clock_degraded is True``)
    """
    reasons: list[str] = []

    if getattr(request.app.state, "redis", None) is None:
        reasons.append("redis_unavailable")

    if getattr(request.app.state, "db_engine", None) is None:
        reasons.append("postgres_unavailable")

    clock_degraded: bool = getattr(request.app.state, "clock_degraded", False)
    if clock_degraded:
        reasons.append("clock_skew_exceeded_500ms")

    # Check if any circuit breaker is OPEN.
    circuit_breakers: Any = getattr(request.app.state, "circuit_breakers", {})
    if isinstance(circuit_breakers, dict):
        for key, state in circuit_breakers.items():
            if str(state).upper() == "OPEN":
                reasons.append(f"circuit_breaker_open:{key}")
                break  # one is enough to signal degraded

    return bool(reasons), reasons


# ---------------------------------------------------------------------------
# GET /v1/analytics/providers
# ---------------------------------------------------------------------------


@router.get(
    "/analytics/providers",
    summary="List all provider health summaries",
    description=(
        "Returns a list of all registered providers with their health status, "
        "circuit-breaker state, and key metrics. "
        "Data is sourced from app.state.provider_registry (Requirements 16.1, 16.2)."
    ),
    tags=["Analytics"],
)
async def list_providers(request: Request) -> Response:
    """Return a summary health record for every registered provider.

    Response (canonical envelope)::

        {
          "data": [
            {
              "providerId": "angel_one",
              "providerHealthy": true,
              "circuitBreakerState": "CLOSED",
              "failureRate": 0.002,
              "lastSuccessAt": "2026-01-15T09:14:50.000Z",
              "lastFailureAt": null,
              "avgResponseTimeMs": 45,
              "totalRequests": 8000,
              "successfulRequests": 7984,
              "failedRequests": 16
            },
            ...
          ],
          "metadata": { ... }
        }
    """
    registry = _get_provider_registry(request)
    circuit_breakers: Any = getattr(request.app.state, "circuit_breakers", {})
    if not isinstance(circuit_breakers, dict):
        circuit_breakers = {}

    providers_list = [
        _enrich_with_circuit_state(record, circuit_breakers, pid)
        for pid, record in registry.items()
    ]

    await _log.adebug(
        "analytics_list_providers",
        provider_count=len(providers_list),
    )

    return _json_response(
        _success_envelope(providers_list, data_source_type="LIVE")
    )


# ---------------------------------------------------------------------------
# GET /v1/analytics/providers/{provider_id}
# ---------------------------------------------------------------------------


@router.get(
    "/analytics/providers/{provider_id}",
    summary="Detailed health metrics for a single provider",
    description=(
        "Returns detailed health metrics for the named provider: "
        "providerHealthy, circuitBreakerState (CLOSED/HALF_OPEN/OPEN), "
        "failureRate, lastSuccessAt, lastFailureAt, avgResponseTimeMs, "
        "totalRequests, successfulRequests, failedRequests "
        "(Requirements 16.1, 16.2)."
    ),
    tags=["Analytics"],
)
async def get_provider_detail(provider_id: str, request: Request) -> Response:
    """Return detailed health for a single *provider_id*.

    Returns HTTP 404 when the provider is not in the registry.

    Response (canonical envelope)::

        {
          "data": {
            "providerId": "angel_one",
            "providerHealthy": true,
            "circuitBreakerState": "CLOSED",
            ...
          },
          "metadata": { ... }
        }
    """
    registry = _get_provider_registry(request)

    # Normalise: accept both "angel_one" and "ANGEL_ONE".
    pid_lower = provider_id.lower()
    record = registry.get(pid_lower) or registry.get(provider_id)

    if record is None:
        req_id = _request_id()
        await _log.awarning(
            "analytics_provider_not_found",
            provider_id=provider_id,
            request_id=req_id,
        )
        return _json_response(
            _error_envelope(
                "PROVIDER_NOT_FOUND",
                f"provider '{provider_id}' is not registered",
                request_id=req_id,
                provider=provider_id,
            ),
            status_code=404,
        )

    circuit_breakers: Any = getattr(request.app.state, "circuit_breakers", {})
    if not isinstance(circuit_breakers, dict):
        circuit_breakers = {}

    enriched = _enrich_with_circuit_state(record, circuit_breakers, pid_lower)

    await _log.adebug(
        "analytics_get_provider_detail",
        provider_id=pid_lower,
        circuit_state=enriched.get("circuitBreakerState"),
    )

    return _json_response(
        _success_envelope(enriched, data_source_type="LIVE", provider=pid_lower)
    )


# ---------------------------------------------------------------------------
# GET /v1/analytics/quality
# ---------------------------------------------------------------------------


@router.get(
    "/analytics/quality",
    summary="Aggregate quality metrics across all active symbols",
    description=(
        "Returns aggregated DataConfidenceScore distribution across all "
        "active symbols: averageScore, blockedCount, lowCount, mediumCount, "
        "highCount, stalemaskCount, totalSymbols "
        "(Requirements 16.1, 16.2)."
    ),
    tags=["Analytics"],
)
async def get_quality_metrics(request: Request) -> Response:
    """Return aggregate quality metrics sourced from ``app.state.quality_scores``.

    When no quality scores are available yet (cold start), all counts are 0
    and ``averageScore`` is ``null``.

    Response (canonical envelope)::

        {
          "data": {
            "averageScore": 82.5,
            "blockedCount": 1,
            "lowCount": 3,
            "mediumCount": 12,
            "highCount": 45,
            "stalemaskCount": 0,
            "totalSymbols": 61
          },
          "metadata": { ... }
        }
    """
    scores = _get_quality_scores(request)
    metrics = _aggregate_quality(scores)

    await _log.adebug(
        "analytics_quality_metrics",
        total_symbols=metrics["totalSymbols"],
        average_score=metrics["averageScore"],
    )

    return _json_response(
        _success_envelope(metrics, data_source_type="LIVE")
    )


# ---------------------------------------------------------------------------
# GET /v1/analytics/health
# ---------------------------------------------------------------------------


@router.get(
    "/analytics/health",
    summary="Overall platform health — 200 healthy, 503 degraded",
    description=(
        "Returns HTTP 200 when all critical platform components are healthy "
        "and HTTP 503 when any are degraded (Requirements 16.1, 16.2). "
        "Checks: Redis connectivity, PostgreSQL connectivity, "
        "circuit-breaker states, clock-skew flag."
    ),
    tags=["Analytics"],
)
async def get_platform_health(request: Request) -> Response:
    """Overall platform health check.

    HTTP 200 (healthy)::

        {
          "data": {
            "healthy": true,
            "degradedComponents": []
          },
          "metadata": { ... }
        }

    HTTP 503 (degraded)::

        {
          "data": {
            "healthy": false,
            "degradedComponents": ["redis_unavailable", "circuit_breaker_open:angel_one:LIVE_QUOTE"]
          },
          "metadata": { ... }
        }
    """
    degraded, reasons = _is_platform_degraded(request)

    payload: dict[str, Any] = {
        "healthy": not degraded,
        "degradedComponents": reasons,
    }

    if degraded:
        await _log.awarning(
            "analytics_platform_degraded",
            degraded_components=reasons,
        )
        return _json_response(
            _success_envelope(payload, data_source_type="LIVE"),
            status_code=503,
        )

    return _json_response(
        _success_envelope(payload, data_source_type="LIVE"),
        status_code=200,
    )
