# PROVIDER FAILURE DRILL REPORT
## data-service2.0

**Report date:** 2026-09-17 (Phase 51) — **Updated:** 2026-09-17 (Phase B-K remediation)
**Git commit:** 692bf3f
**Scope:** Circuit breaker and provider failover behavior under controlled failure
**Evidence basis:** Unit tests + controlled drills (2026-09-17)

---

## STATUS

```
PARTIAL — 2 of 5 drills PASS (no-credentials); 3 BLOCKED by provider credentials
```

SUPERSEDES: Previous status "NOT_VERIFIED_LIVE — unit-tested only, no live drills executed."

---

## DRILL RESULTS (2026-09-17)

Executed via `scripts/run_failure_drills.py` at 07:59 UTC 2026-09-17:

| Drill | Result | Evidence |
|-------|--------|---------|
| Drill 3: Both providers down → EQ Yahoo fallback, F&O no Yahoo | **PASS** | Yahoo returned 6 EQ 1d candles; F&O returned 0; ProviderGateway.get_fallback_chain("FO") confirmed Yahoo absent |
| Drill 5: CB OPEN → HALF_OPEN → CLOSED recovery | **PASS** | 3 failures → OPEN; 3s sleep → HALF_OPEN confirmed; record_success() → CLOSED confirmed |
| Drill 1: Angel historical unavailable → Upstox fallback | **BLOCKED** | Upstox credentials not in test environment |
| Drill 2: Upstox historical unavailable → Angel fallback | **NOT_EXECUTED** | Requires live Angel credentials |
| Drill 6: Live streaming failover | **NOT_EXECUTED** | Requires market hours + live connections |

---

## CIRCUIT BREAKER IMPLEMENTATION

| Feature | Status | Evidence |
|---------|--------|---------|
| CLOSED → OPEN → HALF_OPEN | **VERIFIED** | Drill 5 execution 2026-09-17 |
| Per-provider × per-capability isolation | UT | CircuitBreaker(provider, capability) |
| Failure threshold configurable | UT | CIRCUIT_BREAKER_FAILURE_THRESHOLD env var |
| Recovery window configurable | **VERIFIED** | Drill 5: recovery_window_sec=2 confirmed |
| Redis-backed state (cross-replica) | UT | Redis JSON state |
| 429 not counted as failure | UT | record_rate_limit() separate path |
| Capability isolation (one cap does not affect others) | UT | Separate circuit per capability |

Capability keys:
```
angel_one:HISTORICAL_OHLCV  angel_one:LIVE_QUOTE  angel_one:OPTION_GREEKS
angel_one:HISTORICAL_OI     angel_one:WEBSOCKET
upstox:HISTORICAL_OHLCV     upstox:LIVE_QUOTE     upstox:OPTION_GREEKS
upstox:OPTION_CHAIN         upstox:WEBSOCKET
```

---

## FALLBACK ROUTING (ProviderGateway — FIXED 2026-09-17)

ProviderGateway.get_fallback_chain() — capability-specific provider lists:

| Capability | Fallback chain | Yahoo for F&O |
|-----------|--------------|--------------|
| EQ HISTORICAL_OHLCV | Angel One → Upstox → Yahoo Finance | N/A |
| IDX HISTORICAL_OHLCV | Upstox → Angel One | N/A |
| FO HISTORICAL_OHLCV | Angel One → Upstox | **BLOCKED** (verified Drill 3) |
| FO LIVE_QUOTE | Angel One → Upstox | Blocked by design |
| FO OPTION_CHAIN | Upstox → Angel One | Blocked by design |

Key invariant VERIFIED: Yahoo Finance is not in any F&O fallback chain.

---

## REMAINING DRILLS

| Drill | Blocker | Required action |
|-------|---------|----------------|
| Angel → Upstox live provider fallback | Upstox OAuth expired 2026-09-14 | Refresh token via OAuth callback |
| Upstox → Angel live provider fallback | Angel credentials not configured | Configure ANGEL_ONE_* env vars |
| Live streaming failover | Requires market hours + WS connections | Run during NSE trading hours |
| Rate-limit load test (429 behavior) | Live provider required | Run with configured credentials |
| Provider recovery under real load | Same as above | Same |
