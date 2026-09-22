# ANGEL ONE DATA COMPLETENESS REPORT
## data-service2.0 — AlphaForge Market Data Platform

**Report Date:** 2026-09-16 *(see UPDATE — 2026-09-22 for v2.2.0 changes)*  
**Provider:** Angel One SmartAPI  
**Audit Type:** Static code analysis + API documentation cross-reference  
**Live Verification:** Requires credentials — NOT_VERIFIED without live credentials

---

## SUMMARY

| Dimension | Status |
|-----------|--------|
| Authentication | IMPLEMENTED — Multi-worker safe Redis JWT sharing |
| Historical OHLCV | IMPLEMENTED — 1m/5m/10m/15m/30m/1h/1d/1w (V2 API) |
| Historical OI | IMPLEMENTED (new) — getOIData endpoint wired |
| Live Quote (FULL mode) | IMPLEMENTED — all 17+ fields normalized |
| Option Greeks REST | IMPLEMENTED (new) — optionGreek endpoint wired |
| Broker Analytics (PCR, OI Buildup) | IMPLEMENTED |
| WebSocket (SmartStream V2) | IMPLEMENTED — binary decode, NFO exchange type |
| 3m interval block | ENFORCED — at DB, adapter, and capability matrix levels |
| 1M monthly candle | CORRECTLY BLOCKED — not supported by Angel One |

---

## AUTHENTICATION

| API | Endpoint | Status | Notes |
|----|----------|--------|-------|
| TOTP + JWT Login | POST /rest/auth/.../loginByPassword | IMPLEMENTED | TOTP generated from seed |
| Token refresh | POST /rest/auth/.../generateTokens | IMPLEMENTED | rotate_token() |
| Get Profile (feedToken) | GET /rest/secure/.../getProfile | IMPLEMENTED | feedToken for SmartStream |
| Multi-worker JWT sharing | Redis mds:angel_one:jwt:{client_id} | IMPLEMENTED | 6h TTL, distributed lock |
| Logout | POST /rest/secure/.../logout | NOT_IMPLEMENTED | Not called on shutdown — low priority |

**Credential security:** API key, TOTP secret, MPIN, JWT — none logged or persisted except JWT in Redis (required for multi-worker). Redis key has 6h TTL.

---

## HISTORICAL DATA

| Interval | API | Max Window | OI in Response | Status | Latency* |
|----------|-----|-----------|----------------|--------|----------|
| 1m | getCandleData | 30 days | ❌ No | IMPLEMENTED | NOT_MEASURED |
| 5m | getCandleData | 90 days | ❌ No | IMPLEMENTED | NOT_MEASURED |
| 10m | getCandleData | 90 days | ❌ No | IMPLEMENTED | NOT_MEASURED |
| 15m | getCandleData | 90 days | ❌ No | IMPLEMENTED | NOT_MEASURED |
| 30m | getCandleData | 90 days | ❌ No | IMPLEMENTED | NOT_MEASURED |
| 1h | getCandleData | 90 days | ❌ No | IMPLEMENTED | NOT_MEASURED |
| 1d | getCandleData | 365 days | ❌ No | IMPLEMENTED | NOT_MEASURED |
| 1w | getCandleData | 365 days | ❌ No | IMPLEMENTED | NOT_MEASURED |
| 1M | NOT_AVAILABLE | — | — | CORRECTLY_BLOCKED | — |
| 3m | PERMANENTLY_BLOCKED | — | — | CORRECTLY_BLOCKED | — |

*Latency not measurable without live credentials.

**Key constraint:** Angel One historical candles do NOT include open interest. OI must be sourced separately via getOIData.

---

## HISTORICAL OPEN INTEREST

| API | Intervals | Exchange | Status | Notes |
|----|-----------|----------|--------|-------|
| getOIData | 1m,5m,10m,15m,30m,1h,1d | NFO, MCX | IMPLEMENTED (new) | Dedicated endpoint — separate from candles |

**OI semantic compliance:** OI is NULL when absent — never substituted with zero. Never populated from tradedValue.

---

## LIVE MARKET DATA

| Capability | Mode | Max Instruments | Fields | Status |
|-----------|------|-----------------|--------|--------|
| LTP quote | FULL | Multiple via exchangeTokens | All FULL fields | IMPLEMENTED |
| LTP only (lightweight) | LTP | 1 | ltp only | IMPLEMENTED (new) — getLtpData |
| OHLC quote | OHLC | Multiple | O/H/L/C | Available (same endpoint, different mode) |
| Full quote with depth | FULL | Multiple | 17+ fields incl. depth | IMPLEMENTED |

**FULL quote fields captured:**

| Field | Angel One Key | Status |
|-------|---------------|--------|
| LTP | ltp | ✅ CAPTURED |
| Open | open | ✅ CAPTURED |
| High | high | ✅ CAPTURED |
| Low | low | ✅ CAPTURED |
| Previous Close | close | ✅ CAPTURED |
| Volume | tradeVolume | ✅ CAPTURED |
| Open Interest | opnInterest | ✅ CAPTURED (OI null when absent) |
| Net Change | netChange | ✅ CAPTURED |
| % Change | percentChange | ✅ CAPTURED |
| Avg Price | avgPrice | ✅ CAPTURED |
| Total Buy Qty | totBuyQtn | ✅ CAPTURED |
| Total Sell Qty | totSellQtn | ✅ CAPTURED |
| Last Traded Qty | lastTradedQty | ✅ CAPTURED |
| Upper Circuit | upperCircuit | ✅ CAPTURED |
| Lower Circuit | lowerCircuit | ✅ CAPTURED |
| 52W High | yearHigh | ✅ CAPTURED |
| 52W Low | yearLow | ✅ CAPTURED |
| Exchange Feed Time | exchFeedTime | ✅ CAPTURED (as sourceTimestamp) |
| Depth Buy 5 levels | depth.buy | ✅ CAPTURED (normalized to depthBuy[]) |
| Depth Sell 5 levels | depth.sell | ✅ CAPTURED (normalized to depthSell[]) |

---

## OPTION GREEKS

| API | Per Call | Fields | Status |
|----|---------|--------|--------|
| optionGreek endpoint | 1 underlying+expiry | strikePrice, optionType, delta, gamma, theta, vega, iv, tradeVolume, oi | IMPLEMENTED (new) |

**Greeks provenance:** All Angel One Greeks tagged `greekSource=PROVIDER`. Zero IV rejected (treated as missing). rho is NULL (Angel One does not provide rho).

---

## BROKER ANALYTICS

| API | Status |
|----|--------|
| Put-Call Ratio | IMPLEMENTED |
| OI Buildup (long/short buildup, covering, unwinding) | IMPLEMENTED |
| Gainers/Losers | IMPLEMENTED |
| NSE Intraday Market Breadth | IMPLEMENTED (new) |
| BSE Intraday Market Breadth | NOT_IMPLEMENTED — low priority |

---

## WEBSOCKET (SmartStream V2)

| Capability | Status | Notes |
|-----------|--------|-------|
| Connection URL | CORRECT | wss://smartapisocket.angelone.in/smart-stream |
| Binary frame decode | IMPLEMENTED (new) | Struct unpacker for LTP/QUOTE/FULL modes |
| Paise → INR conversion | IMPLEMENTED | Divide by 100 in decoder |
| Exchange type NSE EQ (1) | IMPLEMENTED | |
| Exchange type NFO (2) | IMPLEMENTED (new) | Required for options |
| Exchange type BSE EQ (3) | IMPLEMENTED (new) | |
| Exchange type MCX (5) | IMPLEMENTED (new) | |
| Mode: LTP (1) | IMPLEMENTED | Integer value, not string |
| Mode: QUOTE (2) | IMPLEMENTED | Integer value |
| Mode: FULL (3) | IMPLEMENTED | Integer value + depth decoded |
| Depth in FULL mode | IMPLEMENTED | Best-5 buy/sell decoded |
| OI in FULL mode | IMPLEMENTED | From explicit OI field only |
| feedToken auto-fetch | PARTIAL | feedToken supplied externally (from getProfile) |
| Reconnect with backoff | IMPLEMENTED | exp base=1s max=60s, 10 attempts |
| Resubscribe on reconnect | IMPLEMENTED | token_groups replayed |
| Unsubscribe | IMPLEMENTED (new) | action=0 |

**CRITICAL NOTE:** The Protobuf `.proto` file for SmartStream V2 binary format is based on reverse-engineering the documented format. The exact byte offsets for FULL mode fields (OI, depth, circuit limits) should be verified against live feed data when credentials are available.

---

## ERROR RATE

| Error Type | Handling | Status |
|-----------|---------|--------|
| HTTP 429 | ProviderRateLimitedError — circuit breaker NOT counted | IMPLEMENTED |
| HTTP 401 | Re-auth once, then ProviderAuthError | IMPLEMENTED |
| HTTP 5xx | ProviderUnavailableError — circuit breaker counted | IMPLEMENTED |
| Network timeout | ProviderUnavailableError | IMPLEMENTED |
| Malformed JSON | ProviderDataError — circuit breaker counted | IMPLEMENTED |
| Market closed | ProviderMarketClosedError — circuit breaker NOT counted | IMPLEMENTED |
| 3m interval request | ProviderUnsupportedError — circuit breaker NOT counted | IMPLEMENTED |

---

## KNOWN LIMITATIONS

1. **1M monthly candles:** Not supported by Angel One SmartAPI. Use Upstox V3 (months/1).
2. **Historical candle OI:** Angel One getCandleData does NOT include OI. Must use getOIData separately.
3. **FULL mode depth accuracy:** Depth byte offsets in SmartStream binary protocol are based on documented format — verify against live feed.
4. **feedToken lifecycle:** Angel One feedToken is obtained at login but its expiry is not explicitly documented. May need rotation with JWT.
5. **Concurrent TOTP sessions:** Single TOTP code valid for 30s window — Redis distributed lock prevents conflicts but does not handle clock skew between workers.

---

*Report generated: 2026-09-16. Live verification requires credentials configured in .env.local.*


---

## UPDATE — 2026-09-17

| Change | Detail |
|--------|--------|
| BUG-001 FIXED | MarketEngine now resolves numeric Angel One token via InstrumentMasterService — zero HTTP 400s |
| BUG-002 FIXED | `fetch_pcr()` handles list response correctly |
| market_quote persistence ADDED | Live quotes (ltp, OHLC, depth, weekHigh52/Low, circuits) now persisted to `market_quote` |
| Candle upsert source_timestamp ADDED | Written from provider response where available |
| API response per-candle provider/sourceType ADDED | `provider` and `sourceType` now included in every candle in `GET /v1/india/historical` |
| Live verification | Auth re-confirmed; RELIANCE ltp=1240.6 in market_quote; HDFCBANK ltp=714.1 in market_quote; option_greeks 328 contracts confirmed |

*Unit tests: 4,413 passing (0 failures) as of 2026-09-17.*

---

## UPDATE — 2026-09-22 (v2.2.0)

| Change | Detail |
|--------|--------|
| 5y intraday backfill COMPLETE | ~123M equity candle rows; 298 instruments; Nifty50: full 5y; F&O universe: ~2y (Upstox plan limit) |
| fo_universe table seeded | 314 rows (293 active, 21 retired); used by catch-up worker as instrument source |
| Angel One → Upstox auto-fallback | `angel_one_token_unknown_upstox_fallback` fires for 301 symbol ISIN-mapped instruments |
| OHLCV catch-up worker uses Angel One | IDX intraday intervals route to Angel One (`1m`–`1h`); Upstox used only for EOD |
| 1M monthly not available | Unchanged — Angel One does not support monthly candles |
| Historical OI plan restriction | `getOIData` still returns "Invalid Bad Request" (BLOCKED_BY_EXTERNAL — plan restriction) |
| Catch-up worker for Angel One | Worker calls `HistoricalEngine.run_backfill()` per-instrument per-interval from last Redis checkpoint |

**Angel One intraday depth for indices:** NSE index instruments (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, etc.) use Angel One as the intraday provider, since Upstox returns `UDAPI100011` for all `NSE_INDEX` instruments at `1m`–`1h`. Angel One provides index token-based intraday data. `MIDCPNIFTY` is available on Angel One but not Upstox.

*Unit tests: 4,821+ passing (0 failures) as of 2026-09-22.*
