"""
Unit tests for analytics API endpoints (Task 14.1).

Tests cover:
    GET /v1/analytics/providers          — list all providers with health summaries
    GET /v1/analytics/providers/{id}     — single provider detail
    GET /v1/analytics/quality            — aggregate quality metrics
    GET /v1/analytics/health             — overall platform health check

Requirements: 16.1, 16.2

Tests use an in-process ASGI test client (``httpx.AsyncClient`` via
``ASGITransport``) so no live Redis or PostgreSQL is required.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.analytics import router as analytics_router


# ---------------------------------------------------------------------------
# Test application factory
# ---------------------------------------------------------------------------


def _make_app(
    *,
    redis: Any = object(),
    db_engine: Any = object(),
    provider_registry: dict[str, dict[str, Any]] | None = None,
    quality_scores: dict[str, int] | None = None,
    circuit_breakers: dict[str, str] | None = None,
    clock_degraded: bool = False,
) -> FastAPI:
    """Build a minimal FastAPI app with only the analytics router."""
    app = FastAPI()
    app.include_router(analytics_router, prefix="/v1")

    app.state.redis = redis
    app.state.db_engine = db_engine
    app.state.clock_degraded = clock_degraded
    app.state.circuit_breakers = circuit_breakers or {}

    if provider_registry is not None:
        app.state.provider_registry = provider_registry

    if quality_scores is not None:
        app.state.quality_scores = quality_scores

    return app


async def _get(app: FastAPI, path: str, **params: Any) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


# ---------------------------------------------------------------------------
# Shared fixture: a registry with two explicit providers
# ---------------------------------------------------------------------------

_REGISTRY_TWO: dict[str, dict[str, Any]] = {
    "angel_one": {
        "providerId": "angel_one",
        "providerHealthy": True,
        "circuitBreakerState": "CLOSED",
        "failureRate": 0.001,
        "lastSuccessAt": "2026-01-15T09:14:50.000Z",
        "lastFailureAt": None,
        "avgResponseTimeMs": 45,
        "totalRequests": 8000,
        "successfulRequests": 7992,
        "failedRequests": 8,
    },
    "upstox": {
        "providerId": "upstox",
        "providerHealthy": True,
        "circuitBreakerState": "CLOSED",
        "failureRate": 0.002,
        "lastSuccessAt": "2026-01-15T09:14:55.000Z",
        "lastFailureAt": None,
        "avgResponseTimeMs": 60,
        "totalRequests": 5000,
        "successfulRequests": 4990,
        "failedRequests": 10,
    },
}


# ===========================================================================
# GET /v1/analytics/providers
# ===========================================================================


class TestListProviders:
    """Tests for GET /v1/analytics/providers."""

    @pytest.mark.asyncio
    async def test_returns_200(self) -> None:
        """Endpoint must return HTTP 200."""
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_canonical_envelope_shape(self) -> None:
        """Response must be wrapped in the canonical success envelope."""
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers")
        body = resp.json()

        assert "data" in body
        assert "metadata" in body
        meta = body["metadata"]
        assert "requestedAt" in meta
        assert "dataAsOf" in meta
        assert meta["dataSourceType"] == "LIVE"
        assert meta["requestedAt"].endswith("Z")

    @pytest.mark.asyncio
    async def test_data_is_list(self) -> None:
        """data field must be a list."""
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers")
        body = resp.json()
        assert isinstance(body["data"], list)

    @pytest.mark.asyncio
    async def test_list_contains_registered_providers(self) -> None:
        """Each registered provider must appear in the list."""
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers")
        provider_ids = {item["providerId"] for item in resp.json()["data"]}
        assert "angel_one" in provider_ids
        assert "upstox" in provider_ids

    @pytest.mark.asyncio
    async def test_each_record_has_required_fields(self) -> None:
        """Every provider record must contain the required health fields."""
        required = {
            "providerId",
            "providerHealthy",
            "circuitBreakerState",
            "failureRate",
            "lastSuccessAt",
            "lastFailureAt",
            "avgResponseTimeMs",
            "totalRequests",
            "successfulRequests",
            "failedRequests",
        }
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers")
        for item in resp.json()["data"]:
            missing = required - set(item.keys())
            assert not missing, f"Record missing fields: {missing}"

    @pytest.mark.asyncio
    async def test_circuit_breaker_state_applied(self) -> None:
        """Open circuit for a provider should be reflected in the list."""
        app = _make_app(
            provider_registry=_REGISTRY_TWO,
            circuit_breakers={"angel_one:LIVE_QUOTE": "OPEN"},
        )
        resp = await _get(app, "/v1/analytics/providers")
        ao_records = [
            item for item in resp.json()["data"]
            if item["providerId"] == "angel_one"
        ]
        assert ao_records, "angel_one should appear in the list"
        assert ao_records[0]["circuitBreakerState"] == "OPEN"
        assert ao_records[0]["providerHealthy"] is False

    @pytest.mark.asyncio
    async def test_returns_200_with_empty_registry(self) -> None:
        """Even with an empty registry, endpoint must return 200 with a list."""
        app = _make_app(provider_registry={})
        resp = await _get(app, "/v1/analytics/providers")
        # Falls back to Capability_Matrix or well-known providers.
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"], list)

    @pytest.mark.asyncio
    async def test_falls_back_when_registry_absent(self) -> None:
        """When provider_registry not set, endpoint falls back gracefully."""
        app = _make_app()  # no provider_registry on state
        resp = await _get(app, "/v1/analytics/providers")
        assert resp.status_code == 200
        assert isinstance(resp.json()["data"], list)
        # At least a few providers should appear.
        assert len(resp.json()["data"]) > 0


# ===========================================================================
# GET /v1/analytics/providers/{provider_id}
# ===========================================================================


class TestGetProviderDetail:
    """Tests for GET /v1/analytics/providers/{provider_id}."""

    @pytest.mark.asyncio
    async def test_returns_200_for_existing_provider(self) -> None:
        """Returns 200 for a known provider_id."""
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers/angel_one")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_canonical_envelope_shape(self) -> None:
        """Response must be wrapped in the canonical success envelope."""
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers/angel_one")
        body = resp.json()

        assert "data" in body
        assert "metadata" in body
        assert body["metadata"]["requestedAt"].endswith("Z")
        assert body["metadata"]["dataSourceType"] == "LIVE"
        assert body["metadata"]["provider"] == "angel_one"

    @pytest.mark.asyncio
    async def test_data_contains_provider_id(self) -> None:
        """data.providerId should match the path parameter."""
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers/angel_one")
        assert resp.json()["data"]["providerId"] == "angel_one"

    @pytest.mark.asyncio
    async def test_all_detail_fields_present(self) -> None:
        """All required detail fields must be present in data."""
        required = {
            "providerId",
            "providerHealthy",
            "circuitBreakerState",
            "failureRate",
            "lastSuccessAt",
            "lastFailureAt",
            "avgResponseTimeMs",
            "totalRequests",
            "successfulRequests",
            "failedRequests",
        }
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers/angel_one")
        missing = required - set(resp.json()["data"].keys())
        assert not missing, f"Missing fields: {missing}"

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_provider(self) -> None:
        """Returns 404 for a provider_id that is not registered."""
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers/nonexistent_provider_xyz")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_404_error_uses_canonical_envelope(self) -> None:
        """404 response must use the canonical error envelope."""
        app = _make_app(provider_registry=_REGISTRY_TWO)
        resp = await _get(app, "/v1/analytics/providers/nonexistent_xyz")
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "PROVIDER_NOT_FOUND"
        assert "requestId" in body["error"]

    @pytest.mark.asyncio
    async def test_circuit_breaker_enriched_in_detail(self) -> None:
        """Detailed view must reflect live circuit-breaker state."""
        app = _make_app(
            provider_registry=_REGISTRY_TWO,
            circuit_breakers={"upstox:HISTORICAL_OHLCV": "HALF_OPEN"},
        )
        resp = await _get(app, "/v1/analytics/providers/upstox")
        data = resp.json()["data"]
        assert data["circuitBreakerState"] == "HALF_OPEN"
        assert data["providerHealthy"] is False

    @pytest.mark.asyncio
    async def test_healthy_provider_has_healthy_true(self) -> None:
        """A provider with CLOSED circuit should have providerHealthy: true."""
        app = _make_app(
            provider_registry=_REGISTRY_TWO,
            circuit_breakers={},  # all CLOSED
        )
        resp = await _get(app, "/v1/analytics/providers/angel_one")
        assert resp.json()["data"]["providerHealthy"] is True
        assert resp.json()["data"]["circuitBreakerState"] == "CLOSED"

    @pytest.mark.asyncio
    async def test_open_circuit_makes_provider_unhealthy(self) -> None:
        """OPEN circuit breaker must set providerHealthy: false."""
        app = _make_app(
            provider_registry=_REGISTRY_TWO,
            circuit_breakers={"angel_one:LIVE_QUOTE": "OPEN"},
        )
        resp = await _get(app, "/v1/analytics/providers/angel_one")
        data = resp.json()["data"]
        assert data["circuitBreakerState"] == "OPEN"
        assert data["providerHealthy"] is False


# ===========================================================================
# GET /v1/analytics/quality
# ===========================================================================


class TestGetQualityMetrics:
    """Tests for GET /v1/analytics/quality."""

    @pytest.mark.asyncio
    async def test_returns_200(self) -> None:
        """Returns 200 with quality scores set."""
        app = _make_app(quality_scores={"NIFTY": 90, "RELIANCE": 55, "BANKNIFTY": 25})
        resp = await _get(app, "/v1/analytics/quality")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_canonical_envelope_shape(self) -> None:
        app = _make_app(quality_scores={"NIFTY": 90})
        resp = await _get(app, "/v1/analytics/quality")
        body = resp.json()
        assert "data" in body
        assert "metadata" in body
        assert body["metadata"]["dataSourceType"] == "LIVE"

    @pytest.mark.asyncio
    async def test_aggregate_fields_present(self) -> None:
        """data must contain all required aggregate fields."""
        required = {
            "averageScore",
            "blockedCount",
            "lowCount",
            "mediumCount",
            "highCount",
            "stalemaskCount",
            "totalSymbols",
        }
        app = _make_app(quality_scores={"NIFTY": 90})
        resp = await _get(app, "/v1/analytics/quality")
        missing = required - set(resp.json()["data"].keys())
        assert not missing, f"Missing fields: {missing}"

    @pytest.mark.asyncio
    async def test_score_distribution_counts(self) -> None:
        """Band counts must correctly categorise scores."""
        # score 90 → HIGH; 55 → MEDIUM; 25 → BLOCKED; 40 → LOW; 0 → stale
        app = _make_app(
            quality_scores={
                "SYM_HIGH": 90,
                "SYM_MED": 55,
                "SYM_BLOCKED": 25,
                "SYM_LOW": 40,
                "SYM_STALE": 0,
            }
        )
        resp = await _get(app, "/v1/analytics/quality")
        data = resp.json()["data"]
        assert data["highCount"] == 1
        assert data["mediumCount"] == 1
        assert data["blockedCount"] == 1
        assert data["lowCount"] == 1
        assert data["stalemaskCount"] == 1
        assert data["totalSymbols"] == 5

    @pytest.mark.asyncio
    async def test_average_score_computed_correctly(self) -> None:
        """averageScore should be the mean of all scores."""
        app = _make_app(quality_scores={"A": 80, "B": 40})  # mean = 60
        resp = await _get(app, "/v1/analytics/quality")
        data = resp.json()["data"]
        assert data["averageScore"] == pytest.approx(60.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_no_scores_returns_nulls_and_zeros(self) -> None:
        """With no quality scores, averageScore should be null and counts 0."""
        app = _make_app(quality_scores={})
        resp = await _get(app, "/v1/analytics/quality")
        data = resp.json()["data"]
        assert data["averageScore"] is None
        assert data["totalSymbols"] == 0
        assert data["highCount"] == 0
        assert data["blockedCount"] == 0

    @pytest.mark.asyncio
    async def test_returns_200_when_quality_scores_absent(self) -> None:
        """When quality_scores is not set on state, endpoint returns 200 safely."""
        app = _make_app()  # no quality_scores
        resp = await _get(app, "/v1/analytics/quality")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["totalSymbols"] == 0
        assert data["averageScore"] is None

    @pytest.mark.asyncio
    async def test_all_high_scores(self) -> None:
        """All symbols above 80 should be counted as high, none in other bands."""
        app = _make_app(quality_scores={"A": 81, "B": 90, "C": 95})
        resp = await _get(app, "/v1/analytics/quality")
        data = resp.json()["data"]
        assert data["highCount"] == 3
        assert data["mediumCount"] == 0
        assert data["blockedCount"] == 0
        assert data["lowCount"] == 0

    @pytest.mark.asyncio
    async def test_score_95_is_high(self) -> None:
        """Score of exactly 95 (max allowed) should count as HIGH."""
        app = _make_app(quality_scores={"TOP": 95})
        resp = await _get(app, "/v1/analytics/quality")
        assert resp.json()["data"]["highCount"] == 1


# ===========================================================================
# GET /v1/analytics/health
# ===========================================================================


class TestGetPlatformHealth:
    """Tests for GET /v1/analytics/health."""

    @pytest.mark.asyncio
    async def test_returns_200_when_healthy(self) -> None:
        """Returns HTTP 200 when all checks pass."""
        app = _make_app(
            redis=object(),
            db_engine=object(),
            circuit_breakers={},
            clock_degraded=False,
        )
        resp = await _get(app, "/v1/analytics/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_canonical_envelope_on_healthy(self) -> None:
        """Healthy response must use canonical success envelope."""
        app = _make_app()
        resp = await _get(app, "/v1/analytics/health")
        body = resp.json()
        assert "data" in body
        assert "metadata" in body
        assert body["data"]["healthy"] is True
        assert body["data"]["degradedComponents"] == []

    @pytest.mark.asyncio
    async def test_returns_503_when_redis_unavailable(self) -> None:
        """Returns HTTP 503 when Redis is None."""
        app = _make_app(redis=None)
        resp = await _get(app, "/v1/analytics/health")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_503_body_identifies_redis(self) -> None:
        """503 response must identify 'redis_unavailable' in degradedComponents."""
        app = _make_app(redis=None, db_engine=object())
        resp = await _get(app, "/v1/analytics/health")
        body = resp.json()
        assert body["data"]["healthy"] is False
        assert "redis_unavailable" in body["data"]["degradedComponents"]

    @pytest.mark.asyncio
    async def test_returns_503_when_postgres_unavailable(self) -> None:
        """Returns HTTP 503 when db_engine is None."""
        app = _make_app(db_engine=None)
        resp = await _get(app, "/v1/analytics/health")
        assert resp.status_code == 503
        body = resp.json()
        assert "postgres_unavailable" in body["data"]["degradedComponents"]

    @pytest.mark.asyncio
    async def test_returns_503_when_clock_degraded(self) -> None:
        """Returns HTTP 503 when clock skew flag is set."""
        app = _make_app(clock_degraded=True)
        resp = await _get(app, "/v1/analytics/health")
        assert resp.status_code == 503
        body = resp.json()
        assert "clock_skew_exceeded_500ms" in body["data"]["degradedComponents"]

    @pytest.mark.asyncio
    async def test_returns_503_when_circuit_open(self) -> None:
        """Returns HTTP 503 when any circuit breaker is OPEN."""
        app = _make_app(
            circuit_breakers={"angel_one:LIVE_QUOTE": "OPEN"},
        )
        resp = await _get(app, "/v1/analytics/health")
        assert resp.status_code == 503
        body = resp.json()
        assert any(
            "circuit_breaker_open" in reason
            for reason in body["data"]["degradedComponents"]
        )

    @pytest.mark.asyncio
    async def test_multiple_degraded_components(self) -> None:
        """All failing checks appear in degradedComponents."""
        app = _make_app(
            redis=None,
            db_engine=None,
            clock_degraded=True,
        )
        resp = await _get(app, "/v1/analytics/health")
        assert resp.status_code == 503
        components = resp.json()["data"]["degradedComponents"]
        assert "redis_unavailable" in components
        assert "postgres_unavailable" in components
        assert "clock_skew_exceeded_500ms" in components

    @pytest.mark.asyncio
    async def test_closed_circuit_does_not_degrade(self) -> None:
        """CLOSED circuit breaker should not trigger 503."""
        app = _make_app(
            circuit_breakers={"angel_one:LIVE_QUOTE": "CLOSED"},
            redis=object(),
            db_engine=object(),
            clock_degraded=False,
        )
        resp = await _get(app, "/v1/analytics/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_503_uses_canonical_envelope(self) -> None:
        """503 response still uses the canonical success-style data envelope."""
        app = _make_app(redis=None)
        resp = await _get(app, "/v1/analytics/health")
        body = resp.json()
        # Even degraded responses carry data + metadata (not an error envelope).
        assert "data" in body
        assert "metadata" in body
        assert body["data"]["healthy"] is False

    @pytest.mark.asyncio
    async def test_healthy_flag_false_when_degraded(self) -> None:
        """data.healthy must be false when platform is degraded."""
        app = _make_app(redis=None)
        resp = await _get(app, "/v1/analytics/health")
        assert resp.json()["data"]["healthy"] is False
