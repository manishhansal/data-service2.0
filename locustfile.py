"""
locustfile.py — DATA-SERVICE 2.0 Manual Load Testing Scenarios
================================================================

This Locust file is used for manual load testing against a live or staging
deployment of DATA-SERVICE 2.0.  It models realistic consumer traffic patterns
based on the AlphaForge data requirements matrix (Requirement 21.1).

Prerequisites
-------------
1. Install dev dependencies::

       pip install -e ".[dev]"

2. Start a local instance::

       docker-compose up -d
       # (or) DATA_SERVICE_PORT=8200 uvicorn src.server:app --port 8200

Usage
-----
Interactive UI::

    locust -f locustfile.py --host=http://localhost:8200

Headless (CI / automated)::

    locust -f locustfile.py \
        --host=http://localhost:8200 \
        --users=100 \
        --spawn-rate=10 \
        --run-time=60s \
        --headless \
        --only-summary

Load shapes available
---------------------
* ``IndiaLiveDataUser``   — simulates India scalping / options workbench consumers
* ``HistoricalDataUser``  — simulates backtesting / strategy-lab consumers
* ``QualityGateUser``     — simulates signal-engine consumers polling quality gate
* ``CryptoDataUser``      — simulates crypto futures overview consumers
* ``HealthCheckUser``     — simulates orchestration / k8s liveness probes
* ``MixedConsumerUser``   — representative blend of all consumer types

SLO targets (Requirements 18.2, 20.1)
--------------------------------------
Endpoint                              p99 target
GET  /v1/health/live                  < 10 ms
GET  /v1/analytics/health             < 100 ms
POST /v1/quality/evaluate             < 20 ms
GET  /v1/quality/score                < 10 ms
GET  /v1/india/quotes/{symbol}        < 50 ms
GET  /v1/india/historical             < 500 ms (≤1000 records)
GET  /v1/india/option-chain           < 200 ms
GET  /v1/crypto/futures/overview      < 100 ms
GET  /v1/instruments/fno-universe     < 50 ms
"""

from __future__ import annotations

import json
import random
from typing import Any

from locust import HttpUser, TaskSet, between, events, task


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# NSE equity symbols commonly used in Indian market strategies.
_EQUITY_SYMBOLS = [
    "RELIANCE",
    "TCS",
    "HDFC",
    "INFOSYS",
    "WIPRO",
    "ICICIBANK",
    "SBIN",
    "HCLTECH",
    "KOTAKBANK",
    "LT",
    "HINDUNILVR",
    "ITC",
    "AXISBANK",
    "ASIANPAINT",
    "MARUTI",
]

# NSE index underlyings used for option chain and F&O queries.
_INDEX_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]

# F&O-eligible equity underlyings.
_FNO_SYMBOLS = [
    "RELIANCE", "TCS", "HDFC", "INFOSYS", "WIPRO",
    "ICICIBANK", "SBIN", "AXISBANK", "LT", "BHARTIARTL",
]

# Crypto symbols tracked by the platform.
_CRYPTO_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
_CRYPTO_CURRENCIES = ["BTC", "ETH", "SOL"]

# Canonical Indian-market intervals.
_CANONICAL_INTERVALS = ["1m", "5m", "10m", "15m", "30m", "1h", "1d"]

# Sample quality gate request body.
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
# Common request headers (consumers must supply a valid API key or JWT in prod)
# ---------------------------------------------------------------------------
_DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    # Replace with a valid API key for authenticated deployments.
    "X-API-Key": "test-load-key",
}


# ===========================================================================
# Task sets
# ===========================================================================


class IndiaLiveTaskSet(TaskSet):
    """India live-data tasks: quotes, option chain, market status.

    Represents the traffic pattern of:
    - India Scalping strategy (Req 21.1 — live LTP/OI/ticks)
    - India Options Workbench (live option chain)
    - Market Heatmap (live quotes, changePct, volume)
    """

    @task(5)
    def get_live_quote(self) -> None:
        """GET /v1/india/quotes/{symbol} — live equity/index quote."""
        symbol = random.choice(_EQUITY_SYMBOLS + _INDEX_UNDERLYINGS)
        with self.client.get(
            f"/v1/india/quotes/{symbol}",
            headers=_DEFAULT_HEADERS,
            name="/v1/india/quotes/{symbol}",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Unexpected status {resp.status_code}: {resp.text[:100]}")

    @task(3)
    def get_option_chain(self) -> None:
        """GET /v1/india/option-chain — option chain snapshot."""
        underlying = random.choice(_INDEX_UNDERLYINGS)
        with self.client.get(
            "/v1/india/option-chain",
            params={"underlying": underlying},
            headers=_DEFAULT_HEADERS,
            name="/v1/india/option-chain",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200,):
                resp.success()
            elif resp.status_code in (400, 404):
                # Acceptable for closed market or missing data.
                resp.success()
            else:
                resp.failure(f"option-chain {resp.status_code}: {resp.text[:100]}")

    @task(2)
    def get_market_status(self) -> None:
        """GET /v1/india/market/status — session phase + holidays."""
        with self.client.get(
            "/v1/india/market/status",
            headers=_DEFAULT_HEADERS,
            name="/v1/india/market/status",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"market/status {resp.status_code}")

    @task(1)
    def get_instruments(self) -> None:
        """GET /v1/instruments — instrument list with filters."""
        params = {
            "exchange": "NSE",
            "instrumentType": random.choice(["EQ", "FUTIDX", "OPTIDX"]),
        }
        with self.client.get(
            "/v1/instruments",
            params=params,
            headers=_DEFAULT_HEADERS,
            name="/v1/instruments",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"instruments {resp.status_code}")


class HistoricalTaskSet(TaskSet):
    """Historical OHLCV data tasks.

    Represents the traffic pattern of:
    - India Backtesting / Strategy Lab
    - India F&O Trend History (daily OHLCV + OI)
    - India Best Time analysis (5m, 1h)
    """

    @task(4)
    def get_historical_daily(self) -> None:
        """GET /v1/india/historical — daily OHLCV (1d interval)."""
        symbol = random.choice(_EQUITY_SYMBOLS + _INDEX_UNDERLYINGS)
        with self.client.get(
            "/v1/india/historical",
            params={
                "symbol": symbol,
                "exchange": "NSE",
                "interval": "1d",
                "from": "2024-01-01",
                "to": "2024-06-30",
            },
            headers=_DEFAULT_HEADERS,
            name="/v1/india/historical?interval=1d",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"historical/1d {resp.status_code}: {resp.text[:100]}")

    @task(3)
    def get_historical_intraday(self) -> None:
        """GET /v1/india/historical — intraday OHLCV (5m interval)."""
        symbol = random.choice(_EQUITY_SYMBOLS)
        interval = random.choice(["5m", "15m", "1h"])
        with self.client.get(
            "/v1/india/historical",
            params={
                "symbol": symbol,
                "exchange": "NSE",
                "interval": interval,
                "from": "2024-01-15",
                "to": "2024-01-31",
            },
            headers=_DEFAULT_HEADERS,
            name=f"/v1/india/historical?interval={interval}",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"historical/{interval} {resp.status_code}")

    @task(2)
    def get_historical_status(self) -> None:
        """GET /v1/india/historical/status — coverage + gap summary."""
        with self.client.get(
            "/v1/india/historical/status",
            headers=_DEFAULT_HEADERS,
            name="/v1/india/historical/status",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"historical/status {resp.status_code}")

    @task(1)
    def get_historical_gaps(self) -> None:
        """GET /v1/india/historical/gaps — gap records."""
        symbol = random.choice(_EQUITY_SYMBOLS)
        with self.client.get(
            "/v1/india/historical/gaps",
            params={"symbol": symbol, "limit": 50},
            headers=_DEFAULT_HEADERS,
            name="/v1/india/historical/gaps",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"historical/gaps {resp.status_code}")

    @task(1)
    def get_3m_interval_rejected(self) -> None:
        """GET /v1/india/historical?interval=3m — must return HTTP 400.

        Validates Requirement 1.5: 3m is permanently unsupported.
        """
        with self.client.get(
            "/v1/india/historical",
            params={
                "symbol": "RELIANCE",
                "exchange": "NSE",
                "interval": "3m",
                "from": "2024-01-01",
                "to": "2024-01-31",
            },
            headers=_DEFAULT_HEADERS,
            name="/v1/india/historical?interval=3m (expect 400)",
            catch_response=True,
        ) as resp:
            if resp.status_code == 400:
                # Confirm correct error code.
                try:
                    body = resp.json()
                    if body.get("error", {}).get("code") == "INTERVAL_NOT_SUPPORTED":
                        resp.success()
                    else:
                        resp.failure(f"Wrong error code: {body}")
                except Exception:  # noqa: BLE001
                    resp.success()
            else:
                resp.failure(
                    f"Expected HTTP 400 for 3m interval, got {resp.status_code}"
                )


class QualityGateTaskSet(TaskSet):
    """Signal quality gate tasks.

    Represents the traffic pattern of:
    - Signal Quality Gate consumers (Req 21.1)
    - India Paper Trading quality checks
    """

    @task(6)
    def evaluate_quality_gate(self) -> None:
        """POST /v1/quality/evaluate — gate evaluation."""
        body = {
            **_QUALITY_GATE_BODY,
            "symbol": random.choice(_EQUITY_SYMBOLS + _INDEX_UNDERLYINGS),
            "confidenceScore": random.randint(0, 95),
        }
        with self.client.post(
            "/v1/quality/evaluate",
            json=body,
            headers=_DEFAULT_HEADERS,
            name="POST /v1/quality/evaluate",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"quality/evaluate {resp.status_code}: {resp.text[:100]}")

    @task(4)
    def get_quality_score(self) -> None:
        """GET /v1/quality/score — score from components."""
        freshness = random.choice(["FRESH", "AGING", "STALE", "EXPIRED", "UNKNOWN"])
        with self.client.get(
            "/v1/quality/score",
            params={
                "freshness": freshness,
                "completeness": round(random.uniform(0, 100), 1),
                "provider_healthy": random.choice(["true", "false"]),
                "timestamp_valid": random.choice(["true", "false"]),
                "agreement": round(random.uniform(0, 1), 2),
            },
            headers=_DEFAULT_HEADERS,
            name="GET /v1/quality/score",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                body = resp.json()
                score = body.get("data", {}).get("score", -1)
                if 0 <= score <= 95:
                    resp.success()
                else:
                    resp.failure(f"Score {score} out of [0, 95] range")
            else:
                resp.failure(f"quality/score {resp.status_code}")


class CryptoTaskSet(TaskSet):
    """Crypto market data tasks.

    Represents the traffic pattern of:
    - Crypto Futures Overview (funding, OI, L/S ratio)
    - Crypto Options analytics (Deribit mark IV, max pain)
    - Crypto Signals (OHLCV klines)
    """

    @task(4)
    def get_futures_overview(self) -> None:
        """GET /v1/crypto/futures/overview — mark price, funding, OI."""
        with self.client.get(
            "/v1/crypto/futures/overview",
            headers=_DEFAULT_HEADERS,
            name="GET /v1/crypto/futures/overview",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 503):
                resp.success()
            else:
                resp.failure(f"crypto/futures/overview {resp.status_code}")

    @task(3)
    def get_crypto_klines(self) -> None:
        """GET /v1/crypto/klines/{symbol} — OHLCV klines."""
        symbol = random.choice(_CRYPTO_SYMBOLS)
        interval = random.choice(["1m", "5m", "1h", "4h", "1d"])
        with self.client.get(
            f"/v1/crypto/klines/{symbol}",
            params={
                "interval": interval,
                "from": "2024-01-01T00:00:00Z",
                "to": "2024-01-07T00:00:00Z",
            },
            headers=_DEFAULT_HEADERS,
            name="GET /v1/crypto/klines/{symbol}",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 404, 503):
                resp.success()
            else:
                resp.failure(f"crypto/klines {resp.status_code}")

    @task(2)
    def get_crypto_options_overview(self) -> None:
        """GET /v1/crypto/options/{currency}/overview — Deribit OptionsOverview."""
        currency = random.choice(_CRYPTO_CURRENCIES)
        with self.client.get(
            f"/v1/crypto/options/{currency}/overview",
            headers=_DEFAULT_HEADERS,
            name="GET /v1/crypto/options/{currency}/overview",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 404, 503):
                resp.success()
            else:
                resp.failure(f"crypto/options/{currency} {resp.status_code}")

    @task(1)
    def get_long_short_ratio(self) -> None:
        """GET /v1/crypto/futures/long-short/{symbol} — L/S ratio."""
        symbol = random.choice(_CRYPTO_SYMBOLS)
        with self.client.get(
            f"/v1/crypto/futures/long-short/{symbol}",
            headers=_DEFAULT_HEADERS,
            name="GET /v1/crypto/futures/long-short/{symbol}",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 404, 503):
                resp.success()
            else:
                resp.failure(f"long-short {resp.status_code}")


class HealthProbeTaskSet(TaskSet):
    """Kubernetes / orchestration health probe traffic.

    Models high-frequency liveness probes from container orchestration.
    """

    @task(10)
    def liveness_probe(self) -> None:
        """GET /v1/health/live — always HTTP 200, never blocks."""
        with self.client.get(
            "/v1/health/live",
            name="GET /v1/health/live",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                body = resp.json()
                if body.get("status") == "alive":
                    resp.success()
                else:
                    resp.failure(f"Unexpected alive body: {body}")
            else:
                resp.failure(f"liveness {resp.status_code}")

    @task(3)
    def readiness_probe(self) -> None:
        """GET /v1/health/ready — checks Redis + PG."""
        with self.client.get(
            "/v1/health/ready",
            name="GET /v1/health/ready",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 503):
                # 503 is valid when dependencies are not available.
                resp.success()
            else:
                resp.failure(f"readiness {resp.status_code}")

    @task(2)
    def analytics_health(self) -> None:
        """GET /v1/analytics/health — overall platform health."""
        with self.client.get(
            "/v1/analytics/health",
            headers=_DEFAULT_HEADERS,
            name="GET /v1/analytics/health",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 503):
                resp.success()
            else:
                resp.failure(f"analytics/health {resp.status_code}")


# ===========================================================================
# User classes
# ===========================================================================


class IndiaLiveDataUser(HttpUser):
    """User simulating India live-data consumers (scalping, options workbench).

    Wait time: 0.1–0.5 s between tasks (represents high-frequency polling
    during market hours with a small jitter to prevent thundering-herd).

    Representative of:
    - India Scalping  (Req 21.1, 1m/5m live data)
    - India Options Workbench (live option chain)
    - Market Heatmap (live quotes)
    """

    tasks = [IndiaLiveTaskSet]
    wait_time = between(0.1, 0.5)


class HistoricalDataUser(HttpUser):
    """User simulating backtesting and historical data consumers.

    Wait time: 0.5–2.0 s between tasks (historical queries are less frequent
    than live data polls).

    Representative of:
    - Strategy Lab Backtest (1h/4h/1d OHLCV)
    - India F&O Trend History (daily EOD + OI)
    - India Best Time analysis
    """

    tasks = [HistoricalTaskSet]
    wait_time = between(0.5, 2.0)


class QualityGateUser(HttpUser):
    """User simulating signal-engine quality gate consumers.

    Wait time: 0.05–0.2 s (signal engines poll the quality gate very
    frequently during market hours).

    Representative of:
    - Signal Quality Gate (Req 21.1, live)
    - India Paper Trading
    """

    tasks = [QualityGateTaskSet]
    wait_time = between(0.05, 0.2)


class CryptoDataUser(HttpUser):
    """User simulating crypto market data consumers.

    Wait time: 1.0–5.0 s between tasks (crypto strategies typically operate
    on slower polling cadences than Indian market scalpers).

    Representative of:
    - Crypto Futures Overview (live)
    - Crypto Options analytics (Deribit)
    - Strategy Lab Backtest (crypto klines)
    """

    tasks = [CryptoTaskSet]
    wait_time = between(1.0, 5.0)


class HealthCheckUser(HttpUser):
    """User simulating k8s / orchestration health probes.

    Wait time: 2.0–10.0 s (probes are infrequent compared to data consumers
    but must always succeed quickly).
    """

    tasks = [HealthProbeTaskSet]
    wait_time = between(2.0, 10.0)


class MixedConsumerUser(HttpUser):
    """Representative blend of all consumer types.

    Task weights approximate production traffic distribution based on
    AlphaForge usage patterns (Req 21.1).  Use this user class for realistic
    end-to-end load testing.

    Traffic mix (approximate):
    - 40% India live data (quotes + option chain)
    - 25% Quality gate evaluations
    - 20% Historical OHLCV queries
    - 10% Crypto data
    -  5% Health probes
    """

    wait_time = between(0.1, 1.0)

    @task(8)
    def live_quote(self) -> None:
        """Live equity quote (India Scalping / Market Heatmap path)."""
        symbol = random.choice(_EQUITY_SYMBOLS)
        with self.client.get(
            f"/v1/india/quotes/{symbol}",
            headers=_DEFAULT_HEADERS,
            name="/v1/india/quotes/{symbol}",
            catch_response=True,
        ) as resp:
            resp.success() if resp.status_code == 200 else resp.failure(
                f"{resp.status_code}"
            )

    @task(6)
    def quality_evaluate(self) -> None:
        """Quality gate evaluation (Signal Quality Gate path)."""
        body = {
            **_QUALITY_GATE_BODY,
            "symbol": random.choice(_EQUITY_SYMBOLS),
            "confidenceScore": random.randint(30, 95),
        }
        with self.client.post(
            "/v1/quality/evaluate",
            json=body,
            headers=_DEFAULT_HEADERS,
            name="POST /v1/quality/evaluate",
            catch_response=True,
        ) as resp:
            resp.success() if resp.status_code == 200 else resp.failure(
                f"{resp.status_code}"
            )

    @task(4)
    def historical_daily(self) -> None:
        """Daily OHLCV (Strategy Lab Backtest / F&O Trend History path)."""
        symbol = random.choice(_FNO_SYMBOLS)
        with self.client.get(
            "/v1/india/historical",
            params={
                "symbol": symbol,
                "exchange": "NSE",
                "interval": "1d",
                "from": "2024-01-01",
                "to": "2024-03-31",
            },
            headers=_DEFAULT_HEADERS,
            name="/v1/india/historical?interval=1d",
            catch_response=True,
        ) as resp:
            resp.success() if resp.status_code == 200 else resp.failure(
                f"{resp.status_code}"
            )

    @task(2)
    def crypto_futures_overview(self) -> None:
        """Crypto Futures Overview (Binance mark price + funding + OI)."""
        with self.client.get(
            "/v1/crypto/futures/overview",
            headers=_DEFAULT_HEADERS,
            name="GET /v1/crypto/futures/overview",
            catch_response=True,
        ) as resp:
            resp.success() if resp.status_code in (200, 503) else resp.failure(
                f"{resp.status_code}"
            )

    @task(1)
    def health_live(self) -> None:
        """Liveness probe (k8s orchestration traffic)."""
        with self.client.get(
            "/v1/health/live",
            name="GET /v1/health/live",
            catch_response=True,
        ) as resp:
            resp.success() if resp.status_code == 200 else resp.failure(
                f"{resp.status_code}"
            )


# ===========================================================================
# Event hooks — custom reporting
# ===========================================================================


@events.test_start.add_listener
def on_test_start(environment: Any, **kwargs: Any) -> None:  # type: ignore[misc]
    """Print a banner at the start of each Locust run."""
    print("\n" + "=" * 65)
    print("  DATA-SERVICE 2.0 — Locust Load Test")
    print("  SLO targets: p99 < 50ms (quotes), < 500ms (historical),")
    print("               p99 < 20ms (quality), < 10ms (health/live)")
    print("=" * 65 + "\n")


@events.test_stop.add_listener
def on_test_stop(environment: Any, **kwargs: Any) -> None:  # type: ignore[misc]
    """Print a brief summary after the test run completes."""
    stats = environment.stats
    total = stats.total
    print("\n" + "-" * 65)
    print(f"  Total requests   : {total.num_requests}")
    print(f"  Failed requests  : {total.num_failures}")
    failure_pct = (
        total.num_failures / total.num_requests * 100
        if total.num_requests
        else 0
    )
    print(f"  Failure rate     : {failure_pct:.2f}%")
    print(f"  Avg response time: {total.avg_response_time:.2f} ms")
    print(f"  p99 response time: {total.get_response_time_percentile(0.99):.2f} ms")
    print("-" * 65 + "\n")
