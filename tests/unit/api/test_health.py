"""
Unit tests for health and metrics endpoints (Task 1.5).

Requirements: 16.5, 16.6, 16.7, 18.2, 20.3, 20.4

Tests use an in-process ASGI test client (``httpx.AsyncClient`` via
``ASGITransport``) so no live Redis or PostgreSQL is required.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.health import router as health_router
from src.api.metrics import router as metrics_router


# ---------------------------------------------------------------------------
# Test application factory
# ---------------------------------------------------------------------------


def _make_app(
    redis_alive: bool = True,
    pg_alive: bool = True,
    session_phase: str = "REGULAR",
    clock_degraded: bool = False,
    duplicate_rate: float = 0.001,
    gap_counts: dict[str, int] | None = None,
    freshness_stats: dict[str, Any] | None = None,
    circuit_breakers: dict[str, str] | None = None,
    start_time_offset_ms: float = 0.0,
) -> FastAPI:
    """Build a minimal FastAPI app pre-loaded with test state."""
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(metrics_router)

    # App state consumed by health endpoint helpers.
    app.state.start_time_ms = (time.monotonic() * 1000) - start_time_offset_ms
    app.state.session_phase = session_phase
    app.state.clock_degraded = clock_degraded
    app.state.duplicate_rate = duplicate_rate
    app.state.gap_counts = gap_counts or {"LOW": 1, "MEDIUM": 0, "HIGH": 0}
    app.state.freshness_stats = freshness_stats or {
        "p50_ms": 80,
        "p99_ms": 450,
        "success_rate": 0.998,
    }
    app.state.circuit_breakers = circuit_breakers or {}

    # Stub redis and db_engine on state; actual pings are patched per test.
    app.state.redis = object() if redis_alive else None
    app.state.db_engine = object() if pg_alive else None

    return app


async def _get(app: FastAPI, path: str) -> Any:
    """Make a GET request via the ASGI transport and return the response."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ---------------------------------------------------------------------------
# /v1/health/live
# ---------------------------------------------------------------------------


class TestHealthLive:
    """Tests for GET /v1/health/live (Requirement 16.5, 20.3)."""

    @pytest.mark.asyncio
    async def test_always_returns_200(self) -> None:
        """Liveness probe must always return HTTP 200."""
        app = _make_app()
        response = await _get(app, "/v1/health/live")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_response_body_fields(self) -> None:
        """Response must include status, version, uptimeMs, timestamp."""
        app = _make_app(start_time_offset_ms=5000.0)
        response = await _get(app, "/v1/health/live")
        data = response.json()

        assert data["status"] == "alive"
        assert data["version"] == "2.0.0"
        assert isinstance(data["uptimeMs"], int)
        assert data["uptimeMs"] >= 0
        # Timestamp must be UTC ISO-8601 with Z suffix.
        assert data["timestamp"].endswith("Z")

    @pytest.mark.asyncio
    async def test_uptime_increases_over_time(self) -> None:
        """uptimeMs should reflect elapsed time since start."""
        app = _make_app(start_time_offset_ms=10_000.0)  # 10 seconds ago
        response = await _get(app, "/v1/health/live")
        data = response.json()
        # At least 9 500 ms (allow for minor drift in test execution)
        assert data["uptimeMs"] >= 9_500

    @pytest.mark.asyncio
    async def test_does_not_call_redis(self) -> None:
        """Liveness probe must not attempt to contact Redis."""
        # Even when redis is None (unavailable), live should return 200.
        app = _make_app()
        app.state.redis = None
        app.state.db_engine = None
        response = await _get(app, "/v1/health/live")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# /v1/health/ready
# ---------------------------------------------------------------------------


class TestHealthReady:
    """Tests for GET /v1/health/ready (Requirement 16.6, 20.4)."""

    @pytest.mark.asyncio
    async def test_200_when_both_dependencies_up(self) -> None:
        """HTTP 200 when Redis and PostgreSQL both respond."""
        app = _make_app(redis_alive=True, pg_alive=True)
        with (
            patch("src.api.health._ping_redis", new=AsyncMock(return_value=True)),
            patch("src.api.health._ping_postgres", new=AsyncMock(return_value=True)),
        ):
            response = await _get(app, "/v1/health/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["capabilities"]["redis"] is True
        assert data["capabilities"]["postgres"] is True

    @pytest.mark.asyncio
    async def test_503_when_redis_down(self) -> None:
        """HTTP 503 when Redis is unreachable."""
        app = _make_app()
        with (
            patch("src.api.health._ping_redis", new=AsyncMock(return_value=False)),
            patch("src.api.health._ping_postgres", new=AsyncMock(return_value=True)),
        ):
            response = await _get(app, "/v1/health/ready")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["capabilities"]["redis"] is False
        assert data["capabilities"]["postgres"] is True

    @pytest.mark.asyncio
    async def test_503_when_postgres_down(self) -> None:
        """HTTP 503 when PostgreSQL is unreachable."""
        app = _make_app()
        with (
            patch("src.api.health._ping_redis", new=AsyncMock(return_value=True)),
            patch("src.api.health._ping_postgres", new=AsyncMock(return_value=False)),
        ):
            response = await _get(app, "/v1/health/ready")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["capabilities"]["redis"] is True
        assert data["capabilities"]["postgres"] is False

    @pytest.mark.asyncio
    async def test_503_when_both_dependencies_down(self) -> None:
        """HTTP 503 when both Redis and PostgreSQL are unreachable."""
        app = _make_app()
        with (
            patch("src.api.health._ping_redis", new=AsyncMock(return_value=False)),
            patch("src.api.health._ping_postgres", new=AsyncMock(return_value=False)),
        ):
            response = await _get(app, "/v1/health/ready")

        assert response.status_code == 503
        data = response.json()
        assert data["capabilities"]["redis"] is False
        assert data["capabilities"]["postgres"] is False

    @pytest.mark.asyncio
    async def test_capabilities_map_always_present(self) -> None:
        """``capabilities`` key must be present in every response."""
        app = _make_app()
        with (
            patch("src.api.health._ping_redis", new=AsyncMock(return_value=True)),
            patch("src.api.health._ping_postgres", new=AsyncMock(return_value=True)),
        ):
            response = await _get(app, "/v1/health/ready")

        data = response.json()
        assert "capabilities" in data
        assert "redis" in data["capabilities"]
        assert "postgres" in data["capabilities"]

    @pytest.mark.asyncio
    async def test_timestamp_field_present(self) -> None:
        """timestamp must be included in ready response."""
        app = _make_app()
        with (
            patch("src.api.health._ping_redis", new=AsyncMock(return_value=True)),
            patch("src.api.health._ping_postgres", new=AsyncMock(return_value=True)),
        ):
            response = await _get(app, "/v1/health/ready")

        data = response.json()
        assert "timestamp" in data
        assert data["timestamp"].endswith("Z")


# ---------------------------------------------------------------------------
# /v1/health/data
# ---------------------------------------------------------------------------


class TestHealthData:
    """Tests for GET /v1/health/data (Requirement 16.7)."""

    @pytest.mark.asyncio
    async def test_always_returns_200(self) -> None:
        """Data health endpoint must always return HTTP 200."""
        app = _make_app()
        response = await _get(app, "/v1/health/data")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_session_phase_included(self) -> None:
        """Response must include marketSession.sessionPhase."""
        app = _make_app(session_phase="REGULAR")
        response = await _get(app, "/v1/health/data")
        data = response.json()
        assert data["marketSession"]["sessionPhase"] == "REGULAR"

    @pytest.mark.asyncio
    async def test_clock_degraded_false_by_default(self) -> None:
        """clockDegraded should be False when NTP is healthy."""
        app = _make_app(clock_degraded=False)
        response = await _get(app, "/v1/health/data")
        data = response.json()
        assert data["clockDegraded"] is False

    @pytest.mark.asyncio
    async def test_clock_degraded_true_when_set(self) -> None:
        """clockDegraded must reflect app.state.clock_degraded = True."""
        app = _make_app(clock_degraded=True)
        response = await _get(app, "/v1/health/data")
        data = response.json()
        assert data["clockDegraded"] is True

    @pytest.mark.asyncio
    async def test_freshness_stats_present(self) -> None:
        """Response must include freshness stats with p50Ms, p99Ms, successRate."""
        app = _make_app(
            freshness_stats={"p50_ms": 75, "p99_ms": 390, "success_rate": 0.995}
        )
        response = await _get(app, "/v1/health/data")
        data = response.json()
        assert data["freshness"]["p50Ms"] == 75
        assert data["freshness"]["p99Ms"] == 390
        assert data["freshness"]["successRate"] == 0.995

    @pytest.mark.asyncio
    async def test_gap_counts_present(self) -> None:
        """Response must include gap counts by severity."""
        app = _make_app(gap_counts={"LOW": 3, "MEDIUM": 1, "HIGH": 0})
        response = await _get(app, "/v1/health/data")
        data = response.json()
        assert data["gaps"]["low"] == 3
        assert data["gaps"]["medium"] == 1
        assert data["gaps"]["high"] == 0

    @pytest.mark.asyncio
    async def test_duplicate_rate_present(self) -> None:
        """Response must include duplicateRate."""
        app = _make_app(duplicate_rate=0.002)
        response = await _get(app, "/v1/health/data")
        data = response.json()
        assert data["duplicateRate"] == pytest.approx(0.002)

    @pytest.mark.asyncio
    async def test_circuit_breakers_present(self) -> None:
        """Response must include circuitBreakers map."""
        breakers = {"angel_one:historical_ohlcv": "CLOSED", "upstox:live_quote": "OPEN"}
        app = _make_app(circuit_breakers=breakers)
        response = await _get(app, "/v1/health/data")
        data = response.json()
        assert data["circuitBreakers"] == breakers

    @pytest.mark.asyncio
    async def test_empty_state_returns_safe_defaults(self) -> None:
        """When app.state has nothing set, data endpoint must still return 200
        with safe default values."""
        app = FastAPI()
        app.include_router(health_router)
        # Deliberately set no state at all.
        response = await _get(app, "/v1/health/data")
        assert response.status_code == 200
        data = response.json()
        # Should have zeroed-out / default values, not errors.
        assert data["clockDegraded"] is False
        assert data["duplicateRate"] == 0.0
        assert data["gaps"]["low"] == 0
        assert data["freshness"]["successRate"] == 1.0

    @pytest.mark.asyncio
    async def test_all_required_top_level_keys(self) -> None:
        """All required top-level keys must be present in the data response."""
        app = _make_app()
        response = await _get(app, "/v1/health/data")
        data = response.json()
        required_keys = {
            "status",
            "version",
            "timestamp",
            "uptimeMs",
            "marketSession",
            "freshness",
            "gaps",
            "duplicateRate",
            "circuitBreakers",
            "clockDegraded",
        }
        missing = required_keys - set(data.keys())
        assert not missing, f"Missing keys: {missing}"


# ---------------------------------------------------------------------------
# GET /metrics
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """Tests for GET /metrics (Requirement 18.2)."""

    @pytest.mark.asyncio
    async def test_returns_200(self) -> None:
        """Metrics endpoint must return HTTP 200."""
        app = FastAPI()
        app.include_router(metrics_router)
        response = await _get(app, "/metrics")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_content_type_is_prometheus_text(self) -> None:
        """Content-Type must be the standard Prometheus text exposition type."""
        app = FastAPI()
        app.include_router(metrics_router)
        response = await _get(app, "/metrics")
        assert "text/plain" in response.headers["content-type"]

    @pytest.mark.asyncio
    async def test_response_contains_all_metric_names(self) -> None:
        """All 10 registered metric names must appear in the scrape output."""
        app = FastAPI()
        app.include_router(metrics_router)
        response = await _get(app, "/metrics")
        body = response.text

        expected_metrics = [
            "mds_request_duration_seconds",
            "mds_provider_call_duration_seconds",
            "mds_cache_hits_total",
            "mds_cache_misses_total",
            "mds_circuit_breaker_state",
            "mds_tick_publish_rate",
            "mds_gap_count",
            "mds_quality_score",
            "mds_quality_score_distribution",
            "mds_duplicate_rate",
        ]
        for metric_name in expected_metrics:
            assert metric_name in body, f"Metric '{metric_name}' missing from /metrics output"

    @pytest.mark.asyncio
    async def test_response_is_non_empty(self) -> None:
        """Metrics response body must not be empty."""
        app = FastAPI()
        app.include_router(metrics_router)
        response = await _get(app, "/metrics")
        assert len(response.text) > 0


# ---------------------------------------------------------------------------
# Metrics helper: set_circuit_breaker_state
# ---------------------------------------------------------------------------


class TestSetCircuitBreakerState:
    """Tests for the ``set_circuit_breaker_state`` helper."""

    def test_closed_maps_to_zero(self) -> None:
        from src.api.metrics import CIRCUIT_BREAKER_STATE, set_circuit_breaker_state

        set_circuit_breaker_state("test_provider", "test_cap", "CLOSED")
        val = CIRCUIT_BREAKER_STATE.labels(
            provider="test_provider", capability="test_cap"
        )._value.get()
        assert val == 0.0

    def test_open_maps_to_one(self) -> None:
        from src.api.metrics import CIRCUIT_BREAKER_STATE, set_circuit_breaker_state

        set_circuit_breaker_state("test_provider2", "test_cap2", "OPEN")
        val = CIRCUIT_BREAKER_STATE.labels(
            provider="test_provider2", capability="test_cap2"
        )._value.get()
        assert val == 1.0

    def test_half_open_maps_to_two(self) -> None:
        from src.api.metrics import CIRCUIT_BREAKER_STATE, set_circuit_breaker_state

        set_circuit_breaker_state("test_provider3", "test_cap3", "HALF_OPEN")
        val = CIRCUIT_BREAKER_STATE.labels(
            provider="test_provider3", capability="test_cap3"
        )._value.get()
        assert val == 2.0

    def test_unknown_state_defaults_to_zero(self) -> None:
        from src.api.metrics import CIRCUIT_BREAKER_STATE, set_circuit_breaker_state

        set_circuit_breaker_state("test_provider4", "test_cap4", "UNKNOWN_STATE")
        val = CIRCUIT_BREAKER_STATE.labels(
            provider="test_provider4", capability="test_cap4"
        )._value.get()
        assert val == 0.0
