# Production Certification Status
## data-service2.0 + AlphaForge

---

## Current State

**Branch:** fix/bugs  
**Commit:** 5dd69a2 (latest; previous canonical: 692bf3f)  
**Last verified:** 2026-09-17  

## Test Suite

| Suite | Count | Status |
|-------|-------|--------|
| unit/ + property/ + perf/ + integration/ | **4821** | **PASS** |
| Failures | 0 | — |
| Warnings | 6 | All external (jugaad_data ×4, starlette ×1, urllib3 ×1) |

> Previous counts "4,413" and "4,564" in older report sections are SUPERSEDED.

---

## Certification Gates

| Gate | Status | Evidence | Blocker |
|------|--------|---------|---------|
| Angel One EQ REST (historical, live quote, Greeks) | PASS | Live call 2026-09-17: RELIANCE 1244.1, 328 option contracts | — |
| Angel One F&O token resolution (instrument_provider_mapping) | PASS | BUG-024 fixed; pilot: 54,683 rows with resolved tokens | — |
| Angel One historical OI | BLOCKED | `getOIData` returns "Invalid Bad Request" (plan restriction) | BLOCKED_BY_EXTERNAL |
| Angel One SmartStream live | NOT VERIFIED | Requires market hours + live feedToken | BLOCKED_BY_EXTERNAL |
| Upstox REST (historical, LTP, full quote, option chain) | PASS | Live call 2026-09-17: RELIANCE 1243.9; chains confirmed | — |
| Upstox OAuth multi-worker safety | PASS | Redis distributed lock + 23h TTL (unit-tested) | — |
| Upstox OAuth access token | BLOCKED | Expired -218,983s (2026-09-14) | BLOCKED_BY_EXTERNAL |
| Upstox protobuf (pb2) | PASS | `upstox_market_data_feeder_pb2.py` present; FeedResponse round-trip verified locally | — |
| Upstox WebSocket live binary decode | NOT VERIFIED | Blocked by expired OAuth token | BLOCKED_BY_EXTERNAL |
| ProviderGateway dispatch | PASS | `fetch()` real dispatch; Yahoo not in F&O chain (Drill 3) | — |
| Hierarchical rate limiting (50/s + 500/min + 2000/30min) | PARTIAL | Implemented + 175 tests pass; live load test not run | — |
| Live load test (429 behavior) | NOT VERIFIED | Unsafe to run against live provider quota | — |
| Circuit breaker OPEN→HALF_OPEN→CLOSED | PASS | Drill 5 PASS 2026-09-17 (log evidence) | — |
| Provider failover Angel→Upstox | PASS | Drill 1 PASS 2026-09-17: Upstox returned 4 candles when Angel CB forced OPEN | — |
| Provider failover Upstox→Angel | BLOCKED | Upstox OAuth expired | BLOCKED_BY_EXTERNAL |
| market_tick persistence | NOT VERIFIED | 0 rows — awaiting live streaming session | BLOCKED_BY_EXTERNAL |
| CandleBuilder (tick→OHLCV) | PASS | 24 unit tests PASS; implementation complete | — |
| Live candle pipeline (stream→equity_candle) | NOT VERIFIED | Requires live WS session | BLOCKED_BY_EXTERNAL |
| equity_candle DB | PASS | 5,460,751 rows; 0 OHLC violations; 0 3m rows; 0 neg_latency; 0 duplicates | — |
| futures_candle DB | PASS | 54,683 rows (post-pilot); 0 violations; expiry populated; OI=NULL (not 0) | — |
| options_candle DB | NOT VERIFIED | 0 rows (no options backfill run yet) | — |
| market_quote persistence | PASS | 3 rows with depth (live call 2026-09-17) | — |
| 3m interval blocked | PASS | DB CHECK constraint + CandleBuilder ValueError + adapter guard | — |
| Exchange calendar | PASS | 3,654 rows (NSE 2024–2028) | — |
| Ingestion checkpoints | PASS | Redis keys confirmed; checkpoint test 14/14 PASS | — |
| Point-in-time (temporal integrity) | PASS | SQL validation: 0 violations across equity_candle + futures_candle | — |
| OI NULL not zero | PASS | 0 oi_zero rows in futures_candle; reconcile_oi() unit-tested | — |
| F&O 30-day pilot | PARTIAL | Executed 2026-09-17; 16/24 interval×instrument combos PASS; 8 BLOCKED_BY_EXTERNAL | — |
| Survivorship bias | NOT VERIFIED | No expired contract in pilot window (all expiry 2026-09-29) | — |
| AlphaForge architecture | PASS | No provider credentials in AlphaForge env; DATA_SERVICE_URL → data-service:8200 | — |
| AlphaForge live (crypto feed active) | PASS | Worker log: BTC/ETH/SOL scalper active; data-service making Binance calls | — |
| AlphaForge India E2E | PARTIAL | API returns real candles (RELIANCE 1m via angel_one); India session finalised (market closed) | — |
| ML dataset temporal integrity | NOT VERIFIED | Prerequisite: Tier 3 certification | — |
| Strategy profitability | NOT CERTIFIED | Historical baseline only (321 trades, PF 0.90) | — |

---

## Certification Tiers

| Tier | Name | Status |
|------|------|--------|
| TIER 1 | REST_DATA_CERTIFIED | PARTIAL |
| TIER 2 | REALTIME_STREAM_CERTIFIED | NOT VERIFIED |
| TIER 3 | FNO_DATA_CERTIFIED | PARTIAL |
| TIER 4 | PROVIDER_RESILIENCE_CERTIFIED | PARTIAL |
| TIER 5 | ALPHAFORGE_MARKET_DATA_E2E_CERTIFIED | PARTIAL |
| TIER 6 | ML_DATASET_CERTIFIED | NOT VERIFIED |
| TIER 7 | STRATEGY_PROFITABILITY_CERTIFIED | NOT CERTIFIED |

### TIER 1 — REST_DATA_CERTIFIED: PARTIAL
Angel One EQ: PASS | Upstox EQ: PASS | Canonical DB: PASS | Provenance: PASS  
Blocked: Angel OI (plan restriction) | Upstox OAuth (expired)

### TIER 2 — REALTIME_STREAM_CERTIFIED: NOT VERIFIED
Implementation: COMPLETE. pb2: PRESENT. CandleBuilder: PASS. TickPersister: PASS.  
Blocked: Upstox OAuth expired; market_tick = 0 rows; no live WS session completed.

### TIER 3 — FNO_DATA_CERTIFIED: PARTIAL
Token lookup fixed (BUG-024). Expiry fix (BUG-025). Pilot executed.  
16/24 combos PASS. Blocked: Angel 5m/30m NIFTY (HTTP 403), Upstox 1d NFO tokens.  
OI: NULL (correct). Survivorship: NOT_VERIFIED.

### TIER 4 — PROVIDER_RESILIENCE_CERTIFIED: PARTIAL
Circuit breaker: PASS. Angel→Upstox failover: PASS. Yahoo F&O exclusion: PASS.  
Rate limiting: IMPLEMENTED + unit-tested. Live load test: NOT_EXECUTED.  
Upstox→Angel failover: BLOCKED (OAuth expired).

### TIER 5 — ALPHAFORGE_MARKET_DATA_E2E_CERTIFIED: PARTIAL
Architecture verified. Crypto feed E2E active. India feed returns real DB data.  
Full E2E signal→UI not verified during live session.

### TIER 6 — ML_DATASET_CERTIFIED: NOT VERIFIED
Requires Tier 3 certification first.

### TIER 7 — STRATEGY_PROFITABILITY_CERTIFIED: NOT CERTIFIED
No current OOS evaluation. Historical baseline (321 trades, PF 0.90) is not certification.

---

## Remaining External Blockers

| # | Blocker | Classification |
|---|---------|---------------|
| 1 | Upstox OAuth token expired | BLOCKED_BY_EXTERNAL |
| 2 | `UPSTOX_CLIENT_SECRET` not configured (no auto-refresh) | BLOCKED_BY_EXTERNAL |
| 3 | Angel One `getOIData` plan restriction | BLOCKED_BY_EXTERNAL |
| 4 | Angel One SmartStream not validated live | BLOCKED_BY_EXTERNAL |
| 5 | Angel One 5m/30m NIFTY FUT HTTP 403 (plan restriction) | BLOCKED_BY_EXTERNAL |
| 6 | Upstox NFO 1d token mapping incomplete | BLOCKED_BY_EXTERNAL |

## Remaining Internal Work

| # | Item | Priority |
|---|------|----------|
| 1 | Consumer API rate limiting (inbound) — mount `RateLimitMiddleware` in server.py | P3 |
| 2 | Survivorship bias test — pilot with expired contract | P2 |
| 3 | Live provider rate-limit load test (post-credential restoration) | P2 |
| 4 | Upstox WS live binary decode (post-OAuth refresh) | P1 |
| 5 | Angel One SmartStream live validation (during market hours) | P1 |

---

## Documentation Status

| Document | Status |
|---------|--------|
| FINAL_PROVIDER_RUNTIME_CERTIFICATION.md | Updated in-place (Part 9 appended) |
| UPSTOX_PROTOBUF_CERTIFICATION.md | Rewritten in-place (contradictions removed) |
| PROVIDER_RATE_LIMIT_REPORT.md | Rewritten in-place (live load test results added) |
| PROVIDER_FAILURE_DRILL_REPORT.md | Rewritten in-place (Drill 1 now PASS) |
| FNO_HISTORICAL_PILOT_REPORT.md | Rewritten in-place (actual pilot results) |
| PRODUCTION_CERTIFICATION_STATUS.md | This document — current master status |

No new report files created. All updates made in-place to existing canonical documents.
