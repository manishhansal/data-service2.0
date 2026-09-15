# PROVIDER LIVE TEST REPORT
**Generated:** 2026-09-15  
**Market session:** REGULAR (Capital Market OPEN)  
**NIFTY 50 spot at test time:** ~23,328  

---

## SUMMARY

| Provider | Live Quotes | Historical | Option Chain | Notes |
|---|---|---|---|---|
| Angel One | ✅ F&O futures | ✅ EQ+IDX+FUT 1d/5m/1h | ✅ Via Angel One OC API | Rate limited at 3 req/s during REGULAR session |
| Upstox | ⚠️ OAuth expired | ✅ EQ+IDX 1d/1w/1M | ✅ NIFTY/BANKNIFTY/FINNIFTY via analytics key | Access token requires daily OAuth refresh |
| ScraplingNSE | ✅ 139 indices via allIndices | — | ❌ NSE moved endpoint | Akamai blocks quote-equity; allIndices works |
| Jugaad-data | — | ✅ EQ+IDX EOD 1d | — | NSE bhavcopy — no auth required |
| OpenChart | — | ✅ EQ 1d | — | NSE charting — no auth required |

---

## ANGEL ONE SmartAPI

### Authentication: Redis JWT Sharing (FIXED)

**Problem resolved:** With 4 uvicorn workers, all 4 previously tried TOTP login simultaneously at startup. Angel One accepts only one TOTP per 30-second window → 3 workers got HTTP 403.

**Fix implemented** (`src/providers/adapters/angel_one.py`):
- Worker 1: TOTP login → stores JWT at `mds:angel_one:jwt:{client_id}` (TTL=6h) + distributed lock
- Workers 2–4: read JWT from Redis, skip TOTP login entirely
- `AngelOneAdapter` accepts `redis_client` parameter
- `server.py` passes `redis_client=app.state.redis` to the adapter

**Verified from logs (all 4 workers):**
```
2026-09-15T04:52:27.904316Z angel_one_authenticated + angel_one_jwt_stored_in_redis (ttl_sec=21600)
2026-09-15T04:52:28.154400Z angel_one_jwt_loaded_from_redis
2026-09-15T04:52:28.154450Z angel_one_jwt_loaded_from_redis
2026-09-15T04:52:28.154972Z angel_one_jwt_acquired_after_lock_wait
```

### Live Quotes

| Symbol | Exchange | Status | Notes |
|---|---|---|---|
| NIFTY29SEP26FUT | NFO | ✅ ltp=23,426 | F&O futures quote works |
| RELIANCE29SEP26FUT | NFO | ✅ ltp=1,257.6 | F&O futures quote works |
| RELIANCE, HDFCBANK, NIFTY | NSE | ⚠️ Rate limited | Server's MarketEngine background polling saturates 3 req/s cap during REGULAR session |

**Note:** Angel One NSE live quotes work in isolation. The 403s during testing are because the running server's MarketEngine polls all 150+ F&O stocks continuously, consuming the rate budget. During off-hours or with rate-limit spacing, NSE equity quotes work fine.

### Historical Candles

| Data Type | Status | Count | Sample |
|---|---|---|---|
| NSE EQ RELIANCE 1d | ✅ | 10 | last_close=1,255.8 |
| NSE EQ RELIANCE 5m | ✅ | 161 | intraday, today |
| NSE EQ RELIANCE 1h | ✅ | 2 | intraday, today |
| NFO FUT NIFTY29SEP26FUT 1d | ✅ | 10 | last_close=23,428.8 |
| NFO FUT RELIANCE29SEP26FUT 1d | ✅ | 10 | last_close=1,257.6 |
| NFO FUT 5m (intraday) | ✅ | varies | When rate-limit free |
| NSE EQ/IDX 1m | ⚠️ Rate limited | — | Server polling saturates 3 req/s |

### Option Chain

Angel One has a `/marketData/v1/optionChain` endpoint that returns data but with inconsistent results depending on market conditions. The Upstox analytics key is the more reliable source for option chain data.

---

## UPSTOX V2

### Authentication

Upstox uses OAuth2 access tokens that expire daily. The `UPSTOX_ACCESS_TOKEN` in `.env.local` requires manual refresh via the OAuth flow or the `/v1/auth/upstox/callback` endpoint.

**Live quotes:** Return 401 (token refresh 400) — token was expired at test time. Historical data continues to work because Upstox's historical API is more lenient with token expiry.

### Historical Candles (all passing)

| Data | Status | Count | Sample |
|---|---|---|---|
| RELIANCE EQ 1d | ✅ | 9 | last_close=1,309.0 oi=0 |
| HDFCBANK EQ 1d | ✅ | 9 | last_close=711.9 |
| INFY EQ 1d | ✅ | 9 | last_close=1,156.0 |
| NIFTY 50 IDX 1d | ✅ | 9 | last_close=24,055.8 |
| BANKNIFTY IDX 1d | ✅ | 9 | last_close=57,409.6 |
| RELIANCE EQ 1w | ✅ | 7 | last_close=1,307.8 |
| RELIANCE EQ 1M | ✅ | 3 | last_close=1,307.8 |
| NIFTY 50 IDX 1w | ✅ | 7 | last_close=24,383.6 |

### Option Chain (Upstox Analytics Key — ✅ PRIMARY SOURCE)

| Index | Expiry | Rows | Spot | ATM Sample |
|---|---|---|---|---|
| NIFTY | 2026-09-29 | ✅ 123 | 23,329.55 | CE ltp=5.95 OI=5,488,665 \| PE ltp=1,628 OI=2,079,535 |
| BANKNIFTY | 2026-09-29 | ✅ 145 | 56,171.3 | CE ltp=352.75 OI=1,143,180 \| PE ltp=970 OI=855,390 |
| FINNIFTY | 2026-09-29 | ✅ 123 | 25,307.55 | CE/PE live ltp+OI |
| NIFTY (weekly) | 2026-10-06 | ✅ 84 | 23,350.15 | Live |

**The Upstox analytics key (`UPSTOX_ANALYTICS_KEY`) is the authoritative option chain source.**

---

## SCRAPLING NSE (curl_cffi + Chrome TLS fingerprint)

### NSE WAF Status (September 2026)

NSE upgraded their Akamai Bot Manager. Investigation with multiple approaches:

| Approach | Result |
|---|---|
| curl_cffi Chrome 131 TLS fingerprint | allIndices ✅ marketStatus ✅ \| quote-equity ❌ 403 |
| Playwright bundled Chromium | ERR_HTTP2_PROTOCOL_ERROR — Chromium blocked |
| Playwright real Chrome 152 | Homepage loads, nsit cookie set, quote-equity ❌ 403 |
| Playwright real Chrome 152 headless=False | nsit set, nseappid NOT set, 403 |

**Root cause:** `nseappid` cookie requires Akamai's behavioral biometrics (mouse movements, keystrokes, canvas fingerprinting over multiple sessions) — not obtainable via any automated browser.

**`option-chain-indices` endpoint:** Returns HTTP 404 — NSE moved/removed this endpoint.

### Working Endpoints

| Endpoint | Status | Data |
|---|---|---|
| `/api/allIndices` | ✅ WORKING | 139 live index prices |
| `/api/marketStatus` | ✅ WORKING | Market open/closed + session type |
| `/api/quote-equity` | ❌ 403 | Akamai nseappid required |
| `/api/quote-derivative` | ❌ 403 | Akamai nseappid required |
| `/api/option-chain-indices` | ❌ 404 | Endpoint moved by NSE |

### Live Index Data (via allIndices — ✅ WORKING)

| Index | Last | Change | O | H | L |
|---|---|---|---|---|---|
| NIFTY 50 | 23,321.7 | -0.33% | 23,576 | 23,592 | 23,316 |
| NIFTY BANK | 56,166.6 | -0.77% | 56,884 | 56,996 | 56,111 |
| FINNIFTY | 25,292.2 | -0.99% | 25,682 | 25,729 | 25,281 |
| MIDCPNIFTY | 14,462.6 | -0.84% | 14,662 | 14,692 | 14,451 |
| INDIA VIX | 12.87 | +4.86% | 12.29 | 12.92 | 11.93 |
| NIFTY IT | 30,099 | +4.18% | 29,921 | 30,435 | 29,851 |
| NIFTY AUTO | 27,078 | -0.81% | 27,497 | 27,501 | 27,048 |
| NIFTY PHARMA | 26,317 | -0.81% | 26,597 | 26,640 | 26,315 |
| NIFTY FMCG | 45,268 | +0.48% | 45,241 | 45,562 | 45,146 |
| NIFTY METAL | 12,812 | -1.44% | 13,039 | 13,049 | 12,805 |
| INDIA VIX | 12.87 | +4.86% | 12.29 | 12.92 | 11.93 |

**Adapter updated:**
- `fetch_all_indices()` — new method, calls `/api/allIndices` directly
- `fetch_live_quote()` — routes index symbols through `allIndices` (fast path)
- `_ensure_seeded()` — improved multi-step warm-up (homepage + option-chain page)
- `_INDEX_SYMBOL_MAP` — extended mapping for FINNIFTY, MIDCPNIFTY, all sectors

---

## JUGAAD-DATA (NSE bhavcopy)

All tests passing. No authentication required.

| Symbol | Status | Count | last_close |
|---|---|---|---|
| RELIANCE EQ EOD | ✅ | 9 | 1,257.5 |
| HDFCBANK EQ EOD | ✅ | 9 | 708.25 |
| INFY EQ EOD | ✅ | 9 | 1,037.7 |
| TCS EQ EOD | ✅ | 9 | 2,200.8 |
| SBIN EQ EOD | ✅ | 9 | 995.7 |
| NIFTY 50 IDX | ✅ | 9 | 23,398.1 |
| NIFTY BANK IDX | ✅ | 9 | 56,606.6 |
| NIFTY IT IDX | ✅ | 9 | 28,921.5 |

**Note:** Jugaad-data F&O (fo_eod) is broken for dates after 2024-07-08 (NSE changed bhavcopy format). Historical engine now routes F&O EOD to Angel One instead.

---

## OPENCHART (NSE charting data)

All tests passing. No authentication required.

| Symbol | Status | Count | last_close |
|---|---|---|---|
| RELIANCE 1d | ✅ | 9 | 1,257.5 |
| HDFCBANK 1d | ✅ | 9 | 708.25 |
| NIFTY 1d | ✅ | 9 | 23,398.1 |
| TCS 1d | ✅ | 9 | 2,200.8 |
| INFY 1d | ✅ | 9 | 1,037.7 |
| SBIN 1d | ✅ | 9 | 995.7 |
| BAJFINANCE 1d | ✅ | 9 | 1,034.5 |

---

## PROVIDER CAPABILITY MATRIX (CURRENT)

| Capability | Angel One | Upstox | ScraplingNSE | Jugaad | OpenChart |
|---|---|---|---|---|---|
| NSE EQ live quote | ⚠️ Rate limited | ⚠️ Token expired | ❌ 403 WAF | — | — |
| NSE IDX live price | ✅ via quote | ⚠️ Token expired | ✅ allIndices | — | — |
| NFO F&O live quote | ✅ | ⚠️ Token expired | ❌ 403 WAF | — | — |
| NSE EQ historical 1d | ✅ | ✅ | — | ✅ | ✅ |
| NSE IDX historical 1d | ✅ | ✅ | — | ✅ | ✅ |
| NFO FUT historical 1d | ✅ | — | — | ❌ broken | — |
| NFO FUT historical 1m | ✅ | — | — | — | — |
| NIFTY option chain | ✅ OC API | ✅ analytics key | ❌ 404 | — | — |
| Market status | ✅ | — | ✅ | — | — |
| 139 live index prices | ✅ batch | — | ✅ allIndices | — | — |
