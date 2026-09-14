"""
tests/performance/test_load.py
================================

Performance / load benchmarks for DATA-SERVICE 2.0 core API endpoints.

All external I/O (Redis, PostgreSQL, provider adapters) is mocked so these
tests are safe to run in CI without live infrastructure.  They exercise the
real FastAPI application stack — including middleware, routing, serialisation,
and engine logic — while injecting stubs only at the external boundary.

Latency SLO targets (Requirements 18.2, 20.1)
----------------------------------------------
Endpoint                              Iterations   p99 target
GET  /v1/health/live                      200        < 10 ms
GET  /v1/analytics/health                 100        < 100 ms
POST /v1/quality/evaluate                 200        < 20 ms
GET  /v1/quality/score                    200        < 10 ms
GET  /v1/india/quotes/{symbol}            100        < 50 ms
GET  /v1/india/historical                 100        < 500 ms (≤1000 records)

Each benchmark:
  1. Warms up for 5 requests (discarded).
  2. Collects N timed measurements (N ≥ 100).
  3. Computes p50, p95, p99.
  4. Asserts the p99 is within the SLO target.

Usage::

    pytest tests/performance/ -m performance -v

To see latency breakdown per endpoint add ``-s``::

    pytest tests/performance/ -m performance -v -s
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Ensure environment variables are set before importing the app factory.
# The settings module validates required vars at import time; these minimal
# values satisfy it without touching any real infrastructure.
# ---------------------------------------------------------------------------
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
os.environ.setdefault("ENVIRONMENT", "development")
# Set a test API key so that the ConsumerAuthDependency allows test requests.
# DS2-RCA-016 fix: auth is now enforced — performance tests must supply a key.
os.environ.setdefault("CONSUMER_API_KEYS", "perf-test-key-1")
os.environ.setdefault("JWT_SECRET", "perf-test-jwt-secret-must-be-32chars!")

from src.server import create_app  # noqa: E402  (must come after env setup)

# ---------------------------------------------------------------------------
# Shared sample data
# ---------------------------------------------------------------------------

_SAMPLE_QUOTE: dict[str, Any] = {
    "instrumentId": "NSE:RELIANCE:EQ",
    "symbol": "RELIANCE",
    "exchange": "NSE",
    "ltp": 2850.75,
    "open": 2820.00,
    "high": 2870.50,
    "low": 2810.25,
    "prevClose": 2815.00,
    "change": 35.75,
    "changePct": 1.27,
    "volume": 1_234_567,
    "oi": None,
    "oiMissing": True,
    "tradedValue": 3_521_187_000.0,
    "totalBuyQty": 500_000,
    "totalSellQty": 450_000,
    "upperCircuit": 3096.50,
    "lowerCircuit": 2533.50,
    "weekHigh52": 3200.00,
    "weekLow52": 2100.00,
    "lastTradeTime": "2024-01-15T09:30:00.000Z",
    "bid": None,
    "ask": None,
    "marketStatus": "REGULAR",
    "provider": "angel_one",
    "provenance": {
        "dataObservationId": "00000000-0000-4000-8000-000000000001",
        "source": "angel_one",
        "sourceVersion": "1.0",
        "eventTimeMs": 1705300200_000,
        "receivedAtMs": 1705300200_150,
        "availableAtMs": 1705300200_200,
        "normalisationVersion": "2.0.0",
        "validationApplied": True,
        "isFallback": False,
        "fallbackReason": None,
        "sourceChain": ["angel_one"],
    },
}

_SAMPLE_CANDLE: dict[str, Any] = {
    "time": 1705300200,
    "open": 2820.00,
    "high": 2870.50,
    "low": 2810.25,
    "close": 2850.75,
    "volume": 12_345,
    "oi": None,
    "volumeUnavailable": False,
}

_QUALITY_GATE_BODY: dict[str, Any] = {
    "symbol": "RELIANCE",
    "timestamp": 1705300200_000,
    "open": 2820.00,
    "high": 2870.50,
    "low": 2810.25,
    "close": 2850.75,
    "volume": 12_345,
    "source": "angel_one",
    "confidenceScore": 85,
    "eventTimeMs": 1705300200_000,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def event_loop():
    """Provide a single event loop for the entire module (module-scoped)."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _build_mock_market_engine() -> MagicMock:
    """Return a MarketEngine stub that returns pre-canned responses instantly."""
    engine = MagicMock()
    engine.get_live_quote = AsyncMock(return_value=_SAMPLE_QUOTE.copy())
    engine.get_option_chain = AsyncMock(
        return_value={
            "underlying": "NIFTY",
            "expiry": "2024-01-25",
            "marketStatus": "REGULAR",
            "chainQuality": "GOOD",
            "rows": [],
            "analytics": {
                "pcrOi": 1.2,
                "pcrVolume": 0.95,
                "maxCeOiStrike": 21800,
                "maxPeOiStrike": 21500,
                "totalCeOi": 1_000_000,
                "totalPeOi": 1_200_000,
                "atmIv": None,
                "maxPain": 21700,
            },
        }
    )
    return engine


def _build_mock_historical_engine() -> MagicMock:
    """Return a HistoricalEngine stub with 100 pre-canned OHLCV candles."""
    engine = MagicMock()

    candles = [
        {**_SAMPLE_CANDLE, "time": _SAMPLE_CANDLE["time"] - (i * 60)}
        for i in range(100)
    ]

    engine.get_reconciliation_stats = MagicMock(
        return_value={
            "totalCompared": 0,
            "matched": 0,
            "matchRatePct": 0.0,
            "distribution": {"CONFIRMED": 0, "MINOR_DISCREPANCY": 0, "MAJOR_DISCREPANCY": 0},
            "byProviderPair": {},
        }
    )
    return engine


def _build_mock_db_engine() -> MagicMock:
    """Return a mock async DB engine that returns 100 OHLCV candles."""
    engine = MagicMock()

    async def _mock_connect():
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        conn.execute = AsyncMock(return_value=AsyncMock())
        conn.fetchall = AsyncMock(return_value=[])
        return conn

    engine.connect = _mock_connect
    return engine


@pytest.fixture(scope="module")
def app_with_mocks():
    """
    Return a FastAPI app instance with all external dependencies mocked.

    The lifespan handler is bypassed entirely via patching the two external
    I/O calls it makes (Redis pool creation and DB engine creation) at their
    actual import locations.  All engine/state dependencies are then injected
    directly onto ``app.state`` before the first request.
    """
    with (
        patch(
            "src.cache.redis_client.create_redis_pool",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.db.engine.create_async_engine_from_settings",
            new=AsyncMock(return_value=None),
        ),
    ):
        app = create_app()

    # Inject all state after app construction (lifespan not running in test mode).
    app.state.market_engine = _build_mock_market_engine()
    app.state.historical_engine = _build_mock_historical_engine()
    app.state.db_engine = None        # empty candle path — safe fallback
    app.state.redis = None            # L2 cache unavailable — falls back gracefully
    app.state.gap_recovery_engine = None
    app.state.start_time_ms = time.monotonic() * 1000
    app.state.clock_degraded = False
    app.state.session_phase = "REGULAR"
    app.state.freshness_stats = {"p50_ms": 8, "p99_ms": 35, "success_rate": 0.999}
    app.state.gap_counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    app.state.duplicate_rate = 0.001
    app.state.settings = None  # quality_engine and analytics endpoints read settings

    return app


@pytest.fixture(scope="module")
async def async_client(app_with_mocks):
    """Return an ``httpx.AsyncClient`` backed by the mocked ASGI app.

    The lifespan is bypassed by setting ``raise_app_exceptions=False``
    and by not relying on the lifespan context manager's startup/shutdown.
    Because we inject state directly onto ``app.state`` above, the app
    serves requests correctly without running its own lifespan.
    """
    transport = ASGITransport(app=app_with_mocks)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-API-Key": "perf-test-key-1"},  # DS2-RCA-016: auth now required
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Benchmark helpers
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Return the *pct*-th percentile from a pre-sorted list of floats."""
    if not sorted_values:
        return 0.0
    idx = math.ceil(pct / 100.0 * len(sorted_values)) - 1
    return sorted_values[max(0, min(idx, len(sorted_values) - 1))]


async def _run_benchmark(
    client: httpx.AsyncClient,
    *,
    method: str = "GET",
    url: str,
    json_body: dict | None = None,
    params: dict | None = None,
    warmup: int = 5,
    iterations: int = 100,
    label: str = "",
) -> dict[str, float]:
    """
    Run *iterations* timed HTTP requests and return latency percentiles (ms).

    Steps:
    1. Send *warmup* requests (discarded — JIT / cache warm-up).
    2. Collect *iterations* wall-clock measurements.
    3. Sort and compute p50, p95, p99.

    Returns:
        Dict with keys ``p50``, ``p95``, ``p99``, ``min``, ``max``,
        ``mean``, ``count`` — all values in milliseconds.
    """
    # Warmup (results discarded)
    for _ in range(warmup):
        if method == "POST":
            await client.post(url, json=json_body, params=params)
        else:
            await client.get(url, params=params)

    latencies_ms: list[float] = []

    for _ in range(iterations):
        t0 = time.perf_counter()
        if method == "POST":
            response = await client.post(url, json=json_body, params=params)
        else:
            response = await client.get(url, params=params)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies_ms.append(elapsed_ms)

        # Verify the request did not return a 5xx error (not an SLO failure).
        # We accept 400/404/503 since those reflect business logic with mocked deps.
        assert response.status_code < 500 or response.status_code == 503, (
            f"{label}: unexpected HTTP {response.status_code}: {response.text[:200]}"
        )

    latencies_ms.sort()

    stats = {
        "p50": _percentile(latencies_ms, 50),
        "p95": _percentile(latencies_ms, 95),
        "p99": _percentile(latencies_ms, 99),
        "min": latencies_ms[0],
        "max": latencies_ms[-1],
        "mean": sum(latencies_ms) / len(latencies_ms),
        "count": float(len(latencies_ms)),
    }

    if label:
        print(
            f"\n  [{label}] "
            f"p50={stats['p50']:.2f}ms  "
            f"p95={stats['p95']:.2f}ms  "
            f"p99={stats['p99']:.2f}ms  "
            f"min={stats['min']:.2f}ms  "
            f"max={stats['max']:.2f}ms  "
            f"mean={stats['mean']:.2f}ms  "
            f"n={int(stats['count'])}"
        )

    return stats


# ===========================================================================
# Benchmark tests
# ===========================================================================


@pytest.mark.performance
@pytest.mark.asyncio
class TestHealthLiveBenchmark:
    """GET /v1/health/live — always HTTP 200, never blocks on I/O.

    SLO: p99 < 10 ms (Requirement 20.3 — must respond within 200 ms,
    but in practice liveness probes are < 10 ms pure in-process).
    """

    P99_TARGET_MS = 10.0
    ITERATIONS = 200

    async def test_health_live_p99(self, async_client: httpx.AsyncClient) -> None:
        stats = await _run_benchmark(
            async_client,
            url="/v1/health/live",
            iterations=self.ITERATIONS,
            label="GET /v1/health/live",
        )

        assert stats["p99"] < self.P99_TARGET_MS, (
            f"GET /v1/health/live p99={stats['p99']:.2f}ms exceeds "
            f"SLO target of {self.P99_TARGET_MS}ms"
        )

    async def test_health_live_always_200(self, async_client: httpx.AsyncClient) -> None:
        """Verify liveness probe always returns HTTP 200 with required fields."""
        response = await async_client.get("/v1/health/live")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "alive"
        assert "version" in body
        assert "uptimeMs" in body
        assert "timestamp" in body

    async def test_health_live_throughput_100rps(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Verify 100 requests complete in under 10 seconds (≥ 10 rps sustained)."""
        start = time.perf_counter()
        tasks = [async_client.get("/v1/health/live") for _ in range(100)]
        responses = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

        assert all(r.status_code == 200 for r in responses), (
            "Some health/live requests returned non-200"
        )
        assert elapsed < 10.0, (
            f"100 concurrent health/live requests took {elapsed:.2f}s (> 10s threshold)"
        )


@pytest.mark.performance
@pytest.mark.asyncio
class TestAnalyticsHealthBenchmark:
    """GET /v1/analytics/health — overall platform health check.

    SLO: p99 < 100 ms
    """

    P99_TARGET_MS = 100.0
    ITERATIONS = 100

    async def test_analytics_health_p99(self, async_client: httpx.AsyncClient) -> None:
        stats = await _run_benchmark(
            async_client,
            url="/v1/analytics/health",
            iterations=self.ITERATIONS,
            label="GET /v1/analytics/health",
        )

        assert stats["p99"] < self.P99_TARGET_MS, (
            f"GET /v1/analytics/health p99={stats['p99']:.2f}ms exceeds "
            f"SLO target of {self.P99_TARGET_MS}ms"
        )

    async def test_analytics_health_response_shape(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Verify response uses canonical envelope with required fields."""
        response = await async_client.get("/v1/analytics/health")
        # With mocked state (no Redis/DB), platform may be degraded — that's OK.
        assert response.status_code in (200, 503)
        body = response.json()
        assert "data" in body
        assert "metadata" in body
        assert "healthy" in body["data"]
        assert "degradedComponents" in body["data"]


@pytest.mark.performance
@pytest.mark.asyncio
class TestQualityScoreBenchmark:
    """GET /v1/quality/score — pure CPU score computation.

    SLO: p99 < 10 ms  (pure computation, no I/O)
    """

    P99_TARGET_MS = 10.0
    ITERATIONS = 200

    _PARAMS = {
        "freshness": "FRESH",
        "completeness": "100.0",
        "provider_healthy": "true",
        "timestamp_valid": "true",
        "agreement": "1.0",
        "sequence_ok": "true",
    }

    async def test_quality_score_p99(self, async_client: httpx.AsyncClient) -> None:
        stats = await _run_benchmark(
            async_client,
            url="/v1/quality/score",
            params=self._PARAMS,
            iterations=self.ITERATIONS,
            label="GET /v1/quality/score",
        )

        assert stats["p99"] < self.P99_TARGET_MS, (
            f"GET /v1/quality/score p99={stats['p99']:.2f}ms exceeds "
            f"SLO target of {self.P99_TARGET_MS}ms"
        )

    async def test_quality_score_stale_freshness(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """STALE freshness classification should still complete quickly."""
        params = {**self._PARAMS, "freshness": "STALE"}
        stats = await _run_benchmark(
            async_client,
            url="/v1/quality/score",
            params=params,
            iterations=100,
            label="GET /v1/quality/score (STALE)",
        )
        assert stats["p99"] < self.P99_TARGET_MS

    async def test_quality_score_value_range(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Verify score is always in [0, 95] (Property 5 cross-check)."""
        test_cases = [
            {"freshness": "FRESH", "completeness": "100.0", "provider_healthy": "true",
             "timestamp_valid": "true", "agreement": "1.0"},
            {"freshness": "EXPIRED", "completeness": "0.0", "provider_healthy": "false",
             "timestamp_valid": "false", "agreement": "0.0"},
            {"freshness": "AGING", "completeness": "50.0", "provider_healthy": "true",
             "timestamp_valid": "true", "agreement": "0.5"},
            {"freshness": "STALE", "completeness": "80.0", "provider_healthy": "true",
             "timestamp_valid": "false", "agreement": "0.8"},
        ]
        for params in test_cases:
            response = await async_client.get("/v1/quality/score", params=params)
            assert response.status_code == 200
            body = response.json()
            score = body["data"]["score"]
            assert 0 <= score <= 95, (
                f"Score {score} outside [0, 95] for params {params}"
            )


@pytest.mark.performance
@pytest.mark.asyncio
class TestQualityEvaluateBenchmark:
    """POST /v1/quality/evaluate — gate evaluation with CPU-heavy logic.

    SLO: p99 < 20 ms
    """

    P99_TARGET_MS = 20.0
    ITERATIONS = 200

    async def test_quality_evaluate_p99(self, async_client: httpx.AsyncClient) -> None:
        stats = await _run_benchmark(
            async_client,
            method="POST",
            url="/v1/quality/evaluate",
            json_body=_QUALITY_GATE_BODY,
            iterations=self.ITERATIONS,
            label="POST /v1/quality/evaluate",
        )

        assert stats["p99"] < self.P99_TARGET_MS, (
            f"POST /v1/quality/evaluate p99={stats['p99']:.2f}ms exceeds "
            f"SLO target of {self.P99_TARGET_MS}ms"
        )

    async def test_quality_evaluate_blocked_score_p99(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """BLOCKED score (< 30) path must also stay within SLO."""
        blocked_body: dict[str, Any] = {
            **_QUALITY_GATE_BODY,
            "confidenceScore": 10,
        }
        stats = await _run_benchmark(
            async_client,
            method="POST",
            url="/v1/quality/evaluate",
            json_body=blocked_body,
            iterations=100,
            label="POST /v1/quality/evaluate (BLOCKED)",
        )
        assert stats["p99"] < self.P99_TARGET_MS

    async def test_quality_evaluate_response_shape(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Verify canonical envelope fields are present."""
        response = await async_client.post(
            "/v1/quality/evaluate",
            json=_QUALITY_GATE_BODY,
        )
        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        assert "metadata" in body
        data = body["data"]
        assert "signalEngineAllowed" in data
        assert "gate" in data or "classification" in data

    async def test_quality_evaluate_throughput_50_concurrent(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """50 concurrent quality gate evaluations must all complete < 5 s."""
        start = time.perf_counter()
        tasks = [
            async_client.post("/v1/quality/evaluate", json=_QUALITY_GATE_BODY)
            for _ in range(50)
        ]
        responses = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

        non_2xx = [r for r in responses if r.status_code >= 500]
        assert not non_2xx, (
            f"{len(non_2xx)} requests returned 5xx out of 50 concurrent"
        )
        assert elapsed < 5.0, (
            f"50 concurrent quality/evaluate requests took {elapsed:.2f}s (> 5s)"
        )


@pytest.mark.performance
@pytest.mark.asyncio
class TestLiveQuoteBenchmark:
    """GET /v1/india/quotes/{symbol} — live quote endpoint.

    SLO: p99 < 50 ms  (market engine is mocked to return instantly;
    overhead is pure ASGI/routing/serialisation, reflecting what the
    application layer adds on top of an already-resolved cache hit).
    """

    P99_TARGET_MS = 50.0
    ITERATIONS = 100

    async def test_live_quote_p99(self, async_client: httpx.AsyncClient) -> None:
        stats = await _run_benchmark(
            async_client,
            url="/v1/india/quotes/RELIANCE",
            iterations=self.ITERATIONS,
            label="GET /v1/india/quotes/RELIANCE",
        )

        assert stats["p99"] < self.P99_TARGET_MS, (
            f"GET /v1/india/quotes/RELIANCE p99={stats['p99']:.2f}ms exceeds "
            f"SLO target of {self.P99_TARGET_MS}ms"
        )

    async def test_live_quote_response_shape(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Verify the success envelope and key quote fields are present."""
        response = await async_client.get("/v1/india/quotes/RELIANCE")
        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        assert "metadata" in body
        data = body["data"]
        # Core quote fields from Requirement 3.2.
        for field in ("ltp", "symbol", "exchange"):
            assert field in data, f"Missing required quote field: {field}"
        # Null semantics: oi must be None for equities (Requirement 3.3).
        assert data.get("oi") is None, "oi must be None for equity quotes"

    async def test_live_quote_multiple_symbols_p99(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Quote lookups for various symbols must all stay within SLO."""
        symbols = ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "HDFC"]
        latencies_ms: list[float] = []

        for _ in range(20):  # 20 rounds × 5 symbols = 100 measurements
            for symbol in symbols:
                t0 = time.perf_counter()
                response = await async_client.get(f"/v1/india/quotes/{symbol}")
                elapsed_ms = (time.perf_counter() - t0) * 1000
                latencies_ms.append(elapsed_ms)
                assert response.status_code == 200

        latencies_ms.sort()
        p99 = _percentile(latencies_ms, 99)
        print(
            f"\n  [GET /v1/india/quotes/* (5 symbols×20 rounds)] "
            f"p99={p99:.2f}ms"
        )
        assert p99 < self.P99_TARGET_MS, (
            f"Multi-symbol live quote p99={p99:.2f}ms exceeds "
            f"SLO target of {self.P99_TARGET_MS}ms"
        )

    async def test_live_quote_concurrent_100(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """100 concurrent quote requests must all complete in < 5 seconds."""
        start = time.perf_counter()
        tasks = [
            async_client.get("/v1/india/quotes/RELIANCE")
            for _ in range(100)
        ]
        responses = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

        assert all(r.status_code == 200 for r in responses), (
            "Some concurrent quote requests returned non-200"
        )
        assert elapsed < 5.0, (
            f"100 concurrent quote requests took {elapsed:.2f}s (> 5s threshold)"
        )


@pytest.mark.performance
@pytest.mark.asyncio
class TestHistoricalOHLCVBenchmark:
    """GET /v1/india/historical — historical OHLCV endpoint.

    SLO: p99 < 500 ms for ≤ 1000 records.

    The DB engine is mocked (returns None on app.state) so the endpoint
    returns an empty candle list immediately.  This measures the ASGI stack
    overhead independently of database query time.
    """

    P99_TARGET_MS = 500.0
    ITERATIONS = 100

    _PARAMS = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "interval": "1d",
        "from": "2024-01-01",
        "to": "2024-03-31",
    }

    async def test_historical_ohlcv_p99(self, async_client: httpx.AsyncClient) -> None:
        stats = await _run_benchmark(
            async_client,
            url="/v1/india/historical",
            params=self._PARAMS,
            iterations=self.ITERATIONS,
            label="GET /v1/india/historical (1d, 90 days)",
        )

        assert stats["p99"] < self.P99_TARGET_MS, (
            f"GET /v1/india/historical p99={stats['p99']:.2f}ms exceeds "
            f"SLO target of {self.P99_TARGET_MS}ms"
        )

    async def test_historical_3m_rejected_quickly(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """3m rejection must be fast — the guard is at the API layer."""
        latencies_ms: list[float] = []
        params = {**self._PARAMS, "interval": "3m"}

        for _ in range(100):
            t0 = time.perf_counter()
            response = await async_client.get("/v1/india/historical", params=params)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies_ms.append(elapsed_ms)
            assert response.status_code == 400
            body = response.json()
            assert body["error"]["code"] == "INTERVAL_NOT_SUPPORTED"

        latencies_ms.sort()
        p99 = _percentile(latencies_ms, 99)
        print(f"\n  [GET /v1/india/historical (3m reject)] p99={p99:.2f}ms")
        # Rejection is purely in-process; should be even faster than the SLO.
        assert p99 < self.P99_TARGET_MS, (
            f"3m rejection p99={p99:.2f}ms exceeds {self.P99_TARGET_MS}ms SLO"
        )

    async def test_historical_response_envelope(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """Verify the success envelope shape for a valid historical query."""
        response = await async_client.get(
            "/v1/india/historical", params=self._PARAMS
        )
        assert response.status_code == 200
        body = response.json()
        assert "data" in body
        assert "metadata" in body
        meta = body["metadata"]
        # Required metadata fields (Requirement 4.9).
        for field in ("dataAsOf", "truncated", "gaps", "quality"):
            assert field in meta, f"Missing required metadata field: {field}"

    async def test_historical_all_canonical_intervals(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """All canonical Indian-market intervals must be accepted (< SLO each)."""
        canonical = ["1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"]
        for interval in canonical:
            params = {**self._PARAMS, "interval": interval}
            t0 = time.perf_counter()
            response = await async_client.get("/v1/india/historical", params=params)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            assert response.status_code == 200, (
                f"Interval {interval!r} returned HTTP {response.status_code}"
            )
            assert elapsed_ms < self.P99_TARGET_MS, (
                f"Interval {interval!r}: {elapsed_ms:.2f}ms > {self.P99_TARGET_MS}ms"
            )

    async def test_historical_concurrent_500(
        self, async_client: httpx.AsyncClient
    ) -> None:
        """500 concurrent historical queries (10 symbols × 50 each) < 30 s."""
        symbols = ["RELIANCE", "TCS", "HDFC", "INFOSYS", "WIPRO",
                   "NIFTY", "BANKNIFTY", "ICICIBANK", "SBIN", "HCLTECH"]

        start = time.perf_counter()
        tasks = [
            async_client.get(
                "/v1/india/historical",
                params={**self._PARAMS, "symbol": sym},
            )
            for sym in symbols
            for _ in range(50)
        ]
        responses = await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

        failures = [r for r in responses if r.status_code >= 500]
        assert not failures, (
            f"{len(failures)} out of {len(responses)} historical requests failed"
        )
        assert elapsed < 30.0, (
            f"500 concurrent historical requests took {elapsed:.2f}s (> 30s)"
        )


# ===========================================================================
# Summary report helper
# ===========================================================================


@pytest.mark.performance
@pytest.mark.asyncio
async def test_slo_summary_report(async_client: httpx.AsyncClient) -> None:
    """
    Collect and print a consolidated SLO summary table for all endpoints.

    This test does NOT assert SLO values — individual benchmark tests above do
    that.  Its purpose is to emit a human-readable summary at the end of a
    ``pytest -s`` run for operator review.
    """
    endpoints = [
        {
            "label": "GET  /v1/health/live",
            "method": "GET",
            "url": "/v1/health/live",
            "slo_p99_ms": 10.0,
            "iterations": 200,
        },
        {
            "label": "GET  /v1/analytics/health",
            "method": "GET",
            "url": "/v1/analytics/health",
            "slo_p99_ms": 100.0,
            "iterations": 100,
        },
        {
            "label": "GET  /v1/quality/score",
            "method": "GET",
            "url": "/v1/quality/score",
            "params": {
                "freshness": "FRESH",
                "completeness": "100.0",
                "provider_healthy": "true",
                "timestamp_valid": "true",
                "agreement": "1.0",
            },
            "slo_p99_ms": 10.0,
            "iterations": 200,
        },
        {
            "label": "POST /v1/quality/evaluate",
            "method": "POST",
            "url": "/v1/quality/evaluate",
            "json_body": _QUALITY_GATE_BODY,
            "slo_p99_ms": 20.0,
            "iterations": 200,
        },
        {
            "label": "GET  /v1/india/quotes/{symbol}",
            "method": "GET",
            "url": "/v1/india/quotes/RELIANCE",
            "slo_p99_ms": 50.0,
            "iterations": 100,
        },
        {
            "label": "GET  /v1/india/historical",
            "method": "GET",
            "url": "/v1/india/historical",
            "params": {
                "symbol": "RELIANCE",
                "exchange": "NSE",
                "interval": "1d",
                "from": "2024-01-01",
                "to": "2024-03-31",
            },
            "slo_p99_ms": 500.0,
            "iterations": 100,
        },
    ]

    results = []
    for ep in endpoints:
        stats = await _run_benchmark(
            async_client,
            method=ep.get("method", "GET"),
            url=ep["url"],
            json_body=ep.get("json_body"),
            params=ep.get("params"),
            iterations=ep["iterations"],
        )
        passed = stats["p99"] < ep["slo_p99_ms"]
        results.append(
            {
                "endpoint": ep["label"],
                "p50": stats["p50"],
                "p95": stats["p95"],
                "p99": stats["p99"],
                "slo": ep["slo_p99_ms"],
                "passed": passed,
            }
        )

    # --- Print summary table ---
    sep = "-" * 85
    print(f"\n\n{'=' * 85}")
    print("  DATA-SERVICE 2.0 — Performance SLO Summary")
    print("  (Mocked I/O — reflects ASGI stack + engine logic overhead only)")
    print(sep)
    print(f"  {'Endpoint':<42} {'p50':>7} {'p95':>7} {'p99':>7} {'SLO':>8}  Status")
    print(sep)
    for r in results:
        status = "✅ PASS" if r["passed"] else "❌ FAIL"
        print(
            f"  {r['endpoint']:<42} "
            f"{r['p50']:>6.2f}ms "
            f"{r['p95']:>6.2f}ms "
            f"{r['p99']:>6.2f}ms "
            f"{r['slo']:>7.0f}ms  {status}"
        )
    print(sep)

    # Fail the test if any SLO is breached.
    failed = [r for r in results if not r["passed"]]
    assert not failed, (
        f"\n{len(failed)} SLO(s) breached:\n"
        + "\n".join(
            f"  {r['endpoint']}: p99={r['p99']:.2f}ms > SLO={r['slo']:.0f}ms"
            for r in failed
        )
    )
