"""
tests/performance
==================

Performance and load tests for DATA-SERVICE 2.0.

This package contains two categories of tests:

1. ``test_load.py``  — pytest-based throughput/latency benchmarks that mock
   all external I/O (Redis, PostgreSQL, provider adapters) and assert SLO
   targets against the core API endpoints.  These tests are safe to run in
   CI without any live infrastructure.

2. ``locustfile.py`` (repo root) — Locust scenarios for manual/loadgen runs
   against a live or staging deployment.

Running the benchmark suite
----------------------------
::

    # From repo root (mocked, CI-safe)
    pytest tests/performance/ -m performance -v

Latency SLO targets (Requirements 18.2, 20.1)
----------------------------------------------
* GET  /v1/india/quotes/{symbol}    — p99 < 50 ms
* GET  /v1/india/historical         — p99 < 500 ms (≤1000 records)
* POST /v1/quality/evaluate         — p99 < 20 ms  (pure CPU)
* GET  /v1/quality/score            — p99 < 10 ms  (pure CPU)
* GET  /v1/analytics/health         — p99 < 100 ms
* GET  /v1/health/live              — p99 < 10 ms  (never blocks on I/O)
"""
