# REPORT 11 — FAILOVER CERTIFICATION
**Audit date:** 2026-09-13

---

## Status: 🟡 IMPLEMENTED BUT NOT RUNTIME-VERIFIED

Failover logic is fully coded. No real failure injection was possible (no running infra).

---

## Failover Architecture (Code Verified)

### Provider Gateway

File: `src/providers/gateway.py`

Components:
- `CapabilityMatrix`: maps provider × capability to supported/unsupported
- `CircuitBreaker`: CLOSED → OPEN → HALF_OPEN → CLOSED state machine
- `TokenBucketRateLimiter`: per-provider rate limiting (Redis-backed)

### Circuit Breaker States (Code)

```
CLOSED (normal operation)
  → failure_count >= threshold (default 5)
  → OPEN (reject all requests immediately)
  → after recovery_window_sec (default 60)
  → HALF_OPEN (allow 1 test request)
  → success → CLOSED
  → failure → OPEN again
```

Per-provider scoping: ✅ each provider has independent circuit breaker  
Per-capability scoping: 🟡 gateway routes by capability but circuit is per-provider, not per-capability  
429 handling: ✅ 429 does NOT increment failure counter (correct)  
Auth failure handling: ✅ `ProviderAuthError` does increment (correct)

---

## Failover Chain (Code Verified)

### Indian Live Data
```
Primary: Angel One SmartAPI (BROKER_AUTHENTICATED)
  → Upstox (BROKER_AUTHENTICATED)
  → NSE/Scrapling (EXCHANGE_DIRECT)
  → Yahoo Finance (AGGREGATOR, limited capability)
```

### Indian Historical
```
Primary: Angel One SmartAPI (intraday 1m–1h)
  → Upstox (intraday + daily)
  → Jugaad-data (daily bhavcopy)
  → OpenChart (intraday)
  → NSE/Scrapling (daily bhavcopy)
  → Yahoo Finance (daily/weekly EOD)
```

### Crypto
```
Primary: Binance (all crypto)
  → Deribit (options only — different capability)
  → Delta Exchange: ❌ MISSING — no failover to Delta possible
```

---

## Controlled Failure Tests (All Blocked)

| Scenario | Expected Behaviour | Actual | Status |
|---|---|---|---|
| Angel One 401 | Re-auth once, retry | Code: ✅ | ⛔ BLOCKED |
| Angel One 429 | ProviderRateLimitedError, no circuit | Code: ✅ | ⛔ BLOCKED |
| Angel One 500 | ProviderUnavailableError, circuit++ | Code: ✅ | ⛔ BLOCKED |
| Angel One unavailable → Upstox fallback | Upstox provides data | Code: ✅ | ⛔ BLOCKED |
| Upstox 401 → token refresh → retry | Code: ✅ | ⛔ BLOCKED | |
| NSE/Scrapling WAF block | Log + fallback | Code: ✅ | ⛔ BLOCKED |
| Yahoo as last resort | Only daily data, mark AGGREGATOR | Code: ✅ | ⛔ BLOCKED |
| Binance 429 | No retry storm, circuit safe | Code: ✅ | ⛔ BLOCKED |
| Redis down | Graceful degradation, no fake data | Code: ✅ | ⛔ BLOCKED |
| PostgreSQL down | Log error, no false persistence claim | Code: ✅ | ⛔ BLOCKED |
| Circuit OPEN → HALF_OPEN | 1 test request after recovery_window | Code: ✅ | ⛔ BLOCKED |

---

## Failover Policy Verification

| Rule | Implemented | Tested |
|---|---|---|
| Failover considers provider capability, not just availability | ✅ CapabilityMatrix | 🟡 unit only |
| 429 does NOT open circuit | ✅ code | 🟡 unit only |
| Auth failure opens circuit | ✅ code | 🟡 unit only |
| No failover to provider that can't serve the data type | ✅ CapabilityMatrix check | 🟡 unit only |
| Stale data never labelled LIVE | ✅ Normaliser + FreshnessClassifier | 🟡 unit only |
| Provider failure not confused with market closed | ✅ ProviderMarketClosedError | 🟡 unit only |

---

## Note on Delta Exchange Failover

Because Delta Exchange is not implemented in DATA-SERVICE:
- When `ACTIVE_BROKER=delta` in AlphaForge, all crypto data bypasses DATA-SERVICE entirely
- There is no failover FROM Delta TO Binance within DATA-SERVICE
- DATA-SERVICE cannot serve as the authoritative crypto authority for Delta instruments
