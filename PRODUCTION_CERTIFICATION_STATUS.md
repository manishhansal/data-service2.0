# Production Certification Status
## data-service2.0 + AlphaForge

---

## Current Commit

**Branch:** fix/bugs
**Commit:** 692bf3f
**Message:** fix: Phase B-K remediation (gateway, protobuf, OAuth, streaming, candle builder, rate limits, OI reconciliation)

## Last Runtime Verification

**Angel One live calls:** 2026-09-17T04:12–04:20 UTC (09:42–09:50 IST, market OPEN)
**Failure drills:** 2026-09-17T07:59 UTC (Drills 3 and 5 — local execution)
**Phase M code gate checks:** 2026-09-17T08:02 UTC (local, no DB)

## Test Suite

| Suite | Count | Status |
|-------|-------|--------|
| unit/ + property/ + mocks/ | 4,564 | **PASS** |
| Failures | 0 | — |
| Warnings | 7 | Deprecation only (jugaad_data, datetime.utcnow) |

---

## Certification Gates

| Gate | Status | Evidence |
|------|--------|---------|
| Angel One REST (EQ historical, live quote, Greeks) | **PASS** | Live call 2026-09-17: RELIANCE 1244.1, HDFCBANK 715.8, NIFTY 23242.45; 328 option contracts |
| Angel One F&O token resolution | **FIXED** | BUG-012: instrument_provider_mapping query added; code verified |
| Angel One historical OI | **BLOCKED** | getOIData returns "Invalid Bad Request" — plan restriction M495775 |
| Angel One SmartStream live | **NOT VERIFIED** | Requires market hours + live feedToken |
| Upstox REST (historical, LTP, full quote, option chain) | **PASS** | Live call 2026-09-17: RELIANCE 1243.9, HDFCBANK 716.35, NIFTY 23240.7; NIFTY/BANKNIFTY/FINNIFTY chains |
| Upstox OAuth multi-worker safety | **FIXED** | Redis distributed lock + token sharing implemented (BUG-015) |
| Upstox OAuth access token | **BLOCKED** | Expired 2026-09-14; analytics token valid 2027-09-03 |
| Upstox protobuf (pb2) | **FIXED** | pb2 generated; FeedResponse works locally (BUG-013) |
| Upstox WebSocket live binary decode | **NOT VERIFIED** | Blocked by expired OAuth token |
| ProviderGateway enforcement | **FIXED** | fetch() is real dispatch; no NotImplementedError stub (BUG-014) |
| Yahoo Finance F&O exclusion | **VERIFIED** | Drill 3 PASS: Yahoo not in F&O fallback chain |
| Hierarchical rate limiting (50/s + 500/min + 2000/30min) | **FIXED** | HierarchicalRateLimiter implemented (BUG-016) |
| Live load test (429 behavior under burst) | **NOT VERIFIED** | Live provider credentials required |
| Circuit breaker OPEN → HALF_OPEN → CLOSED | **VERIFIED** | Drill 5 PASS 2026-09-17 |
| Provider failover (live provider to provider) | **BLOCKED** | Live credentials required |
| market_tick persistence from live streams | **FIXED** | TickPersister + streaming wiring (BUG-018/019) |
| market_tick row count | **NOT VERIFIED** | 0 rows — awaiting live streaming session |
| CandleBuilder (tick → OHLCV) | **FIXED** | 24 unit tests PASS (BUG-017) |
| Live candle pipeline (stream → equity_candle) | **NOT VERIFIED** | Requires live WS session |
| available_at_ms (point-in-time) | **FIXED** | DB migration + ORM model + CandleBuilder enforcement (BUG-020) |
| Look-ahead validation (candle_time_ms <= available_at_ms) | **VERIFIED** | CandleBuilder.validate_point_in_time() unit test PASS |
| OI reconciliation (cross-provider) | **FIXED** | reconcile_oi() unit tests PASS; NULL never corrupted to 0 (BUG-021) |
| OI reconciliation live | **BLOCKED** | Angel One plan restriction; Upstox analytics only |
| Canonical DB persistence (equity_candle) | **PASS** | 5,460,561 rows; 0 OHLC violations; 0 three-m rows |
| Canonical DB persistence (futures_candle) | **PARTIAL** | 20 rows; F&O pilot not yet run |
| Canonical DB persistence (options_candle) | **NOT VERIFIED** | 0 rows |
| market_quote persistence | **PASS** | 3 rows with depth; RELIANCE ltp=1240.6 (live call 2026-09-17) |
| 3m interval blocked (all canonical tables) | **PASS** | DB CHECK constraint enforced; CandleBuilder ValueError; adapter guard |
| candle_bar (no new Indian market writes) | **PASS** | Only Binance+Delta write to candle_bar; all Indian market paths use canonical tables |
| Exchange calendar (NSE/NFO) | **PASS** | 3,654 rows (2024–2028) |
| Ingestion checkpoints (resume on restart) | **PASS** | Redis key per (symbol, exchange, interval) |
| F&O 30-day pilot | **BLOCKED** | Angel credentials + instrument sync required |
| Survivorship bias check | **NOT VERIFIED** | Requires pilot execution |
| Look-ahead check (DB) | **FIXED** | available_at_ms migration applied; SQL validation query in pilot script |
| AlphaForge market-data architecture | **PASS** | README + code: all market data through data-service2.0 client.ts; no direct provider calls |
| AlphaForge services/india/ broker adapters | **PASS** | README confirms: order placement/portfolio only, not market data |
| AlphaForge NSE elimination guard | **PASS** | 12 guard tests in nse-elimination.test.ts |
| AlphaForge E2E (data-service → signal → UI) | **NOT VERIFIED** | Requires running stacks |
| ML dataset temporal integrity | **NOT VERIFIED** | Requires certified data layer first |
| Strategy profitability | **NOT CERTIFIED** | Previous result: -0.18%/trade, PF 0.90 (historical baseline only) |

---

## Certification Tier Summary

### TIER 1 — REST_DATA_CERTIFIED
**Status: PASS (with blockers)**

Angel One EQ REST: PASS | Upstox EQ REST: PASS
Canonical DB: PASS | Provenance: PASS | 3m blocked: PASS
Blocked: Angel OI (plan restriction) | Upstox OAuth (expired)

### TIER 2 — REALTIME_STREAM_CERTIFIED
**Status: NOT VERIFIED**

Implementation complete (CandleBuilder, TickPersister, stream adapter wiring).
Blocked: Upstox OAuth expired; no live streaming session run.
market_tick = 0 rows.

### TIER 3 — FNO_DATA_CERTIFIED
**Status: BLOCKED**

Token resolution fix applied (BUG-012).
Blocked by: Angel One credentials not configured; instrument_provider_mapping sync needed; OI plan restriction.
Pilot script ready: `scripts/run_fno_pilot.py`

### TIER 4 — PROVIDER_RESILIENCE_CERTIFIED
**Status: PARTIAL**

Circuit breaker recovery: VERIFIED (Drill 5).
F&O Yahoo exclusion: VERIFIED (Drill 3).
Live failover drills: BLOCKED (credentials).
Rate limiting: IMPLEMENTED (all 3 windows). Live load test: NOT_EXECUTED.

### TIER 5 — ALPHAFORGE_MARKET_DATA_E2E_CERTIFIED
**Status: NOT VERIFIED**

Architecture is correct (verified via README + code audit). Live E2E path requires running stacks.

### TIER 6 — ML_DATASET_CERTIFIED
**Status: NOT VERIFIED**

Prerequisite: Tier 1–3 must be certified first. Data layer not yet fully certified.

### TIER 7 — STRATEGY_PROFITABILITY_CERTIFIED
**Status: NOT CERTIFIED**

Previous result (historical baseline only): 321 trades, 25.68% WR, -0.18%/trade, PF 0.90.
This is NOT a current profitability certification. Fresh OOS evaluation required after Tier 6.

---

## Remaining Blockers

| # | Issue | Root Cause | Impact | Action Required |
|---|-------|-----------|--------|----------------|
| 1 | Upstox OAuth access token expired | Token expired 2026-09-14 | Upstox WS, intraday blocked | Complete OAuth callback flow at `/v1/auth/upstox/callback` |
| 2 | Angel One historical OI unavailable | Plan restriction account M495775 | OI always NULL for historical F&O | Contact Angel One support to enable getOIData API |
| 3 | Angel One SmartStream not live-validated | No live market session available | TIER 2 incomplete | Run during NSE trading hours with configured credentials |
| 4 | F&O pilot not executed | Credentials + DB sync required | TIER 3 blocked | Configure Angel One env vars + run instrument sync + run pilot script |
| 5 | Live provider failover drills | Live provider credentials required | TIER 4 partial | Configure credentials, run `scripts/run_failure_drills.py` |
| 6 | Live load test | Live provider + load harness | Rate limit certification | Configure credentials, run locustfile.py |
| 7 | AlphaForge E2E | Running stacks required | TIER 5 incomplete | Start both data-service2.0 and AlphaForge stacks |

---

## Documentation Status

| Document | Status |
|---------|--------|
| FINAL_PROVIDER_RUNTIME_CERTIFICATION.md | Updated in-place (Part 8 appended) |
| UPSTOX_PROTOBUF_CERTIFICATION.md | Updated in-place (status: IMPLEMENTED) |
| PROVIDER_RATE_LIMIT_REPORT.md | Updated in-place (3-window implementation) |
| PROVIDER_FAILURE_DRILL_REPORT.md | Updated in-place (Drill 3 + 5 PASS evidence) |
| FNO_HISTORICAL_PILOT_REPORT.md | Updated in-place (Phase B-K fixes appended) |
| PRODUCTION_CERTIFICATION_STATUS.md | Created (this document) |

No new reports created. All updates made in-place to existing canonical documents.
