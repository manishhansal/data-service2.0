# PROVIDER FAILURE DRILL REPORT
## data-service2.0

**Last verified:** 2026-09-18
**Git commit:** 56156ec (fix/bugs; prev: 5dd69a2)
**Test suite:** 4821 passed, 0 failed; 169 failover/circuit/drill tests pass

---

## CURRENT STATUS

```
PARTIAL — 3 of 5 drills PASS; 2 BLOCKED_BY_EXTERNAL (live provider credentials)
```

---

## DRILL RESULTS (2026-09-17, commit 5dd69a2)

| Drill | Result | Evidence |
|-------|--------|---------|
| Drill 1: Angel historical unavailable → Upstox fallback | **PASS** | CB forced OPEN; Upstox returned 4 candles; provenance=upstox; CB reset to CLOSED |
| Drill 3: Both providers down → EQ Yahoo fallback, F&O no Yahoo | **PASS** | Yahoo returned 6 EQ 1d candles; F&O returned 0 (correct) |
| Drill 5: CB OPEN → HALF_OPEN → CLOSED recovery | **PASS** | 3 failures → OPEN; 3s sleep → HALF_OPEN; success → CLOSED confirmed |
| Drill 2: Upstox historical unavailable → Angel fallback | BLOCKED_BY_EXTERNAL | Upstox OAuth token expired |
| Drill 6: Live streaming failover | BLOCKED_BY_EXTERNAL | Requires market hours + WS connections |

**Fixed in this pass:** Drill 1 was previously crashing due to `Redis.set_with_ttl` AttributeError (raw Redis client passed to CircuitBreaker instead of `RedisClient` wrapper). Fixed in `run_failure_drills.py`.

---

## CIRCUIT BREAKER VERIFICATION (Drill 5)

Execution trace:
```
Step 1: Circuit starts CLOSED                    ✅
Step 2: 3 failures → circuit OPENED              ✅ (log: circuit_open, failureRate=1.0)
Step 3: 3s recovery window → HALF_OPEN           ✅
Step 4: probe_succeeded → circuit CLOSED         ✅ (log: circuit_closed, reason=probe_succeeded)
```

| Feature | Status |
|---------|--------|
| CLOSED → OPEN → HALF_OPEN → CLOSED | **VERIFIED** (live drill 2026-09-17) |
| Per-provider × per-capability isolation | UNIT_TESTED |
| Failure threshold configurable | UNIT_TESTED |
| Redis-backed state | UNIT_TESTED |
| 429 not counted as failure | UNIT_TESTED |
| `record_failure()` accepts `RedisClient` wrapper | FIXED |

---

## FALLBACK ROUTING VERIFICATION

| Capability | Fallback chain | Drill result |
|-----------|--------------|-------------|
| EQ HISTORICAL_OHLCV | Angel One → Upstox → Yahoo Finance | PASS (Drill 3) |
| FO HISTORICAL_OHLCV | Angel One → Upstox (no Yahoo) | PASS (Drill 3, 0 candles when both down) |
| Angel One → Upstox | | PASS (Drill 1, 4 candles from Upstox) |

Key invariant VERIFIED: Yahoo Finance not in any F&O fallback chain.

---

## REMAINING DRILLS

| Drill | Status | Blocker |
|-------|--------|---------|
| Upstox → Angel live provider fallback | BLOCKED_BY_EXTERNAL | Upstox OAuth token expired |
| Live streaming failover (WS) | BLOCKED_BY_EXTERNAL | Requires NSE market hours + valid tokens |
| Rate-limit 429 behavior | NOT_VERIFIED_LIVE | Live provider required |
| Provider recovery under real load | NOT_VERIFIED_LIVE | Live provider required |
