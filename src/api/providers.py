"""
src/api/providers.py

Provider health endpoint for DATA-SERVICE 2.0.

``GET /v1/providers/health``

Returns per-provider × per-capability health records including:
  - Operational status  (UP / DOWN / DEGRADED / UNKNOWN)
  - Circuit-breaker state  (CLOSED / OPEN / HALF_OPEN)  — live from Redis
  - Availability, latency percentiles (P50/P99), error rate
  - Last success timestamp, last failure reason
  - Semantic integrity flag

Design decisions
----------------
* The ``ProviderGateway.get_provider_health()`` method generates the full
  set of capability entries from the Capability_Matrix as the base template.
* For each entry the handler attempts to read the live circuit-breaker state
  from Redis via a ``CircuitBreaker`` instance keyed on
  ``mds:cb:{provider}:{capability}``.  If Redis is unavailable or the key is
  absent, the entry keeps its default UNKNOWN / CLOSED values — consumers
  always get a complete, well-formed response (Requirement 5.11).
* All fields whose names contain "key", "token", "secret", "password", or
  "credential" (case-insensitive) are stripped before the response is sent,
  satisfying Requirements 16.8 and 19.4.  This is a defence-in-depth measure;
  the health data should not contain credentials in the first place.

Requirements: 5.11, 16.8, 19.4
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.providers.circuit_breaker import CircuitBreaker
from src.providers.gateway import ProviderGateway

router = APIRouter()
_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Credential field stripping
# ---------------------------------------------------------------------------

_CREDENTIAL_PATTERN = re.compile(
    r"key|token|secret|password|credential",
    re.IGNORECASE,
)


def _strip_credentials(obj: Any) -> Any:
    """Recursively remove fields whose names contain credential-like substrings.

    Handles nested dicts and lists.  Non-dict, non-list values are returned
    unchanged.

    Args:
        obj: Any JSON-serialisable value.

    Returns:
        The same structure with credential-named keys removed.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_credentials(v)
            for k, v in obj.items()
            if not _CREDENTIAL_PATTERN.search(k)
        }
    if isinstance(obj, list):
        return [_strip_credentials(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Circuit-breaker state enrichment
# ---------------------------------------------------------------------------

# Valid circuit state strings — used for validation when reading from Redis.
_VALID_CIRCUIT_STATES = frozenset({"CLOSED", "OPEN", "HALF_OPEN"})

# Valid status strings for provider health.
_VALID_STATUSES = frozenset({"UP", "DOWN", "DEGRADED", "UNKNOWN"})


async def _enrich_with_circuit_state(
    health_entry: dict[str, Any],
    redis_client: Any,
    failure_threshold: int,
    recovery_window_sec: int,
) -> dict[str, Any]:
    """Overwrite ``circuitState`` in *health_entry* with the live Redis value.

    Creates a ``CircuitBreaker`` for the provider × capability pair and calls
    ``get_state()``, which automatically advances OPEN → HALF_OPEN when the
    recovery window has elapsed.

    If the circuit breaker cannot be read (Redis unavailable, no state stored),
    the original UNKNOWN / CLOSED defaults are preserved.

    Args:
        health_entry:          The baseline health record dict (mutated in place).
        redis_client:          ``RedisClient`` instance from ``app.state.redis``,
                               or ``None`` when Redis is unavailable.
        failure_threshold:     Circuit breaker failure threshold (from settings).
        recovery_window_sec:   Circuit breaker recovery window (from settings).

    Returns:
        The (possibly enriched) health record dict.
    """
    provider = health_entry.get("provider", "")
    capability = health_entry.get("capability", "")

    if not provider or not capability:
        return health_entry

    try:
        cb = CircuitBreaker(
            provider=provider,
            capability=capability,
            redis_client=redis_client,
            failure_threshold=failure_threshold,
            recovery_window_sec=recovery_window_sec,
        )
        state = await cb.get_state()
        health_entry["circuitState"] = state.value

        # Derive a coarse status from circuit state when the current status
        # is still UNKNOWN (i.e. no live metrics system has set it yet).
        if health_entry.get("status") == "UNKNOWN":
            if state.value == "OPEN":
                health_entry["status"] = "DOWN"
            elif state.value == "HALF_OPEN":
                health_entry["status"] = "DEGRADED"
            # CLOSED → leave as UNKNOWN; live metrics will set it to UP/DOWN.

        # Read additional state fields stored by CircuitBreaker._write_state()
        # to surface lastFailureReason if available.
        if redis_client is not None:
            from src.providers.circuit_breaker import _cb_key  # noqa: PLC0415
            import json  # noqa: PLC0415

            try:
                raw = await redis_client.get(_cb_key(provider, capability))
                if raw:
                    stored = json.loads(raw)
                    last_reason = stored.get("last_failure_reason")
                    if last_reason and health_entry.get("lastFailureReason") is None:
                        health_entry["lastFailureReason"] = last_reason
            except Exception:  # noqa: BLE001
                pass  # Best-effort; do not degrade the response

    except Exception as exc:  # noqa: BLE001
        # Circuit breaker read failed — keep defaults, log a warning.
        await _log.awarning(
            "provider_health_circuit_read_failed",
            provider=provider,
            capability=capability,
            error=str(exc),
        )

    return health_entry


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/providers/health",
    summary="Provider health",
    description=(
        "Returns per-provider × per-capability health: status, circuit-breaker "
        "state, availability, latency percentiles, error rate, last success "
        "timestamp, last failure reason, and semantic integrity flag. "
        "Circuit-breaker state is read live from Redis when available; falls "
        "back to UNKNOWN/CLOSED when Redis is unavailable. "
        "Credential fields are always stripped from the response "
        "(Requirements 16.8, 19.4)."
    ),
    response_class=JSONResponse,
    tags=["Providers"],
)
async def get_provider_health(request: Request) -> JSONResponse:
    """Return live provider health for all configured providers.

    Response body::

        {
          "providers": {
            "angel_one:HISTORICAL_OHLCV": {
              "provider": "angel_one",
              "capability": "HISTORICAL_OHLCV",
              "status": "UP",
              "circuitState": "CLOSED",
              "availability": 1.0,
              "latencyP50Ms": 45,
              "latencyP99Ms": 180,
              "errorRate": 0.001,
              "lastSuccessAt": "2026-01-15T09:15:00.000Z",
              "lastFailureReason": null,
              "semanticIntegrity": true
            },
            ...
          }
        }

    Raises:
        (never raises — always returns 200 with a safe default structure)
    """
    # ── Resolve settings ─────────────────────────────────────────────────
    settings = getattr(request.app.state, "settings", None)
    failure_threshold: int = (
        int(settings.circuit_breaker_failure_threshold)
        if settings is not None
        else 5
    )
    recovery_window_sec: int = (
        int(settings.circuit_breaker_recovery_window_sec)
        if settings is not None
        else 60
    )

    # ── Redis client (may be None in degraded mode) ───────────────────────
    redis_client = getattr(request.app.state, "redis", None)

    # ── Baseline health data from the gateway (Capability_Matrix-derived) ─
    gateway = ProviderGateway()
    health_map: dict[str, dict[str, Any]] = gateway.get_provider_health()

    # ── Enrich each entry with live circuit-breaker state ────────────────
    import asyncio  # noqa: PLC0415

    enriched_entries = await asyncio.gather(
        *(
            _enrich_with_circuit_state(
                entry,
                redis_client,
                failure_threshold,
                recovery_window_sec,
            )
            for entry in health_map.values()
        )
    )

    # Rebuild keyed dict from enriched list (order preserved).
    enriched_map: dict[str, dict[str, Any]] = {
        f"{entry['provider']}:{entry['capability']}": entry
        for entry in enriched_entries
    }

    # ── Strip credential fields (defence-in-depth) ───────────────────────
    safe_map = _strip_credentials(enriched_map)

    return JSONResponse(
        status_code=200,
        content={"providers": safe_map},
    )
