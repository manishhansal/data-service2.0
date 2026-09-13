"""
Prometheus metrics definitions and ``GET /metrics`` endpoint.

Task 1.5 — Requirement 18.2, 20.3

All metrics defined here are module-level singletons.  Any component in the
codebase imports them directly, e.g.::

    from src.api.metrics import CACHE_HITS, REQUEST_DURATION

and calls ``.inc()``, ``.observe()``, ``.set()``, etc.

The ``/metrics`` endpoint streams the current scrape in the standard
Prometheus text exposition format and **must respond within 500 ms**
(Requirement 18.2).
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from fastapi import APIRouter
from fastapi.responses import Response


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------
# Label naming follows snake_case, consistent with Prometheus conventions.

# ── Request latency ─────────────────────────────────────────────────────────
REQUEST_DURATION: Histogram = Histogram(
    name="mds_request_duration_seconds",
    documentation=(
        "Request latency in seconds per endpoint (labels: endpoint, method, status). "
        "Tracks P50/P95/P99 per endpoint (Requirement 18.2)."
    ),
    labelnames=["endpoint", "method", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ── Provider call latency ────────────────────────────────────────────────────
PROVIDER_CALL_DURATION: Histogram = Histogram(
    name="mds_provider_call_duration_seconds",
    documentation=(
        "Provider call latency in seconds (labels: provider, capability). "
        "Tracks P50/P95/P99 per provider per capability (Requirement 18.2)."
    ),
    labelnames=["provider", "capability"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# ── Cache counters ───────────────────────────────────────────────────────────
CACHE_HITS: Counter = Counter(
    name="mds_cache_hits_total",
    documentation=(
        "Total cache hits per cache level (labels: level). "
        "Levels: l1, l2, l3 (Requirement 18.2)."
    ),
    labelnames=["level"],
)

CACHE_MISSES: Counter = Counter(
    name="mds_cache_misses_total",
    documentation=(
        "Total cache misses per cache level (labels: level). "
        "Levels: l1, l2, l3 (Requirement 18.2)."
    ),
    labelnames=["level"],
)

# ── Circuit breaker state gauge ──────────────────────────────────────────────
# 0 = CLOSED, 1 = OPEN, 2 = HALF_OPEN
CIRCUIT_BREAKER_STATE: Gauge = Gauge(
    name="mds_circuit_breaker_state",
    documentation=(
        "Circuit breaker state per provider and capability "
        "(labels: provider, capability). Values: 0=CLOSED, 1=OPEN, 2=HALF_OPEN "
        "(Requirement 18.2)."
    ),
    labelnames=["provider", "capability"],
)

# ── Streaming / tick metrics ─────────────────────────────────────────────────
TICK_PUBLISH_RATE: Gauge = Gauge(
    name="mds_tick_publish_rate",
    documentation=(
        "Current tick publication rate (ticks per second) across all symbols "
        "(Requirement 18.2)."
    ),
)

# ── Gap metrics ───────────────────────────────────────────────────────────────
GAP_COUNT: Gauge = Gauge(
    name="mds_gap_count",
    documentation=(
        "Number of open data gaps by severity (labels: severity). "
        "Severities: LOW, MEDIUM, HIGH (Requirement 18.2)."
    ),
    labelnames=["severity"],
)

# ── Quality score metrics ────────────────────────────────────────────────────
QUALITY_SCORE: Gauge = Gauge(
    name="mds_quality_score",
    documentation=(
        "Current DataConfidenceScore per instrument (labels: instrument_id). "
        "Range [0, 95] (Requirement 18.2)."
    ),
    labelnames=["instrument_id"],
)

QUALITY_SCORE_DISTRIBUTION: Histogram = Histogram(
    name="mds_quality_score_distribution",
    documentation=(
        "Distribution of DataConfidenceScore values across all instruments. "
        "Range [0, 95] (Requirement 18.2)."
    ),
    buckets=(0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95),
)

# ── Duplicate tick rate ──────────────────────────────────────────────────────
DUPLICATE_RATE: Gauge = Gauge(
    name="mds_duplicate_rate",
    documentation=(
        "Current duplicate tick rate (fraction 0.0–1.0) over a rolling window "
        "(Requirement 18.2)."
    ),
)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

_CIRCUIT_BREAKER_STATE_VALUES: dict[str, int] = {
    "CLOSED": 0,
    "OPEN": 1,
    "HALF_OPEN": 2,
}


def set_circuit_breaker_state(provider: str, capability: str, state: str) -> None:
    """Update the ``mds_circuit_breaker_state`` gauge.

    Args:
        provider:   Provider identifier, e.g. ``"angel_one"``.
        capability: Capability string, e.g. ``"historical_ohlcv"``.
        state:      One of ``"CLOSED"``, ``"OPEN"``, ``"HALF_OPEN"``.
    """
    numeric = _CIRCUIT_BREAKER_STATE_VALUES.get(state.upper(), 0)
    CIRCUIT_BREAKER_STATE.labels(provider=provider, capability=capability).set(numeric)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get(
    "/metrics",
    summary="Prometheus metrics scrape",
    description=(
        "Exposes all platform metrics in the standard Prometheus text format. "
        "Endpoint MUST respond within 500 ms (Requirement 18.2)."
    ),
    include_in_schema=True,
    # Health/metrics endpoints are not under /v1/ — registered at root prefix.
    response_class=Response,
)
async def get_metrics() -> Response:
    """Return all registered Prometheus metrics in text exposition format.

    This endpoint is intentionally synchronous-light: ``generate_latest()``
    is a pure in-process operation and returns in well under 500 ms for the
    metric cardinality in this service.
    """
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
