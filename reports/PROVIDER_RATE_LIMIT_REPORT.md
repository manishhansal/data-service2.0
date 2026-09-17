# PROVIDER RATE LIMIT REPORT
## data-service2.0 — Phase 51 Report

**Report date:** 2026-09-17  
**Scope:** Rate limiter implementation review and live verification status  
**Evidence basis:** Static codebase analysis + unit tests. No live load test executed.

---

## STATUS

```
NOT_VERIFIED_LIVE — unit-tested only
```

---

## CONFIGURED RATE LIMITS

| Provider | Configured limit | Documented limit | Correct? |
|---------|-----------------|-----------------|---------|
| Angel One | 3.0 req/s | 3.0 req/s | ✅ Matches |
| Upstox | 50.0 req/s | 50 req/s, 500/min, 2000/30min | ✅ RPS matches. Per-minute and per-30-min quotas NOT separately enforced (see gap below). |

### Upstox quota gap

Upstox has three distinct rate limits:
1. 50 requests/second
2. 500 requests/minute
3. 2,000 requests/30 minutes

The current implementation enforces only the **50 req/s** token bucket. The per-minute and per-30-minute quotas are not independently tracked. Under burst scenarios where 50 req/s is sustained for >10 seconds, the per-minute quota (500) would be exceeded.

**Risk:** Under backfill load (sustained API calls for multiple symbols), Upstox 429s may occur at the per-minute level even though the per-second limiter is satisfied.

**Severity:** Medium — affects backfill throughput. Historical requests would receive 429, which are retried with backoff. No data loss, but backfill may be slower than expected.

---

## RATE LIMITER IMPLEMENTATION

| Feature | Status | Evidence |
|---------|--------|---------|
| Token bucket algorithm | `UT` | `TokenBucketRateLimiter` in `rate_limiter.py` |
| Redis-backed (cross-replica) | `UT` | Lua atomic script; `test_rate_limiter.py` |
| Local fallback when Redis unavailable | `UT` | `_LocalBucket` fallback |
| 429 not counted in circuit breaker | `UT` | `record_rate_limit()` is a no-op vs failure |
| Per-provider separate bucket | `UT` | Keys are provider-namespaced |
| Burst behavior | `UT` | Token refill rate = configured req/s |

---

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
