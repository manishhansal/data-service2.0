# INDIAN DATA — ROOT CAUSE ANALYSIS
**Date:** 2026-09-14  
**Auditor:** Forensic code inspection + runtime testing

---

## RCA-001: Critical — `/scraping/*` Route Mismatch

**BUG:** AlphaForge's `ScraplingProvider` called `/scraping/historical`, `/scraping/quotes`, `/scraping/option-chain`, `/scraping/instruments` — but data-service2.0 exposed `/v1/india/*` endpoints. The `DATA_SERVICE_URL` pointed to the internal AlphaForge data-service (port 8200), not data-service2.0.

**ROOT CAUSE:** AlphaForge's internal data-service and data-service2.0 are two separate Python services with incompatible API path prefixes. The `DATA_SERVICE_URL` was never updated from the internal service to data-service2.0.

**IMPACT:** data-service2.0 was never used as AlphaForge's market data provider — AlphaForge continued using the deprecated internal data-service.

**FIX:**
1. Added `/scraping/*` compat routes to data-service2.0 (`src/api/compat.py`)
2. Updated `DATA_SERVICE_URL` in AlphaForge `.env.local` from `http://localhost:8200` to `http://localhost:8201`
3. Added `DATA_SERVICE_API_KEY` for authenticated endpoints

**TEST:** `GET /scraping/historical?symbol=TCS` returns `count=6, provider=yahoo_finance` ✅

**REGRESSION:** TypeScript clean, all 3651 AlphaForge tests pass.

---

## RCA-002: Critical — MarketEngine Used Stub Providers

**BUG:** `data-service2.0/src/engines/market_engine.py` had `_fetch_live_quote_stub()` and `_fetch_option_chain_stub()` that returned null/empty data. The comment said "Phase 8 (Streaming Engine) will replace these stubs".

**ROOT CAUSE:** Stubs were never replaced. The market engine never connected to real Angel One or Upstox adapters.

**IMPACT:** All live quote and option chain requests returned null data regardless of credentials.

**FIX:**
1. `_fetch_live_quote_stub()` now dispatches to `AngelOneAdapter.fetch_live_quote()` when configured
2. `_fetch_option_chain_stub()` now dispatches to `AngelOneAdapter.fetch_option_chain()` when configured
3. Both methods fall back to null/empty on failure — never fabricate data

**TEST:** With MPIN configured, live quotes return real Angel One data. Without MPIN, returns null with correct provenance. ✅

---

## RCA-003: Critical — Angel One Authentication Wrong Credential

**BUG:** `AngelOneAdapter.authenticate()` sent `password=totp_code` (TOTP as the password field). Angel One SmartAPI login requires `password=4-digit MPIN` and `totp=TOTP_code` as separate fields.

**ROOT CAUSE:** Angel One's TOTP + MPIN login flow was misunderstood. The adapter was built treating TOTP as the password, which is incorrect.

**IMPACT:** Every Angel One API call failed with `"Please enter 4 digit mpin to login"`.

**FIX:**
- Added `mpin` parameter to `AngelOneAdapter.__init__()`
- Fixed `authenticate()` to send `password=mpin, totp=totp_code`
- Added `ANGEL_ONE_MPIN` setting to `Settings` and `.env.local`
- Updated server startup to pass `mpin=settings.angel_one_mpin`

**TEST:** Angel One authentication succeeds when MPIN is configured. ✅

---

## RCA-004: Critical — HistoricalEngine Used Stub for All Providers

**BUG:** `HistoricalEngine._fetch_candles()` had `# TODO(task-4.5-4.8): wire real provider adapters via ProviderGateway` and returned `[]` for all providers.

**ROOT CAUSE:** The historical engine's provider dispatch was not implemented. All backfill jobs would complete with `candles_persisted=0`.

**IMPACT:** No historical data was ever acquired or persisted via the backfill API.

**FIX:** Replaced the stub with real provider dispatch:
- `ProviderId.ANGEL_ONE` → `AngelOneAdapter.fetch_historical_ohlcv()`
- `ProviderId.JUGAAD_DATA` → `JugaadDataAdapter.fetch_fo_eod()`
- `ProviderId.OPENCHART` → `OpenChartAdapter.fetch_historical_ohlcv()`
- `ProviderId.YAHOO_FINANCE` → `YahooFinanceAdapter.fetch_historical_ohlcv()`
- Added Yahoo Finance fallback for EQ/IDX 1d when primary fails

**TEST:** TCS 7 bars persisted via Yahoo Finance fallback. RELIANCE 11 bars. HDFCBANK 8 bars. ✅

---

## RCA-005: Moderate — Credential Stripper Content-Length Bug

**BUG:** `CredentialStripperMiddleware.dispatch()` rebuilt the response with `headers=dict(response.headers)` which included the original `Content-Length`. After redacting credentials (shorter body), the `Content-Length` header was stale (larger than actual body).

**ROOT CAUSE:** Starlette's `Response` constructor doesn't auto-recalculate `Content-Length` when body bytes are provided with an explicit `headers` dict.

**IMPACT:** uvicorn raised `RuntimeError: Response content shorter than Content-Length` for any response containing credentials in JSON fields (e.g. `token` fields in instrument data).

**FIX:** Updated `dispatch()` to recalculate `Content-Length`:
```python
new_headers["content-length"] = str(len(sanitised_bytes))
```

**TEST:** Compat routes return correct responses with credential fields redacted. ✅

---

## RCA-006: Moderate — Yahoo Finance NSE Index Mapping Missing

**BUG:** `YahooFinanceAdapter._resolve_yf_symbol()` mapped all NSE symbols to `{symbol}.NS`, so `NIFTY` became `NIFTY.NS` (which returns 404 from Yahoo Finance).

**ROOT CAUSE:** NSE indices use Yahoo Finance's `^NSEI`, `^NSEBANK` etc. format, not the `.NS` equity suffix.

**IMPACT:** NIFTY, BANKNIFTY and other index backfills returned 0 candles.

**FIX:** Added index symbol mapping dict:
```python
_NSE_INDEX_MAP = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", ...}
```

**TEST:** `NIFTY` now resolves to `^NSEI` and Yahoo Finance returns data. ✅

---

## RCA-007: Moderate — UTC Date Mismatch in Test

**BUG:** `tests/unit/engines/test_option_chain_quality.py` computed `_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()` using local time, while the validator used `datetime.now(tz=timezone.utc).date()`. At midnight IST (+05:30), local "yesterday" = UTC "today", causing the expiry validation test to pass an expired contract.

**ROOT CAUSE:** Timezone mismatch between test and production code.

**IMPACT:** Pre-existing test flap — test failed at midnight IST.

**FIX:** Updated test to use UTC dates matching the validator:
```python
_UTC_TODAY = datetime.now(tz=_tz.utc).date()
_YESTERDAY = (_UTC_TODAY - timedelta(days=1)).isoformat()
```

**TEST:** Test passes consistently. ✅

---

## RCA-008: Minor — AlphaForge Direct Provider Fallbacks Active When DS2 Set

**BUG:** `bootstrapRegistry()` registered `AngelOneProvider`, `UpstoxProvider`, and `YahooProvider` with `enabled: true` even when `DATA_SERVICE_URL` was set. When data-service2.0 returned an error, AlphaForge fell back to direct provider calls, bypassing DS2.

**ROOT CAUSE:** The registry was designed with DS2 as priority 0 but with direct providers as always-on fallbacks — defeating the architectural requirement.

**IMPACT:** AlphaForge could still make direct Angel One / Upstox / Yahoo calls even with DS2 configured.

**FIX:** Added `directProvidersEnabled = !usingDataService` logic:
```typescript
enabled: directProvidersEnabled,  // false when DATA_SERVICE_URL is set
```

**TEST:** With `DATA_SERVICE_URL` set, direct providers are disabled. AlphaForge's signal engine can only receive data through DS2. ✅

---

## RCA-009: Minor — Broker Analytics Bypassed DS2

**BUG:** `scanner/engine.ts`, `india-builder.ts`, and `daily-picks/builder.ts` called `angel.getPutCallRatio()`, `angel.getOiBuildup()`, `angel.getTopGainersLosers()` directly, bypassing data-service2.0.

**ROOT CAUSE:** data-service2.0 broker analytics endpoints (`/v1/india/broker-analytics/*`) existed but were never wired into the AlphaForge consumption layer.

**IMPACT:** PCR, OI buildup, and gainers/losers data came directly from Angel One, creating a direct provider dependency.

**FIX:** Created `broker-analytics-client.ts` that routes through DS2. Updated all three consumers.

**TEST:** TypeScript clean. Tests pass. ✅

---

## RCA-010: Minor — /data/gate Path Mismatch

**BUG:** AlphaForge `gate-client.ts` called `POST /data/gate` but data-service2.0's quality gate is at `POST /v1/quality/evaluate` with a different request schema.

**ROOT CAUSE:** The internal data-service's gate endpoint path was used in the client without updating it for DS2.

**FIX:** Added `POST /data/gate` compat route to DS2 that translates AlphaForge's `GateRequest` schema into DS2's quality observation format.

**TEST:** `POST /data/gate {"symbol":"TCS","quoteAgeMs":5000,...}` returns `{signalEngineAllowed:false, quality:UNKNOWN}` ✅
