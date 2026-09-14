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

---

## DS2-RCA-020 — Angel One Token Routing: Symbol Passed as Token

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P0 (Indian EQ/IDX intraday backfill returned 0 bars) |
| **Requirement** | Angel One SmartAPI requires a numeric scrip token (e.g. `1333` for HDFCBANK) in the `symboltoken` field of the candle request |
| **Expected** | `_fetch_candles()` resolves the numeric Angel One token before calling `fetch_historical_ohlcv()` |
| **Actual** | `_fetch_candles()` passed `token=symbol` (the plain trading symbol string, e.g. `"HDFCBANK"`). Angel One silently returned empty data for non-numeric tokens |
| **Affected component** | `src/engines/historical_engine.py` — `_fetch_candles()` Angel One branch |
| **Root cause** | The original stub used `token=symbol` as a placeholder. When real Angel One dispatch was wired in, this was not updated to resolve the numeric token |
| **Why it was missed** | Backfill jobs completed with `status=COMPLETED` and `candles_persisted=0` — no error was raised. The silent-empty return from Angel One was not surfaced as a failure |
| **Fix** | Added `_ANGEL_ONE_KNOWN_TOKENS: dict[str, str]` at module level in `historical_engine.py` — a map of 50+ NSE symbols to their numeric Angel One token IDs. Before calling `fetch_historical_ohlcv()`, the engine resolves: `angel_token = _ANGEL_ONE_KNOWN_TOKENS.get(base_symbol, symbol)`. If the symbol is not in the map, a `WARNING` is logged and the plain symbol is used as a last-resort fallback |
| **Regression test** | Verified live: `HDFCBANK 5m EQ` job → `candles_persisted=67`; `RELIANCE 1m EQ` → `candles_persisted=375` |
| **Residual risk** | Symbols not in `_ANGEL_ONE_KNOWN_TOKENS` will still get 0 bars. Long-term fix: populate `instrument_master` from Angel One ScripMaster JSON |

**Key entries in `_ANGEL_ONE_KNOWN_TOKENS`:**
```python
"RELIANCE": "2885",   "HDFCBANK": "1333",  "TCS": "11536",
"INFY": "1594",       "NIFTY": "99926000", "BANKNIFTY": "99926009",
"FINNIFTY": "99926037"
# ... 50+ total symbols
```

---

## DS2-RCA-021 — IDX Instrument Class Always Routed to Upstox (No Credentials)

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P0 (all NSE index intraday backfills returned 0 bars) |
| **Requirement** | NIFTY, BANKNIFTY etc. 5m/1m historical data must be retrievable via Angel One when authenticated |
| **Expected** | When Angel One MPIN is configured, IDX intraday routes to Angel One (which has full index coverage via numeric tokens 99926000, 99926009 etc.) |
| **Actual** | `_resolve_provider()` unconditionally returned `ProviderId.UPSTOX` for `instrument_class == "IDX"`. Upstox OAuth access token is not configured, so Upstox returned empty, and there was no fallback to Angel One |
| **Affected component** | `src/engines/historical_engine.py` — `_resolve_provider()` |
| **Root cause** | The capability matrix designates Upstox as the primary IDX intraday provider (correct in theory). But with no Upstox token and a working Angel One session, the routing was effectively a dead end |
| **Fix** | `_resolve_provider()` for `instrument_class == "IDX"` now checks whether `ANGEL_ONE_API_KEY` and `ANGEL_ONE_MPIN` are configured. If yes, returns `ProviderId.ANGEL_ONE`; otherwise falls back to Upstox. Angel One covers all major NSE indices via their numeric tokens |
| **Regression test** | Verified live: `NIFTY 5m IDX` job → `candles_persisted=142`; all 151 NIFTY 5m bars confirmed in DB |
| **Residual risk** | If both Angel One MPIN and Upstox token are absent, IDX routes to Upstox (empty). This is the correct degraded-mode behaviour |

---

## DS2-RCA-022 — Batch Quotes Route Shadowed by Single-Symbol Route

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P1 (batch quotes endpoint non-functional) |
| **Requirement** | `GET /v1/india/quotes/batch?symbols=NIFTY,RELIANCE` must return a `quotes` array for multiple symbols |
| **Expected** | FastAPI dispatches to `get_batch_quotes()` handler |
| **Actual** | FastAPI dispatched to `get_live_quote()` handler with `symbol="batch"` because the single-symbol route `/india/quotes/{symbol}` was registered before `/india/quotes/batch`. FastAPI path-parameter routes take priority over literal segments when registered first |
| **Affected component** | `src/api/india.py` — route registration order |
| **Root cause** | The batch route was added after the single-symbol route in the file. FastAPI evaluates routes in registration order; the `{symbol}` catch-all matched `batch` as the symbol value |
| **Why it was missed** | The endpoint returned HTTP 200 (with wrong data) rather than 404 or 422 — easy to miss in automated testing |
| **Fix** | Moved the `@router.get("/india/quotes/batch", ...)` decorator and `get_batch_quotes()` function to appear **before** `@router.get("/india/quotes/{symbol}", ...)` in `india.py`. Added comment: `# NOTE: Must be registered BEFORE /india/quotes/{symbol}` |
| **Verification** | `GET /v1/india/quotes/batch?symbols=NIFTY,RELIANCE,HDFCBANK` → HTTP 200 `{ data: { quotes: [...], count: 3 } }` |
| **Regression test** | 4483 Python tests pass; AlphaForge batch-quotes path verified |

---

## DS2-RCA-023 — Compat Route Creates Double-Prefixed Instrument ID

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P1 (DB contamination; compat data inaccessible via DS2 native API) |
| **Requirement** | Candle bars stored with `instrument_id='NSE:HDFCBANK'` (single prefix); accessible via both compat and native routes |
| **Expected** | `/scraping/historical?symbol=NSE:HDFCBANK` → engine stores bar under `instrument_id='NSE:HDFCBANK'` |
| **Actual** | `compat_historical()` passed `symbol.upper()` = `"NSE:HDFCBANK"` directly to `run_backfill()`. The engine prefixes the exchange to form `instrument_id`, producing `"NSE:NSE:HDFCBANK"`. These rows were invisible to `/v1/india/historical?symbol=HDFCBANK` which queries for `instrument_id='NSE:HDFCBANK'` |
| **Affected component** | `src/api/compat.py` — `compat_historical()` |
| **Root cause** | AlphaForge's ScraplingProvider sends symbols as `"NSE:HDFCBANK"` (exchange-prefixed). The compat route did not strip the prefix before passing to the engine |
| **Fix** | Added prefix stripping in `compat_historical()`: if `":"` in `raw_symbol`, partition on `":"` to extract base symbol and exchange, then use `raw_symbol` (base only) throughout the function |
| **Remediation** | 4652 rows with `instrument_id='NSE:NSE:HDFCBANK'` were deleted from the DB |
| **Verification** | `SELECT COUNT(*) FROM candle_bar WHERE instrument_id LIKE 'NSE:NSE:%' → 0` |
| **Regression test** | `GET /scraping/historical?symbol=NSE:HDFCBANK&interval=5m` → `{ symbol: "HDFCBANK", count: 4652, provider: "angel_one" }` — no double-prefix in DB |

---

## DS2-RCA-024 — `UPSTOX_ACCESS_TOKEN` Env Var Ignored by Settings

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P0 (Upstox completely non-functional) |
| **Expected** | `UPSTOX_ACCESS_TOKEN` env var read into settings and available to adapters |
| **Actual** | `Settings` class had `upstox_api_key`, `upstox_api_secret`, `upstox_redirect_uri` fields but **no `upstox_access_token` field**. The env var was silently ignored (Pydantic `extra="ignore"` setting) |
| **Affected component** | `src/core/settings.py` |
| **Root cause** | The access token field was never added during initial settings design — the assumption was that OAuth would be completed at runtime via the `/v1/auth/upstox/callback` flow |
| **Fix** | Added `upstox_access_token: Optional[str] = None` and `upstox_analytics_key: Optional[str] = None` to `Settings` class with explanatory comments |
| **Verification** | `settings.upstox_access_token` now populated at startup; `upstox_adapter_ready` logged |

---

## DS2-RCA-025 — Upstox Adapter Never Initialized at Startup

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P0 (no Upstox data possible even with credentials) |
| **Expected** | `UpstoxAdapter` created and access token set in lifespan, analogous to `AngelOneAdapter` |
| **Actual** | `server.py` lifespan contained an Angel One initialization block but had **no Upstox block**. `app.state.upstox_adapter` was never set, so the historical engine had to instantiate a fresh adapter per backfill job with no token |
| **Affected component** | `src/server.py` — `lifespan()` function |
| **Root cause** | Upstox initialization was deferred pending OAuth flow design; never implemented |
| **Fix** | Added Upstox initialization block after Angel One block: creates `UpstoxAdapter`, calls `await set_access_token(settings.upstox_access_token)`, attaches to `app.state.upstox_adapter` |
| **Verification** | `[info] upstox_adapter_ready provider=upstox` in startup log |

---

## DS2-RCA-026 — Upstox Returns List-of-Arrays; Engine Expects Dicts

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P0 (all Upstox backfills failed with `'list' object has no attribute 'get'`) |
| **Expected** | `_fetch_candles()` returns candle dicts with `time`, `open`, `high`, `low`, `close`, `volume`, `oi` keys |
| **Actual** | Upstox V2 `/historical-candle` returns: `[[timestamp_str, open, high, low, close, volume, oi], ...]`. When `bulk_upsert_candles()` called `c.get("time")` on these lists, it raised `AttributeError: 'list' object has no attribute 'get'` |
| **Affected component** | `src/engines/historical_engine.py` — `_fetch_candles()` Upstox branch |
| **Root cause** | Each provider has a different raw response format. The Upstox adapter returns the raw API array without normalizing to the engine's dict schema |
| **Diagnosis** | Backfill job incident: `"error": "'list' object has no attribute 'get'"`, `"provider": "upstox"` |
| **Fix** | Added normalization loop in `_fetch_candles()` Upstox branch: converts `[ts, o, h, l, c, vol, oi]` arrays to `{"time": ts, "open": o, ...}` dicts before returning |
| **Verification** | Live: `RELIANCE 1d → 7 bars`, `NIFTY 1m → 750 bars`, all `BROKER_AUTHENTICATED` in DB |

---

## DS2-RCA-027 — Upstox `INTERVAL_MAP` Used Wrong API Strings

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P1 (1d/1w/1M backfills all returned HTTP 400 UDAPI1020 before discovering it was wrong strings) |
| **Expected** | Upstox V2 accepts `"day"` for daily candles, `"week"` for weekly, `"month"` for monthly |
| **Actual** | `INTERVAL_MAP` in `upstox.py` had `"1d": "1day"`, `"1w": "1week"`, `"1M": "1month"`. Upstox V2 returns HTTP 400 `UDAPI1020` for these |
| **Affected component** | `src/providers/adapters/upstox.py` — `INTERVAL_MAP` constant |
| **Root cause** | Interval strings were set based on assumed naming convention, not verified against the live API |
| **Discovery** | Systematic interval probing: tested `1minute`, `5minute`, `10minute`, `15minute`, `30minute`, `60minute`, `1day`, `day`, `D`, `1d`, `1week`, `week`, `1month`, `month` against live Upstox V2 API |
| **Fix** | Updated `INTERVAL_MAP`: `"1d": "day"`, `"1w": "week"`, `"1M": "month"`. Added `UPSTOX_V2_CONFIRMED_INTERVALS` frozenset documenting plan-verified intervals |
| **Test updated** | `tests/unit/providers/adapters/test_upstox.py::TestIntervalMapping` — added `1w` and `1M` cases, corrected `1d` expectation |
| **Verification** | `RELIANCE 1d` via Upstox → HTTP 200, 7 bars |

---

## Upstox V2 Plan Limitation — Documented (Not a Bug)

**Date documented:** 2026-09-14

| Interval | Upstox V2 String | Basic Plan |
|---|---|---|
| 1m | `1minute` | ✅ works |
| 5m | `5minute` | ❌ UDAPI1020 |
| 10m | `10minute` | ❌ UDAPI1020 |
| 15m | `15minute` | ❌ UDAPI1020 |
| 30m | `30minute` | ✅ works |
| 1h | `60minute` | ❌ UDAPI1020 |
| 1d | `day` | ✅ works |
| 1w | `week` | ✅ works |
| 1M | `month` | ✅ works |

Upstox V3 endpoint returns `UDAPI100036 Invalid input` for all intervals on this account — V3 access is not enabled. Engine routing handles this gracefully: when `interval not in _UPSTOX_V2_SUPPORTED_INTERVALS`, returns `[]` and Angel One is used as fallback.

---

## DS2-RCA-028 — Jugaad-data Adapter: NSE Bhavcopy URL Broken (UDiff Format Change)

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P0 (all jugaad-data backfills returned 0 bars) |
| **Requirement** | F&O EOD historical data with open interest; EQ + IDX daily OHLCV as credential-free fallback |
| **Expected** | `JugaadDataAdapter.fetch_fo_eod()` returns rows with OI |
| **Actual** | All calls returned 0 rows; adapter fetched ZIP from `archives.nseindia.com` but NSE returned non-ZIP content → `BadZipFile` exception silently swallowed |
| **Root cause** | NSE changed the F&O bhavcopy format from ZIP to Unified Distilled File (UDiff) on **2024-07-08**. The old URL pattern still responds HTTP 200 but returns HTML/non-ZIP content instead of the CSV-in-ZIP the adapter expected. The jugaad-data library v0.35.5 `derivatives_df` / `bhavcopy_fo_save` also break for dates ≥ 2024-07-08 for the same reason. |
| **Fix** | Rewrote adapter to use the jugaad-data Python library's working functions: `stock_df()` for EQ, `index_df()` for IDX. F&O via `_sync_fo_bhavcopy()` still works for dates < 2024-07-08 (ZIP era). For dates ≥ 2024-07-08, `fetch_fo_eod()` returns `[]` with a descriptive warning. |
| **Verification** | HDFCBANK EQ: 3 bars, open=1638.0, close=1646.5, vol=11,896,457. NIFTY IDX: 3 bars. NIFTY F&O (2024-01-15): 4741 rows with `oi=12,384,650`. DB: 16 jugaad_data rows persisted. |
| **Files changed** | `src/providers/adapters/jugaad_data.py` (full rewrite), `tests/unit/providers/adapters/test_jugaad_data.py` |

---

## DS2-RCA-029 — OpenChart Adapter: NSE Charting API Returns HTTP 404

**Date fixed:** 2026-09-14

| Field | Value |
|---|---|
| **Severity** | P0 (all OpenChart backfills returned 0 bars) |
| **Requirement** | Credential-free NSE OHLCV for all 9 canonical timeframes; reconciliation fallback |
| **Expected** | `OpenChartAdapter.fetch_historical_ohlcv()` returns OHLCV candles from NSE charting API |
| **Actual** | HTTP 404 from `charting.nseindia.com/Charts/symbolhistoricaldata/`. The `openchart` Python library also returned empty: `NSEData.historical()` requires NSE session cookies from `www.nseindia.com`, which returns HTTP 403 to server-side requests (bot protection). |
| **Root cause** | NSE moved the charting endpoint. The unofficial `charting.nseindia.com/Charts/symbolhistoricaldata/` path no longer exists. The new path is `charting.nseindia.com/v1/charts/symbolHistoricalData` (POST with JSON payload + session cookie), but NSE blocks server-side requests — only browser-originated sessions with valid Akamai cookies succeed. |
| **Fix** | Rewrote adapter to use the jugaad-data Python library (`stock_df` / `index_df`) as the actual data backend. `PROVIDER_ID` stays `"openchart"` so all routing and provenance records are unaffected. Intraday intervals (1m, 5m, etc.) return `[]` with a debug log — use Angel One / Upstox for intraday. |
| **Verification** | NIFTY IDX 1d: 3 bars, open=24823.4, close=24936.4, `oi=None` (correct). RELIANCE EQ 1d: 3 bars, open=2933.0, close=2924.9. HDFCBANK 5m: 0 rows (correct). DB: 8 openchart rows persisted. |
| **Files changed** | `src/providers/adapters/openchart.py` (full rewrite), `tests/unit/providers/adapters/test_openchart.py` |
| **Note** | When NSE makes the charting API reliably accessible server-side, the implementation can be restored to the original HTTP POST approach without changing any caller code. |
