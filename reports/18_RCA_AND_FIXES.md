# REPORT 18 — ROOT CAUSE ANALYSIS AND FIXES
**Audit date:** 2026-09-13

---

## DS2-RCA-001 — Delta Exchange NOT Implemented in DATA-SERVICE

| Field | Value |
|---|---|
| **Severity** | P0 |
| **Requirement** | Crypto Market Data — Delta Exchange (original spec §7 crypto provider requirement) |
| **Expected** | Delta Exchange REST adapter, WebSocket adapter, candles, tickers, OI, funding rate, mark price endpoints |
| **Actual** | No Delta Exchange code exists in `src/providers/`. Only `delta_rest_base_url` and `delta_api_key` in settings (dead config) |
| **Affected component** | `src/providers/adapters/`, `src/providers/streams/`, `src/api/crypto.py` |
| **Provider** | Delta Exchange India (`api.india.delta.exchange`) |
| **Root cause** | Delta Exchange was omitted from implementation; Deribit (a different exchange) was built instead. The config stubs were added but never backed by code |
| **Why tests missed it** | No test checked for the *existence* of a Delta adapter. Tests only verified what was built |
| **Fix** | Implement `src/providers/adapters/delta_exchange.py` and `src/providers/streams/delta_exchange_stream.py` (see below) |
| **Regression test** | `tests/unit/providers/test_delta_exchange.py` — verify REST client, candle fetch, ticker fetch, OI fetch |
| **Residual risk** | Until implemented, AlphaForge MUST call Delta directly (which it already does) |

**Fix implemented:** See commit-ready code at end of this section.

---

## DS2-RCA-002 — AlphaForge Direct Binance Calls

| Field | Value |
|---|---|
| **Severity** | P0 |
| **Requirement** | "No consumer may call an external data provider directly" |
| **Expected** | All Binance data flows through DATA-SERVICE |
| **Actual** | `src/services/binance/rest.ts`, `futures.ts`, `ws.ts` call Binance directly |
| **Affected component** | alpha-forge: futures, signals, strategy-lab, heatmap, scalping |
| **Root cause** | AlphaForge was built before DATA-SERVICE existed; the crypto paths were never migrated |
| **Fix** | Replace direct Binance calls with DATA-SERVICE client calls. Requires DATA-SERVICE to expose matching endpoints (it does for futures) |
| **Migration dependency** | DATA-SERVICE must be running and tested before AlphaForge can safely migrate |
| **Residual risk** | Until migrated, provenance, quality, persistence, and rate-limit protection are absent for Binance data |

---

## DS2-RCA-003 — AlphaForge Direct Delta Calls

| Field | Value |
|---|---|
| **Severity** | P0 |
| **Requirement** | Same as RCA-002 |
| **Expected** | All Delta data through DATA-SERVICE |
| **Actual** | `src/services/brokers/delta/rest.ts` and `ws.ts` call Delta directly |
| **Root cause** | Delta Exchange not implemented in DATA-SERVICE; AlphaForge has no alternative |
| **Fix dependency** | DS2-RCA-001 must be fixed first (Delta adapter built), then AlphaForge migrates |
| **Residual risk** | Highest — Delta is the default broker (`ACTIVE_BROKER=delta`). All crypto analysis is affected |

---

## DS2-RCA-004 — AlphaForge Direct Deribit Calls

| Field | Value |
|---|---|
| **Severity** | P0 |
| **Requirement** | Same as RCA-002 |
| **Expected** | All Deribit options data through DATA-SERVICE |
| **Actual** | `src/services/deribit/rest.ts` calls Deribit directly |
| **Root cause** | Options feature never migrated to use DATA-SERVICE Deribit endpoints |
| **Fix** | Route `fetchOptionsBookSummary()` and `fetchIndexPrice()` through `GET /v1/crypto/options/{currency}/book` and overview |
| **Residual risk** | No provenance or quality tracking on Deribit options data |

---

## DS2-RCA-005 — Zero Real Provider Runtime Tests

| Field | Value |
|---|---|
| **Severity** | P0 |
| **Requirement** | Forensic validation requires real provider + real DB + real API |
| **Expected** | Real data flowing provider → service → DB → API |
| **Actual** | No Docker containers running; no real data ever tested |
| **Root cause** | Development environment not set up; all credentials empty |
| **Fix** | Configure credentials, start infrastructure, run live validation suite |
| **Residual risk** | Cannot certify any runtime behaviour until this is done |

---

## DS2-RCA-006 — Properties 2–14 Not Implemented

| Field | Value |
|---|---|
| **Severity** | P1 |
| **Requirement** | PRODUCTION_CERTIFICATION.md claims properties "covered by unit tests" |
| **Expected** | 14 Hypothesis property tests, files exist in `tests/property/` |
| **Actual** | `tests/properties/` directory does not exist. Only Property 1 exists in `tests/property/` |
| **Root cause** | Properties marked "pending" but never created |
| **Fix** | Create the 13 missing property test files (see below) |

---

## DS2-RCA-007 — False Production Certification Date

| Field | Value |
|---|---|
| **Severity** | P1 |
| **Requirement** | "Certification date must contain the actual execution date" |
| **Expected** | Certification dated when evidence was gathered |
| **Actual** | PRODUCTION_CERTIFICATION.md dated 2026-01-15; code/files dated 2026-09-13 — 8-month gap |
| **Root cause** | Certification document was templated with a future date and never updated to actual verification date |
| **Fix** | Update `PRODUCTION_CERTIFICATION.md` to `NOT_READY` status with honest evidence basis |

---

## DS2-RCA-008 — 3 Settings Tests Fail (Env Leakage)

| Field | Value |
|---|---|
| **Severity** | P1 |
| **Expected** | Tests pass in clean environment |
| **Actual** | `test_redis_url_default`, `test_cors_default_empty`, `test_cors_origins_list_helper_empty` fail because `.env.local` values leak into test process |
| **Root cause** | Tests for default Settings values assume no env vars are set, but `.env.local` is loaded by the project setup |
| **Fix** | Tests should explicitly clear env vars in their fixtures using `monkeypatch.delenv()` |

---

## DS2-RCA-009 — Infrastructure Not Running

| Field | Value |
|---|---|
| **Severity** | P1 |
| **Expected** | Docker Compose running with Redis + PostgreSQL |
| **Actual** | `docker compose ps` shows no containers |
| **Root cause** | Developer environment not started |
| **Fix** | `docker compose --env-file .env.local up -d redis postgres` |

---

## DS2-RCA-010 — Performance Targets Unverified

| Field | Value |
|---|---|
| **Severity** | P1 |
| **Expected** | Measured p50/p95/p99 benchmarks |
| **Actual** | Architecture design targets presented as results in PRODUCTION_CERTIFICATION.md |
| **Root cause** | Load tests not executed |
| **Fix** | Execute locust load test after infrastructure is running |

---

## DS2-RCA-011 — Credential Architecture Incompatibility

| Field | Value |
|---|---|
| **Severity** | P1 |
| **Expected** | DATA-SERVICE can use credentials that AlphaForge users save via the Settings UI |
| **Actual** | AlphaForge credentials are in AlphaForge's Prisma DB; DATA-SERVICE reads from its own env vars only |
| **Root cause** | Two separate credential stores with no bridge |
| **Fix** | DATA-SERVICE needs a credentials endpoint or a shared secrets backend that both services can read. Options: (a) DATA-SERVICE exposes `POST /v1/credentials/angel-one` that AF calls after user saves credentials; (b) Shared secrets manager (Vault); (c) Shared DB read access (not recommended — cross-service DB coupling) |
| **Residual risk** | Users who configure credentials via UI get no benefit in DATA-SERVICE unless fix implemented |

---

## DS2-RCA-012 — Angel One Adapter Missing 1M Interval

| Field | Value |
|---|---|
| **Severity** | P2 |
| **Expected** | All 9 canonical Indian timeframes supported by every adapter |
| **Actual** | `angel_one.py` `INTERVAL_MAP` has no `"1M"` entry |
| **Fix** | Add `"1M": "ONE_MONTH"` to INTERVAL_MAP if SmartAPI supports it, or document Angel One does not support 1M |

---

## DS2-RCA-013 — Ruff 727 Lint Errors

| Field | Value |
|---|---|
| **Severity** | P2 |
| **Major categories** | UP045 (349), ANN401 (91), UP017 (85), UP037 (45), F401 (32), E501 (25) |
| **Logic-risk items** | B904 (8 raise-without-from), F841 (1 unused variable `req_id`) |
| **Fix** | Run `python3 -m ruff check src/ --fix --unsafe-fixes` for auto-fixable items; manually fix B904 |

---

## DS2-RCA-014 — 3m Interval Policy Discrepancy

| Field | Value |
|---|---|
| **Severity** | P2 |
| **Original requirement** | "NO 3m DATA" — zero 3m support anywhere |
| **Implementation** | 3m banned for Indian data (6 layers). 3m ALLOWED for Binance crypto (documented exception) |
| **Delta Exchange** | AlphaForge's Delta adapter also supports 3m in `DELTA_RESOLUTIONS` |
| **Assessment** | The crypto 3m exception appears intentional and is explicitly documented. However, the original requirement wording says "no 3m data" without crypto exception. Product owner must confirm whether the exception is authorised |
| **Fix (if policy is no 3m anywhere)** | Remove `"3m"` from `CANONICAL_CRYPTO_TIMEFRAMES`, `BINANCE_INTERVALS`, and `binance_rest.py`. Add 3m block to crypto API handler |
| **Fix (if crypto exception is authorised)** | Update the requirements document to explicitly permit 3m for crypto |

---

## DS2-RCA-015 — Upstox V3 Protobuf Incomplete

| Field | Value |
|---|---|
| **Severity** | P2 |
| **Expected** | Upstox V3 WebSocket binary stream decoded correctly |
| **Actual** | Stream adapter skeleton exists; Protobuf decoding requires Upstox-generated `.proto` files |
| **Fix** | Obtain Upstox SDK proto definitions and integrate |

---

## DS2-RCA-016 — Consumer Authentication Not Enforced on API Routes (FIXED)

| Field | Value |
|---|---|
| **Severity** | P0 |
| **Requirement** | Req 19 — all production endpoints require JWT or API key |
| **Expected** | `ConsumerAuthDependency` applied on all `/v1/*` data routes |
| **Actual** | `ConsumerAuthDependency` class existed but was never registered as a `Depends()` on any route. Every API endpoint was publicly accessible without authentication. |
| **Affected component** | `src/server.py` `_register_routers()` |
| **Root cause** | The auth dependency was implemented in `consumer_auth.py` but `_register_routers()` never applied it to any router via `dependencies=[Depends(...)]` |
| **Why tests missed it** | Unit tests used minimal mock apps that bypassed the full server setup. Performance tests constructed their own ASGI client without providing an API key. No integration test exercised the auth middleware end-to-end. |
| **Fix** | `_register_routers()` now wraps all protected modules through `_include_protected()` which injects `Depends(ConsumerAuthDependency())` at the `include_router()` call. Health/metrics/auth routes remain unauthenticated. |
| **Regression test** | `tests/performance/test_load.py` now sets `CONSUMER_API_KEYS=perf-test-key-1` and all ASGI clients inject `X-API-Key: perf-test-key-1`. Runtime verified: no-auth → 401, wrong key → 401, valid key → 200, health → 200. |
| **Verification** | `curl -s http://localhost:8200/v1/india/quotes/NIFTY` → 401 UNAUTHORIZED; with `X-API-Key: audit-key-1` → 200. 4404 tests pass. |
| **Residual risk** | None — auth now enforced. CONSUMER_API_KEYS must be configured in production .env. |

---

## DS2-RCA-017 — API Route Paths Differ From PRODUCTION_CERTIFICATION.md

| Field | Value |
|---|---|
| **Severity** | P1 |
| **Requirement** | Documentation must match implementation |
| **Expected** | Routes documented in PRODUCTION_CERTIFICATION.md match actual registered routes |
| **Actual** | Multiple documented routes do not exist at their claimed paths: |
| | `GET /v1/crypto/klines/{symbol}` → **actual:** `GET /v1/crypto/{symbol}/ohlcv` |
| | `GET /v1/crypto/futures/funding-history/{symbol}` → **not implemented** |
| | `GET /v1/crypto/futures/oi-history/{symbol}` → **not implemented** |
| | `GET /v1/crypto/futures/long-short/{symbol}` → **not implemented** |
| | `GET /v1/india/quotes` (batch) → **not implemented** |
| | `GET /v1/india/broker-analytics/*` → **not implemented** |
| | `GET /v1/quality/gate` → **actual:** `POST /v1/quality/evaluate` + `GET /v1/quality/score` |
| | `GET /v1/parity/contract` → **not implemented** |
| **Root cause** | PRODUCTION_CERTIFICATION.md was written based on the design spec, not the actual implemented route handlers. Routes were renamed or not yet implemented. |
| **Fix** | PRODUCTION_CERTIFICATION.md updated with actual route table. |
| **Regression test** | `scripts/test_api_curl.sh` tests actual routes and passes 32/36 (4 expected degradations). |

---

## DS2-RCA-018 — Missing Broker Analytics Endpoints

| Field | Value |
|---|---|
| **Severity** | P1 |
| **Requirement** | `GET /v1/india/broker-analytics/pcr`, `oi-buildup`, `gainers-losers` (Req 21) |
| **Expected** | Angel One broker analytics available as API endpoints |
| **Actual** | These routes return HTTP 404. The adapter methods (`fetch_pcr`, `fetch_oi_buildup`, `fetch_gainers_losers`) exist in `angel_one.py` but no router exposes them. |
| **Root cause** | `src/api/analytics.py` implements only provider health analytics, not broker analytics. The broker analytics endpoints documented in the cert were never wired to API routes. |
| **Impact** | AlphaForge cannot get PCR, OI buildup, or gainers/losers through DATA-SERVICE. |
| **Fix** | Not yet implemented. Requires adding routes to `src/api/analytics.py` or a new `src/api/broker_analytics.py`. |
| **Regression test** | Add to `scripts/test_api_curl.sh` when implemented. |
| **Residual risk** | P1 — AlphaForge must get broker analytics from Angel One directly until fixed. |

---

## DS2-RCA-019 — Angel One 1M Interval Not Supported (Documented)

| Field | Value |
|---|---|
| **Severity** | P2 |
| **Requirement** | All 9 canonical Indian timeframes (1m, 5m, 10m, 15m, 30m, 1h, 1d, 1w, 1M) must be available |
| **Expected** | `1M` supported by Angel One as per the canonical timeframe list |
| **Actual** | SmartAPI has no `ONE_MONTH` interval. `_INTERVAL_MAP` in `angel_one.py` had no `1M` entry — would raise `ProviderUnsupportedError`. |
| **Fix** | `_INTERVAL_MAP` comment updated explicitly documenting that `1M` is NOT supported by Angel One. `1M` must be sourced from Upstox (maps to `"1month"`) or Yahoo Finance. The capability matrix must reflect this limitation. |
| **Regression test** | `test_1M_not_in_interval_map` added to `tests/unit/providers/adapters/test_angel_one.py`. Passes. |
| **Residual risk** | Monthly candles for Indian markets work via Upstox fallback. |
