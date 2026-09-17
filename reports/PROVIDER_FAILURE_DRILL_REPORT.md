# PROVIDER FAILURE DRILL REPORT
## data-service2.0 — Phase 51 Report

**Report date:** 2026-09-17  
**Scope:** Circuit breaker and provider failover behavior under simulated failure  
**Evidence basis:** Static codebase analysis + unit tests. No live failure drill executed.

---

## STATUS

```
NOT_VERIFIED_LIVE — unit-tested only
```

The circuit breaker and fallback logic are implemented and unit-tested. No controlled failure drill has been executed against real providers. This report documents what is implemented and what must be tested.

---

## CIRCUIT BREAKER IMPLEMENTATION

### `CircuitBreaker` (`src/providers/circuit_breaker.py`)

| Feature | Status | Evidence |
|---------|--------|---------|
| States: CLOSED → OPEN → HALF_OPEN | `UT` | `test_circuit_breaker.py` |
| Per-provider × per-capability isolation | `UT` | `CircuitBreaker(provider, capability)` |
| Failure threshold configurable | `UT` | `CIRCUIT_BREAKER_FAILURE_THRESHOLD` env var |
| Recovery window configurable | `UT` | `CIRCUIT_BREAKER_RECOVERY_WINDOW_SEC` env var |
| Redis-backed state (cross-replica) | `UT` | State stored in Redis |
| 429 not counted as failure | `UT` | `record_rate_limit()` separate from `record_failure()` |
| One failing capability does not open others | `UT` | Separate circuit per capability |

### Capability isolation confirmed

Each of the following has its own circuit breaker key:

```
angel_one:HISTORICAL_OHLCV
angel_one:LIVE_QUOTE
angel_one:OPTION_GREEKS
angel_one:HISTORICAL_OI
angel_one:WEBSOCKET

upstox:HISTORICAL_OHLCV
upstox:LIVE_QUOTE
upstox:OPTION_GREEKS
upstox:OPTION_CHAIN
upstox:WEBSOCKET
```

A broken `upstox:WEBSOCKET` circuit does NOT affect `upstox:HISTORICAL_OHLCV`. This is confirmed in unit tests.

---

## FALLBACK ROUTING

| Scenario | Expected behavior | Status |
|----------|------------------|--------|
| Angel One historical OPEN | Fall to Upstox historical V3 | `UT` — `historical_engine.py` routing |
| Upstox historical OPEN | Fall to Angel One | `UT` — same |
| Both historical OPEN | Fall to Yahoo Finance (EQ 1d only) | `UT` |
| Angel One live OPEN | Upstox live (when wired) | `NVL` — Upstox not wired to MarketEngine |
| Upstox option chain OPEN | No fallback | `UT` — logs MISSING |
| Angel One option greeks OPEN | Fall to Upstox V3 | `UT` |

---

## LIVE FAILURE DRILL PROCEDURE (REQUIRED FOR LRV)

### Drill 1: Angel One unavailable, Upstox available

1. With both providers authenticated and responding normally, record baseline: 5 historical candle requests, 5 live quote requests
2. Simulate Angel One failure: configure `CIRCUIT_BREAKER_FAILURE_THRESHOLD=1` and force a 404/500 from `fetch_historical_ohlcv`
3. Verify: circuit opens on Angel One HISTORICAL_OHLCV within 1 request
4. Verify: next historical request routes to Upstox without error
5. Verify: Angel One LIVE_QUOTE circuit remains CLOSED (one capability failure must not propagate)
6. Verify: no fabricated data in response
7. Verify: DataIncident record created for the failure

### Drill 2: Upstox unavailable, Angel One available

1. Configure Upstox token to invalid value (simulate 401)
2. Verify: Upstox HISTORICAL_OHLCV circuit opens after `FAILURE_THRESHOLD` failures
3. Verify: requests fall through to Angel One
4. Verify: Upstox OPTION_CHAIN separately tracked (analytics key still working ≠ OAuth calls)

### Drill 3: Both providers unavailable

1. Open both Angel One and Upstox circuits
2. Verify: EQ 1d falls to Yahoo Finance
3. Verify: F&O intraday returns appropriate error (no fallback available)
4. Verify: Error response contains correct error code, no fabricated data

### Metrics to record for each drill

```
provider_unavailable_at:
circuit_opened_at:
first_fallback_request_at:
fallback_provider_used:
time_to_fallback_ms:
requests_attempted:
requests_failed:
requests_served_from_fallback:
fabricated_data: 0 (REQUIRED)
data_incidents_created:
```

---

## DRILL RESULTS (TEMPLATE — TO BE FILLED WITH REAL DATA)

| Drill | Status | time_to_fallback_ms | fabricated_data | incidents_created |
|-------|--------|--------------------|-----------------|--------------------|
| Angel unavailable, Upstox available | NOT_RUN | — | — | — |
| Upstox unavailable, Angel available | NOT_RUN | — | — | — |
| Both unavailable | NOT_RUN | — | — | — |
| Circuit recovery (HALF_OPEN test) | NOT_RUN | — | — | — |

---

*No live failure drills executed as of 2026-09-17. All circuit breaker behavior is unit-tested only.*
