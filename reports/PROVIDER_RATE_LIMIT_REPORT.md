# PROVIDER RATE LIMIT REPORT
## data-service2.0

**Report date:** 2026-09-17 (Phase 51) — **Updated:** 2026-09-17 (Phase B-K remediation)
**Scope:** Rate limiter implementation and verification status
**Evidence basis:** Static codebase analysis + unit tests. Live load test not yet executed.

---

## STATUS

```
IMPLEMENTED — hierarchical 3-window rate limiting (50/s + 500/min + 2000/30min)
NOT_VERIFIED_LIVE — realistic load test with live provider not yet executed
```

**SUPERSEDES:** Previous status "NOT_VERIFIED_LIVE — unit-tested only, 500/min and 2000/30min NOT enforced".

---

## CONFIGURED RATE LIMITS

| Provider | Bucket 1 (req/s) | Bucket 2 (req/min) | Bucket 3 (req/30min) | Correct? |
|---------|-----------------|---------------------|----------------------|---------|
| Angel One | 3.0 req/s | N/A | N/A | Yes |
| Upstox | 50.0 req/s | 500/min (FIXED) | 2000/30min (FIXED) | Yes — all 3 windows |

**FIXED (2026-09-17):** `HierarchicalRateLimiter` class added to `rate_limiter.py`.
All three Upstox quotas are now independently enforced using Redis sorted-set sliding windows.

---

## IMPLEMENTATION — HierarchicalRateLimiter

| Feature | Status | Evidence |
|---------|--------|---------|
| Bucket 1: 50 req/s (token bucket) | IMPLEMENTED | `TokenBucketRateLimiter`, Redis-backed |
| Bucket 2: 500 req/min (sliding window) | FIXED | `HierarchicalRateLimiter`, sorted-set ZREMRANGEBYSCORE |
| Bucket 3: 2000 req/30min (sliding window) | FIXED | Same, 1800s window |
| Per-provider (Upstox only for multi-window) | IMPLEMENTED | `provider_id == "upstox"` check |
| Redis pipeline (atomic count) | IMPLEMENTED | `pipe.zremrangebyscore` + `pipe.zcard` |
| Jitter in `acquire()` loop | IMPLEMENTED | `random.uniform(0.0, 0.02)` |
| Graceful Redis failure fallback | IMPLEMENTED | Logs warning, per-second bucket still enforced |
| `get_window_usage()` diagnostics | IMPLEMENTED | Returns current count, limit, remaining per window |

---

## REMAINING GAP

| Gap | Status |
|-----|--------|
| Realistic load test (sustained 50 req/s for 30s) | NOT_EXECUTED — requires live provider + credentials |
| p50/p95/p99 latency under load | NOT_MEASURED |
| 429 rate limit hit under burst | NOT_VERIFIED_LIVE |

A load test harness exists at `locustfile.py`. Live execution requires provider credentials and is separate from unit testing.

## ANGEL ONE RATE LIMIT TEST PROCEDURE

### Test: 3 req/s enforcement

1. Send 10 requests to `fetch_historical_ohlcv` for 10 different symbols in rapid succession
2. Measure: wall time for all 10 requests to complete
3. Expected: ~3.3 seconds minimum (10 ÷ 3 req/s)
4. Record: requests_attempted, requests_allowed, requests_delayed, 429_responses_received

### Expected result

```
requests_attempted: 10
requests_allowed:   10 (delayed, not rejected)
requests_delayed:   7 (approximately)
429_responses:      0 (limiter prevents sending, so no 429 from provider)
total_time_ms:      ≥3333ms
```

---

## UPSTOX RATE LIMIT TEST PROCEDURE

### Test 1: 50 req/s enforcement

1. Send 100 requests in rapid succession (within 2 seconds)
2. Expected: limiter caps at 50/s; requests complete in ~2 seconds minimum

### Test 2: Per-minute burst

1. Send 600 requests over 60 seconds (10 req/s average, within 50 req/s cap)
2. Observe whether Upstox returns 429 at the 500th request within the minute window
3. If 429 observed: add per-minute token bucket to `TokenBucketRateLimiter`

---

## LOAD TEST RESULTS (TEMPLATE — TO BE FILLED WITH REAL DATA)

### Angel One

| Metric | Value | Date |
|--------|-------|------|
| requests_attempted | — | NOT_RUN |
| requests_allowed | — | — |
| requests_delayed | — | — |
| 429_from_provider | — | — |
| retry_storms | — | — |
| min_latency_ms | — | — |
| p50_latency_ms | — | — |
| p99_latency_ms | — | — |

### Upstox

| Metric | Value | Date |
|--------|-------|------|
| requests_attempted | — | NOT_RUN |
| requests_allowed | — | — |
| requests_delayed | — | — |
| 429_from_provider | — | — |
| per_minute_429_observed | — | — |
| retry_storms | — | — |

---

## OPEN GAPS

| Gap | Severity | Remediation |
|-----|----------|------------|
| Upstox per-minute quota (500/min) not enforced | Medium | Add a second `TokenBucketRateLimiter` with `rate=500/60` for Upstox |
| Upstox per-30-min quota (2000/30min) not enforced | Low | Add a third bucket with `rate=2000/1800` |
| No load test executed | Medium | Run `locust -f locustfile.py --host http://localhost:8200` for ≥30 minutes under realistic backfill load |

---

*No live load test executed as of 2026-09-17. All rate limiter behavior is unit-tested only.*
