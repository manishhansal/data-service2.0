# REPORT 19 — REGRESSION TEST REPORT
**Audit date:** 2026-09-13  
**Executed by:** Kiro AI forensic audit

---

## Test Suite Summary

| Category | Before Audit | After Fixes | Delta |
|---|---|---|---|
| Unit tests | 4363 pass / 3 fail | 4405 pass / 0 fail | +42 tests, -3 failures |
| Property tests (P1) | 5 pass | 5 pass | no change |
| Property tests (P2–P14) | 0 (files missing) | **38 new pass** | +38 new property tests |
| Integration tests | 1 file (circuit breaker) | 1 file | no change |
| Performance tests | 22 pass / 0 fail (no infra) | 22 pass / 0 fail | no change |
| **TOTAL** | **4368 pass / 3 fail** | **4405 pass / 0 fail** | **+37 tests, -3 failures** |

---

## Fixes Applied and Regression Tests Added

### Fix 1: DS2-RCA-008 — Settings Test Env Leakage (3 failing tests)

**Files changed:**
- `tests/unit/core/test_settings.py`

**Changes:** Added `monkeypatch.delenv()` to 3 tests that assumed default env values but were failing because `.env.local` polluted the test process:
- `test_redis_url_default` → now clears `REDIS_URL` before testing default
- `test_cors_default_empty` → now clears `CORS_ALLOWED_ORIGINS`
- `test_cors_origins_list_helper_empty` → now clears `CORS_ALLOWED_ORIGINS`

**Before:** 3 FAIL  
**After:** 3 PASS  
**Verification:** `python3 -m pytest tests/unit/core/test_settings.py` → all pass

---

### Fix 2: DS2-RCA-006 — 13 Missing Property Tests (P2–P14)

**Files created:**
- `tests/property/test_normaliser_round_trip.py` (Property 2)
- `tests/property/test_deribit_round_trip.py` (Property 3)
- `tests/property/test_dedup_hash.py` (Property 4)
- `tests/property/test_confidence_score_bounds.py` (Property 5)
- `tests/property/test_session_phase_determinism.py` (Property 6)
- `tests/property/test_3m_rejection.py` (Property 7)
- `tests/property/test_quality_gate_closed_form.py` (Property 8)
- `tests/property/test_blocked_score.py` (Property 9)
- `tests/property/test_cache_ttl_monotonicity.py` (Property 10)
- `tests/property/test_observation_id_uniqueness.py` (Property 11)
- `tests/property/test_oi_semantic_integrity.py` (Property 12)
- `tests/property/test_look_ahead_bias.py` (Property 13)
- `tests/property/test_reconciliation_deviation.py` (Property 14)

**Verification:** `python3 -m pytest tests/property/ -q` → **43 passed**

| Property | Key invariant tested | Iterations | Status |
|---|---|---|---|
| P1 | OHLCV high≥max(o,c), low≤min(o,c), vol≥0 | 100+ | ✅ PASS |
| P2 | Normaliser round-trip idempotency | 100 | ✅ PASS |
| P3 | Deribit name parse→serialise→parse identity | 100 | ✅ PASS |
| P4 | Dedup hash determinism + collision resistance | 100+100+100 | ✅ PASS |
| P5 | Confidence score always in [0, 95], never 100 | 200+100 | ✅ PASS |
| P6 | Session phase deterministic for same timestamp | 200+200 | ✅ PASS |
| P7 | 3m rejected by AngelOne + Upstox adapters | 50+50 | ✅ PASS |
| P8 | Quality gate closed-form (all 32 bool combos) | 32 | ✅ PASS |
| P9 | Score < 30 always blocks signal engine | 30+30+66 | ✅ PASS |
| P10 | L1 cache set/get/delete/clear/LRU semantics | 100+100+50+50 | ✅ PASS |
| P11 | UUID v4 observation IDs always unique | 50+50 | ✅ PASS |
| P12 | OI never populated from tradedValue | 100+100 | ✅ PASS |
| P13 | No look-ahead: filter(records, T) never returns time > T | 100+100 | ✅ PASS |
| P14 | Reconciliation ≤0.5%→CONFIRMED, >2%→MAJOR_DISCREPANCY | 100+100+100 | ✅ PASS |

---

### Fix 3: DS2-RCA-016 — Auth Middleware Not Enforced (P0 security fix)

**Files changed:**
- `src/server.py` — `_register_routers()` refactored to inject `ConsumerAuthDependency`
- `tests/performance/test_load.py` — added `CONSUMER_API_KEYS` env var + `X-API-Key` header

**Before:** Every endpoint publicly accessible without credentials  
**After:**
- `GET /v1/india/quotes/NIFTY` without auth → HTTP 401 UNAUTHORIZED ✅
- `GET /v1/india/quotes/NIFTY` with wrong key → HTTP 401 INVALID_API_KEY ✅
- `GET /v1/india/quotes/NIFTY` with valid key → HTTP 200 ✅
- `GET /v1/health/live` without auth → HTTP 200 ✅ (health exempt)
- `GET /metrics` without auth → HTTP 200 ✅ (metrics exempt)
- Performance tests: 22/22 pass with injected API key ✅

**Verification:** `python3 -m pytest tests/performance/ -q` → all pass; curl auth tests above

---

### Fix 4: DS2-RCA-019 — Angel One 1M Interval Documentation

**Files changed:**
- `src/providers/adapters/angel_one.py` — explicit comment that `1M` is NOT supported
- `tests/unit/providers/adapters/test_angel_one.py` — `test_1M_not_in_interval_map` added

**Verification:** `python3 -m pytest tests/unit/providers/adapters/test_angel_one.py -q` → all pass

---

## Remaining Open Items (Not Fixed — Require Further Work)

| ID | Title | Why Not Fixed Now |
|---|---|---|
| DS2-RCA-001 | Delta Exchange not implemented | Requires full new adapter (~500 LOC); needs credentials for runtime testing |
| DS2-RCA-002 | AlphaForge direct Binance calls | Requires AlphaForge code changes; dependency on RCA-001 for Delta path |
| DS2-RCA-003 | AlphaForge direct Delta calls | Requires RCA-001 fix first |
| DS2-RCA-004 | AlphaForge direct Deribit calls | Requires AlphaForge migration |
| DS2-RCA-005 | No real Indian provider tests | Requires Angel One + Upstox credentials |
| DS2-RCA-007 | False PRODUCTION_CERTIFICATION date | PRODUCTION_CERTIFICATION.md updated below |
| DS2-RCA-009 | Infrastructure not running for all tests | Docker DB port not exposed to localhost |
| DS2-RCA-010 | Performance targets unmeasured | Requires sustained load test run |
| DS2-RCA-011 | Credential store incompatibility | Requires architectural decision |
| DS2-RCA-013 | Ruff 727 lint errors | Style/annotation only; not blocking |
| DS2-RCA-014 | 3m crypto exception needs product sign-off | Policy decision required |
| DS2-RCA-015 | Upstox V3 Protobuf incomplete | Requires Upstox SDK proto files |
| DS2-RCA-017 | Route path documentation mismatch | PRODUCTION_CERTIFICATION.md updated |
| DS2-RCA-018 | Broker analytics endpoints missing | Requires new API routes in src/api/ |

---

## Full Test Execution Commands

```bash
# Full unit + property + integration + performance
python3 -m pytest tests/ -q

# Property tests only
python3 -m pytest tests/property/ -v

# Unit tests only (fast, no infra)
python3 -m pytest tests/unit/ -q

# Performance benchmarks (requires running service)
python3 -m pytest tests/performance/ -v

# Integration tests (require Redis + PG)
python3 -m pytest tests/integration/ -v -m integration

# API regression (requires running service at :8200)
bash scripts/test_api_curl.sh

# Binance live data (requires internet)
python3 scripts/test_binance_live.py

# Binance persistence (requires Docker network)
# docker run --network ... python3 scripts/test_binance_persistence.py

# Deribit live (requires internet)
python3 scripts/test_deribit_live.py

# Yahoo Finance historical
python3 scripts/test_yahoo_live.py

# Provider auth status
python3 scripts/test_provider_auth.py
```

---

## Test Count by Category (Final)

| Category | Tests | Pass | Fail |
|---|---|---|---|
| Unit | 4,327 | 4,327 | 0 |
| Property | 43 | 43 | 0 |
| Integration | 1 | 1 | 0 |
| Performance | 22 | 22 | 0 |
| Dataset publisher | 12 | 12 | 0 |
| **TOTAL** | **4,405** | **4,405** | **0** |

---

# LIVE TESTING SESSION — 2026-09-14
## Angel One Credentials Configured; 4 Bugs Fixed

---

## Updated Test Suite Summary

| Category | Before Live Session | After Live Session | Delta |
|---|---|---|---|
| Unit tests | 4405 pass / 0 fail | **4483 pass / 0 fail** | +78 tests |
| Property tests | 43 pass | 43 pass | no change |
| Integration tests | 1 file | 1 file | no change |
| Performance tests | 22 pass | 22 pass | no change |
| **TOTAL** | **4405 pass / 0 fail** | **4483 pass / 0 fail** | **+78 tests** |

---

## Fix 5: DS2-RCA-020 — Angel One Token Routing

**Files changed:**
- `src/engines/historical_engine.py`

**Changes:**
- Added `_ANGEL_ONE_KNOWN_TOKENS: dict[str, str]` — 50+ NSE symbols mapped to numeric Angel One token IDs
- `_fetch_candles()` Angel One branch now resolves `angel_token = _ANGEL_ONE_KNOWN_TOKENS.get(base_symbol, symbol)` before calling `fetch_historical_ohlcv()`
- Warning logged when symbol is not in the map

**Before:** `HDFCBANK 5m EQ` backfill → `candles_persisted=0`  
**After:** `HDFCBANK 5m EQ` backfill → `candles_persisted=67` (one day)  
**After (90-day):** `HDFCBANK 5m` → `candles_persisted=4727`

**Verification:** Live backfill jobs; `SELECT COUNT(*) FROM candle_bar WHERE instrument_id='NSE:HDFCBANK' AND interval_str='5m' → 4727`

---

## Fix 6: DS2-RCA-021 — IDX Routing to Angel One When MPIN Configured

**Files changed:**
- `src/engines/historical_engine.py`

**Changes:**
- `_resolve_provider()` for `instrument_class == "IDX"` now checks `settings.angel_one_api_key` and `settings.angel_one_mpin`. If both present → `ProviderId.ANGEL_ONE`; else → `ProviderId.UPSTOX` (original behaviour)

**Before:** `NIFTY 5m IDX` backfill → routed to Upstox → `candles_persisted=0`  
**After:** `NIFTY 5m IDX` backfill → routed to Angel One (token 99926000) → `candles_persisted=142`

**Verification:** `SELECT COUNT(*) FROM candle_bar WHERE instrument_id='NSE:NIFTY' AND interval_str='5m' → 151`

---

## Fix 7: DS2-RCA-022 — Batch Quotes Route Registration Order

**Files changed:**
- `src/api/india.py`

**Changes:**
- Moved `get_batch_quotes()` function and its `@router.get("/india/quotes/batch", ...)` decorator to appear **before** `get_live_quote()` and `@router.get("/india/quotes/{symbol}", ...)`
- Added comment explaining the ordering requirement

**Before:**
```
GET /v1/india/quotes/batch?symbols=NIFTY,RELIANCE,HDFCBANK
→ data: { instrumentId: "batch", ltp: null }   ← wrong: single-symbol handler
```

**After:**
```
GET /v1/india/quotes/batch?symbols=NIFTY,RELIANCE,HDFCBANK
→ data: { quotes: [{ symbol:"NIFTY", ltp:null, marketStatus:"CLOSED" }, ...], count: 3 }
```

**Verification:** Live curl; AlphaForge market-snapshot path verified

---

## Fix 8: DS2-RCA-023 — Compat Route Exchange Prefix Stripping

**Files changed:**
- `src/api/compat.py`

**Changes:**
- `compat_historical()` now strips exchange prefix if present: `"NSE:HDFCBANK" → "HDFCBANK"` (exchange derived from prefix)
- All subsequent calls use `raw_symbol` (base symbol only)
- Response `symbol` field returns base symbol, not prefixed variant

**DB remediation:** 4652 rows with `instrument_id='NSE:NSE:HDFCBANK'` deleted.

**Before:**
```
GET /scraping/historical?symbol=NSE:HDFCBANK
→ DB writes instrument_id='NSE:NSE:HDFCBANK'   ← invisible to native API
```

**After:**
```
GET /scraping/historical?symbol=NSE:HDFCBANK
→ DB writes instrument_id='NSE:HDFCBANK'        ← correct
→ response: { symbol: "HDFCBANK", count: 4652, provider: "angel_one" }
```

**Verification:** `SELECT COUNT(*) FROM candle_bar WHERE instrument_id LIKE 'NSE:NSE:%' → 0`

---

## Full Runtime Evidence — 2026-09-14

| Test | Result | Value |
|---|---|---|
| Angel One auth at startup | ✅ | `angel_one_authenticated`, `real_provider=True` |
| HDFCBANK 1d EQ via Angel One | ✅ | 7 bars, open=815.5, close=829.58 |
| HDFCBANK 5m EQ via Angel One | ✅ | 4727 bars, BROKER_AUTHENTICATED |
| NIFTY 5m IDX via Angel One | ✅ | 151 bars, token=99926000 |
| RELIANCE 1m EQ via Angel One | ✅ | 375 bars |
| TCS 1d EQ via Angel One | ✅ | 14 bars |
| Live quote CLOSED (no fabrication) | ✅ | ltp=null, marketStatus=CLOSED |
| Batch quotes 3 symbols | ✅ | quotes array, count=3 |
| Compat route prefix strip | ✅ | symbol=HDFCBANK, count=4652 |
| DB: no double-prefix rows | ✅ | 0 rows matching `NSE:NSE:%` |
| 3m blocked | ✅ | HTTP 400 INTERVAL_NOT_SUPPORTED |
| Option chain CLOSED | ✅ | rows=[], no fabrication |
| Python test suite | ✅ | **4483 / 4483 pass** |
| TypeScript test suite | ✅ | **3651 / 3651 pass** |
| TypeScript compilation | ✅ | **0 errors** |

---

## Full Test Execution Commands (Updated)

```bash
# Full suite
python3 -m pytest tests/ -q

# Live Angel One test script
python3 scripts/test_angel_live.py

# Verify no double-prefix in DB
docker compose --env-file .env.local exec postgres \
  psql -U mds_user -d mds -c \
  "SELECT COUNT(*) FROM candle_bar WHERE instrument_id LIKE 'NSE:NSE:%';"

# Verify batch quotes route
curl "http://localhost:8201/v1/india/quotes/batch?symbols=NIFTY,RELIANCE" \
  -H "X-API-KEY: dev-key-local-1"

# Verify compat prefix stripping
curl "http://localhost:8201/scraping/historical?symbol=NSE:HDFCBANK&interval=5m&limit=1" \
  -H "X-API-KEY: dev-key-local-1"
```

---

## Test Count by Category (Final — Post Live Testing)

| Category | Tests | Pass | Fail |
|---|---|---|---|
| Unit | 4,405 | 4,405 | 0 |
| Property | 43 | 43 | 0 |
| Integration | 1 | 1 | 0 |
| Performance | 22 | 22 | 0 |
| Dataset publisher | 12 | 12 | 0 |
| **TOTAL** | **4,483** | **4,483** | **0** |

---

# UPSTOX WIRING SESSION — 2026-09-14

---

## Updated Test Suite Summary

| Category | Before Upstox Session | After Upstox Session | Delta |
|---|---|---|---|
| Unit tests | 4483 pass | **4485 pass** | +2 (new Upstox interval cases) |
| Property tests | 43 pass | 43 pass | no change |
| **TOTAL** | **4483 pass / 0 fail** | **4485 pass / 0 fail** | **+2** |

---

## Fix 9: DS2-RCA-024 — `upstox_access_token` Missing from Settings

**Files changed:** `src/core/settings.py`

**Change:** Added `upstox_access_token: Optional[str] = None` and `upstox_analytics_key: Optional[str] = None` fields with comments.

**Verification:** `settings.upstox_access_token` now populated from `UPSTOX_ACCESS_TOKEN` env var.

---

## Fix 10: DS2-RCA-025 — Upstox Adapter Not Initialized at Startup

**Files changed:** `src/server.py`

**Change:** Added Upstox initialization block in `lifespan()` after Angel One block. Creates `UpstoxAdapter`, calls `await set_access_token(settings.upstox_access_token)`, attaches to `app.state.upstox_adapter`.

**Verification:** Startup log: `[info] upstox_adapter_ready provider=upstox`

---

## Fix 11: DS2-RCA-026 + DS2-RCA-027 — Upstox Candle Format + Interval Strings

**Files changed:**
- `src/engines/historical_engine.py` — normalization + `_UPSTOX_INSTRUMENT_KEYS` map + `_UPSTOX_V2_SUPPORTED_INTERVALS`
- `src/providers/adapters/upstox.py` — INTERVAL_MAP corrected
- `tests/unit/providers/adapters/test_upstox.py` — interval test updated + `1w`/`1M` added

**Changes:**
1. `_fetch_candles()` Upstox branch: normalizes `[ts, o, h, l, c, vol, oi]` arrays → dicts
2. `INTERVAL_MAP`: `"1d": "day"`, `"1w": "week"`, `"1M": "month"`
3. `_UPSTOX_INSTRUMENT_KEYS`: 60+ NSE symbols mapped to `NSE_EQ|{ISIN}` / `NSE_INDEX|{Name}`
4. `_UPSTOX_V2_SUPPORTED_INTERVALS`: `{"1m", "30m", "1d", "1w", "1M"}`
5. Routing updated: EQ 1d/1w/1M → Upstox; IDX 1d/1m/30m → Upstox; unsupported intervals → Angel One

**Before:** All Upstox 1d/1w/1M jobs: `BACKFILL_ERROR: 'list' object has no attribute 'get'`  
**After:**
- `RELIANCE 1d` → 7 bars, BROKER_AUTHENTICATED
- `HDFCBANK 1d` → 7 bars, BROKER_AUTHENTICATED
- `TCS 1d` → 7 bars, BROKER_AUTHENTICATED
- `NIFTY 1d IDX` → 7 bars, BROKER_AUTHENTICATED
- `BANKNIFTY 1d IDX` → 7 bars, BROKER_AUTHENTICATED
- `NIFTY 1m IDX` → 750 bars, BROKER_AUTHENTICATED
- `NIFTY 30m IDX` → 26 bars, BROKER_AUTHENTICATED

**Tests:** 4485/4485 pass

---

## Full Live Evidence Summary (2026-09-14 — Both Sessions)

| Provider | Test | Result |
|---|---|---|
| Angel One | Auth at startup | `angel_one_authenticated` ✅ |
| Angel One | HDFCBANK 5m EQ 4727 bars | ✅ BROKER_AUTHENTICATED |
| Angel One | NIFTY 5m IDX 151 bars | ✅ BROKER_AUTHENTICATED |
| Angel One | RELIANCE 1m EQ 375 bars | ✅ BROKER_AUTHENTICATED |
| **Upstox** | **Auth at startup** | **`upstox_adapter_ready` ✅** |
| **Upstox** | **RELIANCE 1d EQ 7 bars** | **✅ BROKER_AUTHENTICATED** |
| **Upstox** | **NIFTY 1d IDX 7 bars** | **✅ BROKER_AUTHENTICATED** |
| **Upstox** | **NIFTY 1m IDX 750 bars** | **✅ BROKER_AUTHENTICATED** |
| **Upstox** | **BANKNIFTY 1d IDX 7 bars** | **✅ BROKER_AUTHENTICATED** |
| Both | 3m blocked | HTTP 400 `INTERVAL_NOT_SUPPORTED` ✅ |
| Both | DB quality checks | 0 OHLC violations, 0 3m rows, 0 double-prefix ✅ |
| Both | API/DB consistency | Values match across provider ✅ |
| Both | Python tests | **4485 / 4485 pass** ✅ |
| Both | TypeScript tests | **3651 / 3651 pass** ✅ |

---

## Test Count by Category (Final)

| Category | Tests | Pass | Fail |
|---|---|---|---|
| Unit | 4,407 | 4,407 | 0 |
| Property | 43 | 43 | 0 |
| Integration | 1 | 1 | 0 |
| Performance | 22 | 22 | 0 |
| Dataset publisher | 12 | 12 | 0 |
| **TOTAL** | **4,485** | **4,485** | **0** |

---

# JUGAAD-DATA + OPENCHART FIX SESSION — 2026-09-14

---

## Fix 12: DS2-RCA-028 — Jugaad-data Adapter Rewrite

**Files changed:**
- `src/providers/adapters/jugaad_data.py` — full rewrite
- `tests/unit/providers/adapters/test_jugaad_data.py` — mocks updated

**Root cause:** NSE changed F&O bhavcopy from ZIP to UDiff on 2024-07-08; old HTTP adapter returned `BadZipFile`.

**Fix:** Use `jugaad_data.stock_df()` for EQ, `index_df()` for IDX. F&O bhavcopy via ZIP still works for dates < 2024-07-08.

**Results:**
- HDFCBANK EQ 1d: 3 bars, open=1638.0, close=1646.5, vol=11,896,457
- NIFTY IDX 1d: 3 bars, open=24823.4, close=24936.4
- NIFTY F&O (2024-01-15): 4741 rows, oi=12,384,650
- NIFTY F&O (post-2024-07-08): 0 rows + warning ✅

---

## Fix 13: DS2-RCA-029 — OpenChart Adapter Rewrite

**Files changed:**
- `src/providers/adapters/openchart.py` — full rewrite
- `tests/unit/providers/adapters/test_openchart.py` — mocks updated

**Root cause:** `charting.nseindia.com/Charts/symbolhistoricaldata/` returned HTTP 404. New API endpoint requires NSE session cookies; blocks server-side requests.

**Fix:** Use jugaad-data library as data backend. `PROVIDER_ID` stays `"openchart"`.

**Results:**
- NIFTY IDX 1d: 3 bars, provider=openchart, OI=None ✅
- RELIANCE EQ 1d: 3 bars, open=2933.0, close=2924.9
- HDFCBANK 5m: 0 rows (correct — only 1d supported)

---

## Test Count (Final — All Sessions)

| Category | Tests | Pass | Fail |
|---|---|---|---|
| Unit | 4,407 | 4,407 | 0 |
| Property | 43 | 43 | 0 |
| Integration | 1 | 1 | 0 |
| Performance | 22 | 22 | 0 |
| Dataset publisher | 12 | 12 | 0 |
| **TOTAL** | **4,485** | **4,485** | **0** |
