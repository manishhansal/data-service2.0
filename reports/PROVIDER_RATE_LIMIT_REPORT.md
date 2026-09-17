# PROVIDER RATE LIMIT REPORT
## data-service2.0

**Last verified:** 2026-09-17  
**Git commit:** 5dd69a2 (fix/bugs)  
**Test suite:** 4821 passed, 0 failed (clean env); 175 rate-limit/gateway tests pass

---

## CURRENT STATUS

| Gate | Status | Evidence |
|------|--------|---------|
| Angel One: 3 req/s enforcement | PARTIAL | Unit-tested (175 tests pass). Live load test: NOT_VERIFIED_LIVE |
| Upstox: 50/s enforcement | PARTIAL | Unit-tested. Live load test: NOT_VERIFIED_LIVE |
| Upstox: 500/min enforcement | PARTIAL | Unit-tested. Live load test: NOT_VERIFIED_LIVE |
| Upstox: 2000/30min enforcement | PARTIAL | Unit-tested. Live load test: NOT_VERIFIED_LIVE |
| API consumer rate limiting | NOT IMPLEMENTED | `RateLimitMiddleware` exists but not mounted in server |
| No retry storm | PASS | Observed in drill runs |

**Live provider load test:** NOT_VERIFIED_LIVE  
Blocker: Running sustained Upstox rate-limit load test against live provider without confirmed test quota is unsafe. Provider quota consumption cannot be undone.

---

## CONFIGURED RATE LIMITS

| Provider | Bucket 1 (req/s) | Bucket 2 (req/min) | Bucket 3 (req/30min) |
|---------|-----------------|---------------------|----------------------|
| Angel One | 3.0 req/s | N/A | N/A |
| Upstox | 50.0 req/s | 500/min | 2000/30min |

---

## IMPLEMENTATION DETAIL

| Feature | Status |
|---------|--------|
| `HierarchicalRateLimiter` class | IMPLEMENTED |
| Bucket 1: 50 req/s (token bucket, Redis-backed) | IMPLEMENTED |
| Bucket 2: 500 req/min (sorted-set sliding window) | IMPLEMENTED |
| Bucket 3: 2000 req/30min (sorted-set sliding window) | IMPLEMENTED |
| Per-provider (Upstox only for multi-window) | IMPLEMENTED |
| Redis pipeline (atomic count) | IMPLEMENTED |
| Jitter in `acquire()` loop | IMPLEMENTED |
| Graceful Redis failure fallback | IMPLEMENTED |
| `get_window_usage()` diagnostics | IMPLEMENTED |

---

## LIVE API LOAD TEST RESULTS (2026-09-17, data-service-api:8200)

Executed against `http://localhost:8200` using the running `data-service-api` container.

### Test 1: 100 unauthenticated /v1/health/live at ~50 req/s
| Metric | Value |
|--------|-------|
| Total requests | 100 |
| 200 OK | 100 |
| 429 | 0 |
| p50 latency | 9.3 ms |
| p95 latency | 37.0 ms |
| p99 latency | 48.5 ms |
| max latency | 48.5 ms |

### Test 2: 50 authenticated /v1/india/market/status (rapid)
| Metric | Value |
|--------|-------|
| Total requests | 50 |
| 200 OK | 50 |
| 429 | 0 |
| p50 latency | 2.5 ms |
| p95 latency | 7.9 ms |
| max latency | 21.9 ms |

### Test 3: 200 rapid burst (consumer RL not mounted)
| Metric | Value |
|--------|-------|
| Total requests | 200 |
| 200 OK | 200 |
| 429 | 0 |
| Rate limiter triggered | NO (middleware not mounted) |

**Note:** The consumer `RateLimitMiddleware` (`src/middleware/rate_limiter.py`) is implemented but **not mounted** in `src/server.py`. Inbound API rate limiting is not active.

---

## REMAINING GAPS

| Gap | Status |
|-----|--------|
| Live Upstox provider rate-limit load test (50/s sustained) | NOT_VERIFIED_LIVE |
| Live Angel One provider rate-limit load test (3/s) | NOT_VERIFIED_LIVE |
| Consumer API rate limiting (inbound) | NOT IMPLEMENTED — middleware exists, not mounted |
| 429 from provider observed and handled | NOT_VERIFIED_LIVE |

---

## HISTORICAL — SUPERSEDED

> Prior version stated "Upstox per-minute quota (500/min) not enforced" and "Upstox per-30-min quota (2000/30min) not enforced" as OPEN GAPS.  
> These are SUPERSEDED — `HierarchicalRateLimiter` implements all three windows as of Phase B-K.  
> The remaining gap is live runtime validation against the actual provider.
