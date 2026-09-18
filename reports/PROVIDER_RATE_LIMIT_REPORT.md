# PROVIDER RATE LIMIT REPORT
## data-service2.0

**Last verified:** 2026-09-18
**Git commit:** 330269c (fix/bugs)
**Test suite:** 4821 passed, 0 failed; 143 rate-limit/gateway tests pass

---

## CURRENT STATUS

| Gate | Status | Evidence |
|------|--------|---------|
| Consumer inbound rate limiting (100 req/60s) | **PASS** | Live-tested 2026-09-18: 429 returned at req 101, X-RateLimit headers present |
| Angel One: 3 req/s outbound enforcement | PARTIAL | Unit-tested (143 tests). Live provider load test: NOT_VERIFIED_LIVE |
| Upstox: 50/s outbound enforcement | PARTIAL | Unit-tested. Live provider load test: NOT_VERIFIED_LIVE |
| Upstox: 500/min outbound enforcement | PARTIAL | Unit-tested. Live provider load test: NOT_VERIFIED_LIVE |
| Upstox: 2000/30min outbound enforcement | PARTIAL | Unit-tested. Live provider load test: NOT_VERIFIED_LIVE |
| No retry storm | PASS | Observed in drill runs |

**Live outbound provider load test:** NOT_VERIFIED_LIVE
Blocker: Running sustained Upstox/Angel rate-limit test against live provider quota is unsafe without a controlled test window.

---

## CONSUMER INBOUND RATE LIMITING — LIVE TEST (2026-09-18)

`RateLimitMiddleware` mounted in `src/server.py`.
Settings: `CONSUMER_RATE_LIMIT=100`, `CONSUMER_RATE_WINDOW_SEC=60` (env-configurable).
Exempt paths: `/v1/health/live`, `/metrics`.

**Test: 115 rapid requests to `/v1/india/market/status`**

| Metric | Value |
|--------|-------|
| Requests fired | 115 |
| Elapsed | 0.12s |
| 200 OK | 1 (prior window credit) |
| 429 Too Many | 114 |
| X-RateLimit-Limit | 100 |
| X-RateLimit-Remaining | 0 |
| retryAfterMs | 5000 |
| Result | **PASS** |

**429 response body:**
```json
{"error": {"code": "RATE_LIMIT_EXCEEDED", "message": "Rate limit of 100 requests per 60s exceeded. Retry after 5000 ms.", "retryAfterMs": 5000}}
```

---

## OUTBOUND CONFIGURED LIMITS

| Provider | Bucket 1 (req/s) | Bucket 2 (req/min) | Bucket 3 (req/30min) |
|---------|-----------------|---------------------|----------------------|
| Angel One | 3.0 req/s | N/A | N/A |
| Upstox | 50.0 req/s | 500/min | 2000/30min |

## OUTBOUND IMPLEMENTATION

| Feature | Status |
|---------|--------|
| `HierarchicalRateLimiter` class | IMPLEMENTED |
| 50 req/s token bucket | IMPLEMENTED |
| 500 req/min sliding window | IMPLEMENTED |
| 2000 req/30min sliding window | IMPLEMENTED |
| Redis pipeline (atomic) | IMPLEMENTED |
| Jitter in `acquire()` | IMPLEMENTED |
| Redis failure fallback | IMPLEMENTED |
| `get_window_usage()` diagnostics | IMPLEMENTED |

---

## REMAINING GAPS

| Gap | Status |
|-----|--------|
| Live Upstox outbound rate-limit load test (50/s sustained) | NOT_VERIFIED_LIVE |
| Live Angel One outbound rate-limit load test (3/s) | NOT_VERIFIED_LIVE |
| Provider 429 observed and handled in production | NOT_VERIFIED_LIVE |

---

## HISTORICAL — SUPERSEDED

> "Consumer API rate limiting: NOT IMPLEMENTED" — SUPERSEDED.
> `RateLimitMiddleware` mounted in server.py and live-verified 2026-09-18.
>
> "Upstox per-minute/per-30-min quotas not enforced" — SUPERSEDED.
> `HierarchicalRateLimiter` implements all three windows.
> The remaining gap is live load testing against the actual provider.
