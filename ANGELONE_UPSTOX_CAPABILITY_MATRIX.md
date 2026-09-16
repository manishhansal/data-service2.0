# ANGEL ONE + UPSTOX CAPABILITY MATRIX
## data-service2.0 — AlphaForge Market Data Platform

**Version:** 2.0.0  
**Date:** 2026-09-16  
**Source:** Forensic audit of live provider documentation + codebase analysis

---

## LEGEND

| Status | Meaning |
|--------|---------|
| ✅ IMPLEMENTED | Code exists and is wired correctly |
| ⚠️ PARTIAL | Code exists but incomplete or not wired |
| ❌ MISSING | No implementation found |
| 🔴 BROKEN | Code exists but is functionally incorrect |
| DOCUMENTED | In official provider docs |
| VERIFIED | Confirmed working with live credentials |
| NOT_VERIFIED | Cannot confirm without live credentials |
| DEPRECATED | Provider has deprecated this endpoint |
| DISCONTINUED | Provider has stopped this service |

---

## ANGEL ONE SmartAPI

### Authentication

| Capability | Endpoint | Rate Limit | Implementation | Notes |
|-----------|----------|-----------|----------------|-------|
| TOTP + JWT Login | POST /rest/auth/.../loginByPassword | ~1 per 6h | ✅ IMPLEMENTED | Redis multi-worker lock |
| Token Refresh | POST /rest/auth/.../generateTokens | Low | ✅ IMPLEMENTED | rotate_token() |
| Get Profile (feedToken) | GET /rest/secure/.../getProfile | 3 req/s | ⚠️ PARTIAL | feedToken not auto-managed |
| Logout | POST /rest/secure/.../logout | 3 req/s | ❌ MISSING | Not called on shutdown |

### Live Market Data (REST)

| Capability | Mode | Rate Limit | Max Instruments | Implementation | Fields Captured |
|-----------|------|-----------|-----------------|----------------|-----------------|
| Full Quote | FULL | 3 req/s | Multiple via exchangeTokens | ⚠️ PARTIAL | 10/20+ fields captured |
| OHLC Quote | OHLC | 3 req/s | Multiple | ⚠️ PARTIAL | No dedicated method |
| LTP Quote | LTP | 3 req/s | Multiple | ⚠️ PARTIAL | No dedicated method |
| LTP (lightweight) | N/A | 3 req/s | 1 | ❌ MISSING | getLtpData not implemented |
| Market Depth (best-5) | FULL | 3 req/s | Multiple | ⚠️ PARTIAL | Depth in FULL but not extracted |

**Full Quote Available Fields (DOCUMENTED):**
- ✅ Captured: ltp, open, high, low, close, volume, exchTradeTime, upperCircuit, lowerCircuit, yearHigh, yearLow
- ❌ NOT Captured: netChange, percentChange, avgPrice, opnInterest(from quote), exchFeedTime, lastTradedQty, totBuyQtn, totSellQtn, depth.buy[5], depth.sell[5]

### Historical Data

| Interval | Endpoint | Max Chunk | From Date | OI in Response | Implementation |
|----------|----------|-----------|-----------|----------------|----------------|
| 1m | getCandleData | 30 days | ~2+ years | ❌ No | ✅ IMPLEMENTED |
| 5m | getCandleData | 90 days | ~5 years | ❌ No | ✅ IMPLEMENTED |
| 10m | getCandleData | 90 days | ~5 years | ❌ No | ✅ IMPLEMENTED |
| 15m | getCandleData | 90 days | ~5 years | ❌ No | ✅ IMPLEMENTED |
| 30m | getCandleData | 90 days | ~10 years | ❌ No | ✅ IMPLEMENTED |
| 1h | getCandleData | 90 days | ~10 years | ❌ No | ✅ IMPLEMENTED |
| 1d | getCandleData | 365 days | ~20 years | ❌ No | ✅ IMPLEMENTED |
| 1w | getCandleData | 365 days | ~20 years | ❌ No | ✅ IMPLEMENTED |
| 1M | **NOT_AVAILABLE** | — | — | — | ✅ BLOCKED correctly |
| 3m | **PERMANENTLY BLOCKED** | — | — | — | ✅ BLOCKED correctly |

### Historical Open Interest

| Capability | Endpoint | Intervals | Implementation | Priority |
|-----------|----------|-----------|----------------|----------|
| Historical OI time-series | POST /historical/v1/getOIData | 1m,5m,10m,15m,30m,1h,1d | 🔴 **MISSING** | CRITICAL |

**Required request body:** `{"exchange": "NFO", "symboltoken": "35004", "interval": "ONE_DAY", "fromdate": "...", "todate": "..."}`  
**Response fields:** timestamp, openInterest

### Option Greeks (REST)

| Capability | Endpoint | Per Call | Fields | Implementation | Priority |
|-----------|----------|---------|--------|----------------|----------|
| Option Greeks per expiry | POST /marketData/v1/optionGreek | 1 underlying | strikePrice, optionType, delta, gamma, theta, vega, iv, tradeVolume, oi | 🔴 **MISSING** | CRITICAL |

**Required request body:** `{"name": "NIFTY", "expirydate": "29FEB2024"}`

### Broker Analytics

| Capability | Endpoint | Implementation |
|-----------|----------|----------------|
| Put-Call Ratio | GET /marketData/v1/putCallRatio | ✅ IMPLEMENTED |
| OI Buildup (long/short buildup, covering, unwinding) | POST /marketData/v1/OIBuildup | ✅ IMPLEMENTED |
| Gainers / Losers | GET /marketData/v1/gainersLosers | ✅ IMPLEMENTED |
| NSE Intraday Market Breadth | GET /marketData/v1/nseIntraday | ❌ MISSING |
| BSE Intraday Market Breadth | GET /marketData/v1/bseIntraday | ❌ MISSING |
| Instrument Search | POST /order/v1/searchScrip | ❌ MISSING |

### WebSocket (SmartStream V2)

| Capability | Status | Notes |
|-----------|--------|-------|
| Connection URL | ✅ Correct | wss://smartapisocket.angelone.in/smart-stream |
| Authentication | ✅ Correct | JWT + feedToken headers |
| feedToken auto-fetch | ⚠️ PARTIAL | feedToken not automatically obtained; caller must supply |
| Subscribe (action=1) | 🔴 DEFECTIVE | Mode value sent as string ("LTP"), should be integer (1/2/3) |
| Unsubscribe (action=0) | ❌ MISSING | No unsubscribe method |
| Exchange type: NSE EQ (1) | ✅ IMPLEMENTED | |
| Exchange type: NFO (2) | 🔴 **MISSING** | F&O options CANNOT be subscribed |
| Exchange type: BSE EQ (3) | 🔴 MISSING | |
| Exchange type: MCX (5) | 🔴 MISSING | |
| Binary frame decode | 🔴 **BROKEN** | json.loads() used on binary frames — will fail entirely |
| Mode LTP (1) | 🔴 DEFECTIVE | String sent, integer required |
| Mode QUOTE (2) | 🔴 DEFECTIVE | String sent, integer required |
| Mode FULL (3) | 🔴 DEFECTIVE | String sent, integer required |
| Reconnect + backoff | ✅ IMPLEMENTED | exp backoff base=1s, max=60s, 10 attempts |
| Resubscribe on reconnect | ✅ IMPLEMENTED | tokens replayed |
| OI from FULL mode | ❌ NOT_DECODED | Binary protocol not decoded |
| Depth from FULL mode | ❌ NOT_DECODED | Binary protocol not decoded |

---

## UPSTOX V2/V3

### Authentication

| Capability | Endpoint | Implementation | Notes |
|-----------|----------|----------------|-------|
| OAuth2 Authorization | GET /v2/login/authorization/dialog | ⚠️ PARTIAL | No callback handler; manual token injection |
| Token Exchange | POST /v2/login/authorization/token | ⚠️ PARTIAL | _do_token_refresh() uses wrong grant type for server-side flow |
| 401 → refresh → retry | Automatic | ✅ IMPLEMENTED | |
| Token worker-sharing (Redis) | N/A | ❌ MISSING | Unlike Angel One, Upstox token not shared across workers |
| Analytics Token (long-lived) | Developer portal | ❌ MISSING | March 2026 — 1 year validity, ideal for data service |
| Logout | DELETE /v2/logout | ❌ MISSING | |

### Historical Data

| Interval | V3 URL Pattern | Max Window | From Date | OI | Implementation |
|----------|---------------|-----------|-----------|-----|----------------|
| 1m | /v3/historical-candle/{key}/minutes/1/{to}/{from} | 1 month | Jan 2022 | ✅ Yes (index 6) | 🔴 **MISSING** (uses V2) |
| 5m | /v3/historical-candle/{key}/minutes/5/{to}/{from} | 1 month | Jan 2022 | ✅ Yes | 🔴 MISSING |
| 10m | /v3/historical-candle/{key}/minutes/10/{to}/{from} | 1 month | Jan 2022 | ✅ Yes | 🔴 MISSING |
| 15m | /v3/historical-candle/{key}/minutes/15/{to}/{from} | 1 month | Jan 2022 | ✅ Yes | 🔴 MISSING |
| 30m | /v3/historical-candle/{key}/minutes/30/{to}/{from} | 1 quarter | Jan 2022 | ✅ Yes | 🔴 MISSING |
| 1h | /v3/historical-candle/{key}/hours/1/{to}/{from} | 1 quarter | Jan 2022 | ✅ Yes | 🔴 MISSING |
| 1d | /v3/historical-candle/{key}/days/1/{to}/{from} | 1 decade | Jan 2000 | ✅ Yes | 🔴 MISSING |
| 1w | /v3/historical-candle/{key}/weeks/1/{to}/{from} | Unlimited | Jan 2000 | ✅ Yes | 🔴 MISSING |
| 1M | /v3/historical-candle/{key}/months/1/{to}/{from} | Unlimited | Jan 2000 | ✅ Yes | 🔴 MISSING |
| 3m | PERMANENTLY BLOCKED | — | — | — | ✅ BLOCKED correctly |
| Arbitrary minutes (2-300) | /v3/historical-candle/{key}/minutes/{N}/... | Varies | Jan 2022 | ✅ Yes | ❌ MISSING |

**Note:** V3 candle array: `[timestamp, open, high, low, close, volume, open_interest]` — OI at index 6 is MISSING from current V2 adapter.

### Intraday Data

| Capability | V3 URL Pattern | Implementation | Notes |
|-----------|---------------|----------------|-------|
| Intraday minutes | /v3/historical-candle/intraday/{key}/minutes/{N} | ❌ **MISSING** | Current session only |
| Intraday hours | /v3/historical-candle/intraday/{key}/hours/{N} | ❌ MISSING | |
| Intraday days | /v3/historical-candle/intraday/{key}/days/1 | ❌ MISSING | |

### Live Market Quotes

| Capability | Endpoint | Version | Max Instruments | Rate Limit | Implementation | Fields |
|-----------|----------|---------|-----------------|-----------|----------------|--------|
| LTP | GET /v3/market-quote/ltp | V3 (current) | NOT_VERIFIED | 50 req/s | ❌ MISSING | ltp, ltq, volume, cp |
| OHLC | GET /v3/market-quote/ohlc | V3 (current) | NOT_VERIFIED | 50 req/s | ❌ MISSING | prev_ohlc + live_ohlc |
| Full Quote | GET /v2/market-quote/quotes | V2 (still current) | 500 | 50 req/s | ⚠️ PARTIAL | Raw dict returned, normalization missing |
| LTP V2 | GET /v2/market-quote/ltp | V2 (**DEPRECATED**) | — | — | NOT_USED | Superseded by V3 |
| OHLC V2 | GET /v2/market-quote/ohlc | V2 (**DEPRECATED**) | — | — | NOT_USED | Superseded by V3 |

**Full Quote Fields Available (V2, DOCUMENTED):**
- ✅ Returned by adapter: raw response dict
- ❌ NOT normalized: ohlc, depth.buy[5], depth.sell[5], net_change, lower/upper_circuit, oi (F&O), timestamp

### Option Greeks

| Capability | Endpoint | Max Per Call | Fields | Rate Limit | Implementation |
|-----------|----------|-------------|--------|-----------|----------------|
| Option Greeks | GET /v3/market-quote/option-greek | **50 instruments** | last_price, ltq, volume, cp, iv, vega, gamma, theta, delta, oi | 50 req/s | 🔴 **MISSING** |

**Note:** Batching required for chains with >50 strikes (e.g. NIFTY with 100+ strikes needs 2+ calls).

### Option Chain

| Capability | Endpoint | Implementation | Fields |
|-----------|----------|----------------|--------|
| Option Chain (per expiry) | GET /v2/option/chain | 🔴 **MISSING** | PCR, all strikes, CE+PE: ltp, close, volume, oi, prev_oi, bid, ask + Greeks |
| Option Contracts (active) | GET /v2/option/contract | 🔴 MISSING | instrument_key, strike, expiry, lot_size, tick_size, weekly flag |

### Market Information

| Capability | Endpoint | Launched | Rate Limit | Implementation |
|-----------|----------|---------|-----------|----------------|
| Market Holidays | GET /v2/market/holidays | Legacy | 50 req/s | ❌ MISSING |
| Market Timings | GET /v2/market/timings/{date} | Legacy | 50 req/s | ❌ MISSING |
| Exchange Status | GET /v2/market/status/{exchange} | Legacy | 50 req/s | ❌ MISSING |
| FII Activity | GET (Market Information) | May 2026 | 50 req/s | ❌ MISSING |
| DII Activity | GET (Market Information) | May 2026 | 50 req/s | ❌ MISSING |
| Open Interest Analytics | GET (Market Information) | May 2026 | 50 req/s | ❌ MISSING |
| Change in OI | GET (Market Information) | May 2026 | 50 req/s | ❌ MISSING |
| Max Pain | GET (Market Information) | May 2026 | 50 req/s | ❌ MISSING |
| Put-Call Ratio | GET (Market Information) | May 2026 | 50 req/s | ❌ MISSING |

### Closing Auction Session (CAS)

| Capability | Launched | Implementation | Notes |
|-----------|---------|----------------|-------|
| CAS fields in Full Quote | Sep 4, 2026 | ❌ **MISSING** | indicative_equilibrium_price, quantity, imbalance, reference_price |
| CAS fields in WebSocket V3 | Sep 4, 2026 | ❌ MISSING | Same fields via live feed |
| Exchange CAS status | Aug 2026 | ❌ MISSING | cas_eligible_status in Exchange Status API |
| cas_eligible in instruments | Aug 2026 | ❌ MISSING | Flag in BOD instrument JSON |

**Critical:** Indicative equilibrium price during CAS MUST NOT be treated as LTP.

### WebSocket (V3 Protobuf)

| Capability | Status | Notes |
|-----------|--------|-------|
| Authorization endpoint | 🔴 **BROKEN** | Must call GET .../authorize → use returned URI. Code uses hardcoded V2 URL. |
| V2 WebSocket (discontinued) | 🔴 **DEAD** | Discontinued Aug 22, 2025. Current code URL is V2. |
| Protobuf decode | 🔴 **STUB** | _decode_protobuf() JSON-tries first, then {} — every real tick discarded |
| Mode: ltpc | ⚠️ PARTIAL | Subscription msg correct; response not decoded |
| Mode: option_greeks | ❌ **MISSING** | Not in code |
| Mode: full | ⚠️ PARTIAL | Subscription msg correct; response not decoded |
| Mode: full_d30 (Plus) | ❌ **MISSING** | Not in code |
| Market status first tick | ❌ NOT_DECODED | |
| Fields: ltp, ltt, ltq, cp (ltpc) | ❌ NOT_DECODED | |
| Fields: marketLevel depth-5 | ❌ NOT_DECODED | |
| Fields: marketLevel depth-30 (Plus) | ❌ NOT_DECODED | |
| Fields: optionGreeks | ❌ NOT_DECODED | |
| Fields: marketOHLC | ❌ NOT_DECODED | |
| Fields: atp, vtt, oi, tbq, tsq | ❌ NOT_DECODED | |
| Fields: CAS (Sep 2026) | ❌ NOT_DECODED | |
| Heartbeat (server ping) | ✅ IMPLEMENTED | |
| Reconnect + backoff | ✅ IMPLEMENTED | exp backoff base=1s, max=60s, 10 attempts |
| Resubscribe on reconnect | ✅ IMPLEMENTED | instrument_keys replayed |

**Subscription limits (standard plan):**

| Mode | Individual Limit | Combined Limit |
|------|-----------------|----------------|
| ltpc | 5,000 keys | 2,000 keys |
| option_greeks | 3,000 keys | 2,000 keys |
| full | 2,000 keys | 1,500 keys |
| full_d30 (Plus only) | 50 keys | 1,500 keys |

### Expired Instruments (Upstox Plus)

| Capability | Endpoint | Implementation |
|-----------|----------|----------------|
| Get Expiries | GET /v2/expired-instruments/expiries | ❌ MISSING |
| Get Expired Option Contracts | GET /v2/expired-instruments/option/contract | ❌ MISSING |
| Get Expired Future Contracts | GET /v2/expired-instruments/future/contract | ❌ MISSING |
| Get Expired Historical Candle Data | GET /v2/expired-instruments/historical-candle/... | ❌ MISSING |

**Note:** Upstox Plus plan required. Critical for survivorship-bias-free historical F&O backfill.

---

## RATE LIMITS SUMMARY

| Provider | Current Code | Actual Documented | Correct? |
|---------|-------------|-------------------|---------|
| Angel One | 3.0 req/s | 3.0 req/s | ✅ |
| Upstox | 10.0 req/s | **50.0 req/s**, 500/min, 2000/30min | ❌ 5x understated |
| Scrapling NSE | 2.0 req/s | 2.0 req/s (conservative) | ✅ |
| Jugaad Data | 1.0 req/s | 1.0 req/s (conservative) | ✅ |
| OpenChart | 5.0 req/s | 5.0 req/s (conservative) | ✅ |
| Yahoo Finance | 1.0 req/s | 1.0 req/s (conservative) | ✅ |

---

## CAPABILITY ROUTING MATRIX

| Data Need | Primary Provider | Secondary | Fallback | Notes |
|-----------|-----------------|-----------|----------|-------|
| Live options tick (F&O) | Upstox WS (full mode) | Angel One WS (FULL, once fixed) | Scrapling NSE | |
| Live option Greeks (tick) | Upstox WS (option_greeks mode) | Angel One WS (FULL mode) | None | |
| Option Greeks (REST snapshot) | Upstox V3 /option-greek | Angel One /optionGreek | None | Batch 50 for Upstox |
| Option Chain snapshot | Upstox /v2/option/chain | Scrapling NSE | None | Per expiry |
| Historical OHLCV EQ 1m-1h | Angel One | Upstox V3 | OpenChart | Angel One primary by design |
| Historical OHLCV IDX 1m-1h | Upstox V3 | Angel One | OpenChart | Upstox primary for indices |
| Historical OHLCV F&O intraday | Angel One | Upstox V3 | OpenChart | |
| Historical OHLCV daily/weekly | Upstox V3 | Jugaad Data (F&O) | Yahoo Finance (EQ) | V3 includes OI |
| Historical OHLCV monthly | Upstox V3 (months/1) | Yahoo Finance | None | Angel One no monthly |
| Historical OI time-series | Angel One (getOIData) | Upstox V3 (index 6) | None | Both needed |
| Historical F&O backfill (expired) | Upstox Plus expired APIs | Jugaad Data | None | Anti-survivorship-bias |
| Live equity quote | Angel One FULL | Upstox full quote V2 | Scrapling NSE | |
| Live index quote | Upstox LTP/OHLC V3 | Angel One FULL | Scrapling NSE | |
| Market depth (5 level) | Angel One WS (FULL) | Upstox WS (full) | Upstox REST full quote | |
| Market depth (30 level) | Upstox WS (full_d30, Plus) | Upstox WS (full, 5 levels) | None | Plus plan required |
| Market calendar/holidays | Upstox /market/holidays | Internal calendar table | None | |
| Exchange session status | Upstox /market/status | Internal session engine | None | |
| PCR analytics | Angel One PCR | Upstox Market Info (May 2026) | None | |
| OI buildup analytics | Angel One | None | None | |
| Max Pain | Upstox Market Info (May 2026) | Derived from option chain | None | |
| Closing auction data | Upstox CAS (Sep 2026) | None | None | MUST be separate from LTP |

---

## PROVIDER API USAGE COVERAGE

### Angel One — All Documented Relevant APIs

| API | Status | Reason if Not Used |
|----|--------|-------------------|
| loginByPassword | USED | Authentication |
| generateTokens | USED | Token refresh |
| getProfile (feedToken) | PARTIAL | feedToken not auto-fetched |
| logout | NOT_USED | No graceful shutdown implementation |
| getCandleData | USED | Historical OHLCV |
| getOIData | NOT_USED | **Not implemented — CRITICAL GAP** |
| market/v1/quote (FULL) | USED | Live quote |
| market/v1/quote (LTP mode) | NOT_USED | Adapter always uses FULL regardless of need |
| market/v1/quote (OHLC mode) | NOT_USED | Adapter always uses FULL regardless of need |
| getLtpData | NOT_USED | Lightweight LTP not implemented |
| optionGreek | NOT_USED | **Not implemented — CRITICAL GAP** |
| putCallRatio | USED | PCR analytics |
| OIBuildup | USED | OI buildup analytics |
| gainersLosers | USED | Gainers/losers analytics |
| nseIntraday | NOT_USED | Not implemented |
| bseIntraday | NOT_USED | Not implemented |
| searchScrip | NOT_USED | Not implemented |
| SmartStream WebSocket | PARTIAL/BROKEN | Binary decode broken; NFO exchange type missing |

### Upstox — All Documented Relevant APIs

| API | Status | Reason if Not Used |
|----|--------|-------------------|
| /v2/login/authorization/token | PARTIAL | Requires pre-obtained code |
| Analytics Token | NOT_USED | Not implemented — useful for server-side |
| /v3/historical-candle (V3) | NOT_USED | **CRITICAL — using deprecated V2 instead** |
| /v3/historical-candle/intraday (V3) | NOT_USED | Not implemented |
| /v2/historical-candle (V2) | USED (WRONG) | Deprecated — migrate to V3 |
| /v3/market-quote/ltp | NOT_USED | Not implemented |
| /v3/market-quote/ohlc | NOT_USED | Not implemented |
| /v2/market-quote/quotes (Full) | USED | Live full quote |
| /v3/market-quote/option-greek | NOT_USED | **Not implemented — CRITICAL GAP** |
| /v2/option/chain | NOT_USED | **Not implemented — CRITICAL GAP** |
| /v2/option/contract | NOT_USED | Not implemented |
| /v2/market/holidays | NOT_USED | Not implemented |
| /v2/market/timings | NOT_USED | Not implemented |
| /v2/market/status | NOT_USED | Not implemented |
| Market Information APIs (May 2026) | NOT_USED | Not implemented |
| CAS data (Sep 2026) | NOT_USED | Not implemented |
| /v2/instruments/search | NOT_VERIFIED | Unknown |
| BOD Instrument JSON | NOT_VERIFIED | Unknown |
| Expired Instruments APIs | NOT_USED | Upstox Plus plan required |
| WebSocket V3 (authorized URI) | BROKEN | V2 URL used; Protobuf stub |
| WebSocket V2 | DISCONTINUED | Stopped Aug 22, 2025 |

---

*Machine-readable version: `provider_capability_matrix.json`*  
*Full forensic audit: `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`*
