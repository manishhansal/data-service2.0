# PROVIDER API USAGE MATRIX
## data-service2.0 — AlphaForge Market Data Platform

**Date:** 2026-09-22 *(updated from 2026-09-16)*  
**Scope:** All documented relevant Angel One SmartAPI and Upstox V2/V3 APIs

Classification:
- **USED** — API is implemented and called by production code
- **NOT_NEEDED** — API exists but is not relevant to market data service
- **NOT_SUPPORTED** — Provider does not support this capability
- **NOT_VERIFIED** — Code exists but not confirmed working with live credentials
- **DEPRECATED** — Provider has deprecated this endpoint
- **PLUS_REQUIRED** — Requires Upstox Plus subscription
- **NOT_IMPLEMENTED** — Relevant but not yet implemented

---

## ANGEL ONE SmartAPI

### Authentication & Session

| API Route | Description | Classification | Reason |
|-----------|-------------|----------------|--------|
| POST /rest/auth/.../loginByPassword | TOTP + JWT login | USED | Core authentication |
| POST /rest/auth/.../generateTokens | Token refresh | USED | JWT rotation |
| GET /rest/secure/.../getProfile | User profile + feedToken | USED | feedToken for SmartStream |
| POST /rest/secure/.../logout | Session logout | NOT_IMPLEMENTED | Low priority for server-side service |

### Historical Data

| API Route | Description | Classification | Reason |
|-----------|-------------|----------------|--------|
| POST /historical/v1/getCandleData | Historical OHLCV candles | USED | Primary for EQ/FO intraday 1m–1w; used by OHLCV catch-up worker via HistoricalEngine |
| POST /historical/v1/getOIData | Historical Open Interest time-series | USED | Dedicated OI endpoint for derivatives |

### Live Market Data

| API Route | Description | Classification | Reason |
|-----------|-------------|----------------|--------|
| POST /market/v1/quote (mode=FULL) | Full quote with depth | USED | Primary live quote |
| POST /market/v1/quote (mode=LTP) | LTP only | USED | Lightweight quote (getLtpData wrapper) |
| POST /market/v1/quote (mode=OHLC) | OHLC quote | USED | OHLC without depth |
| POST /order/v1/getLtpData | Lightweight LTP endpoint | USED | Single instrument LTP |

### Market Analytics

| API Route | Description | Classification | Reason |
|-----------|-------------|----------------|--------|
| POST /marketData/v1/optionGreek | Option Greeks per expiry | USED | Delta, Gamma, Theta, Vega, IV |
| GET /marketData/v1/putCallRatio | Put-Call Ratio | USED | PCR analytics |
| POST /marketData/v1/OIBuildup | OI buildup analysis | USED | Long/short buildup detection |
| GET /marketData/v1/gainersLosers | Top gainers/losers | USED | Market breadth |
| GET /marketData/v1/nseIntraday | NSE intraday breadth | USED | Intraday market data |
| GET /marketData/v1/bseIntraday | BSE intraday breadth | NOT_IMPLEMENTED | Low priority — BSE breadth |

### Orders & Portfolio (NOT relevant to market data service)

| API Route | Description | Classification | Reason |
|-----------|-------------|----------------|--------|
| POST /order/v1/placeOrder | Place order | NOT_NEEDED | AlphaForge handles order execution |
| POST /order/v1/modifyOrder | Modify order | NOT_NEEDED | Order management not in scope |
| GET /order/v1/getOrderBook | Order book | NOT_NEEDED | Not a market data function |
| GET /portfolio/v1/getHolding | Holdings | NOT_NEEDED | Portfolio management not in scope |

### Instrument Search

| API Route | Description | Classification | Reason |
|-----------|-------------|----------------|--------|
| POST /order/v1/searchScrip | Search instrument | NOT_IMPLEMENTED | Lower priority; instrument master loaded from static file |

### WebSocket (SmartStream V2)

| Feature | Description | Classification | Reason |
|---------|-------------|----------------|--------|
| wss://smartapisocket.angelone.in/smart-stream | SmartStream V2 | USED | Primary live feed |
| Exchange type 1 (NSE EQ) | NSE equities | USED | Equity live data |
| Exchange type 2 (NFO) | NSE F&O | USED | Options and futures live data |
| Exchange type 3 (BSE EQ) | BSE equities | USED | BSE live data |
| Exchange type 4 (BSE FO) | BSE F&O | USED | BSE derivatives |
| Exchange type 5 (MCX) | Commodity | USED | MCX commodities |
| Mode 1 (LTP) | LTP only | USED | Lightweight streaming |
| Mode 2 (QUOTE) | OHLCV + quantities | USED | Quote streaming |
| Mode 3 (FULL) | Full with depth + OI | USED | Complete market data |

---

## UPSTOX V2/V3

### Authentication

| API Route | Version | Description | Classification | Reason |
|-----------|---------|-------------|----------------|--------|
| GET /v2/login/authorization/dialog | V2 | OAuth2 flow initiation | NOT_IMPLEMENTED | Browser-based flow; server uses pre-obtained token |
| POST /v2/login/authorization/token | V2 | Token exchange | USED | 401-triggered token refresh |
| DELETE /v2/logout | V2 | Logout | NOT_IMPLEMENTED | Not needed for server-side service |
| Analytics Token (developer portal) | V2 | Long-lived token | USED | set_analytics_token() implemented |

### Historical Data

| API Route | Version | Description | Classification | Reason |
|-----------|---------|-------------|----------------|--------|
| GET /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from} | V3 | Historical OHLCV + OI | USED | Primary for all historical data; called by catch-up worker and backfill scripts |
| GET /v3/historical-candle/intraday/{key}/{unit}/{interval} | V3 | Current-session candles | USED | Intraday gap recovery |
| GET /v2/historical-candle/... | V2 | Historical candle (deprecated) | DEPRECATED | Replaced by V3. Backward-compat alias kept for tests only |
| GET /v2/historical-candle/intraday/... | V2 | Intraday (deprecated) | DEPRECATED | Replaced by V3 |
| GET /v2/expired-instruments/expiries | V2 (Plus) | Historical expiry dates | USED | Survivorship-bias-free backfill |
| GET /v2/expired-instruments/option/contract | V2 (Plus) | Expired option contracts | USED | Historical option universe |
| GET /v2/expired-instruments/future/contract | V2 (Plus) | Expired future contracts | USED | Historical futures universe |
| GET /v2/expired-instruments/historical-candle/... | V2 (Plus) | Expired contract candles | USED | Historical F&O OHLCV |

> **NSE_INDEX intraday routing note (2026-09-22):** Upstox returns `UDAPI100011` (Instrument not found) for all `NSE_INDEX` instruments when interval is `1m`–`1h`. The `HistoricalEngine._resolve_provider()` method now routes IDX instruments to Upstox **only for EOD intervals** (`1d`/`1w`/`1M`). Intraday requests for indices fall back to Angel One. `MIDCPNIFTY` (Nifty Midcap Select) is unavailable on Upstox at any interval and always routes to Angel One.

> **301 NSE_EQ\|ISIN keys (2026-09-21):** `_UPSTOX_INSTRUMENT_KEYS` expanded from 51 → 301 symbols, covering all 229 NSE F&O universe stocks with verified ISINs plus all NSE index display name aliases. When `angel_one_token_unknown` fires and the symbol has a registered ISIN key, the engine auto-routes to Upstox and logs `angel_one_token_unknown_upstox_fallback`.

### Live Market Quotes

| API Route | Version | Description | Classification | Reason |
|-----------|---------|-------------|----------------|--------|
| GET /v3/market-quote/ltp | V3 | LTP + ltq + volume + prevClose | USED | Lightweight live quote |
| GET /v3/market-quote/ohlc | V3 | OHLC with prev+live candles | USED | OHLC quote |
| GET /v3/market-quote/quotes | **V3** | Full quote (max 500) + CAS fields | USED | **Migrated from V2 April 2025 — V3 adds CAS indicative price fields** |
| GET /v3/market-quote/option-greek | V3 | Option Greeks (max 50) | USED | IV + Greeks for F&O |
| GET /v2/market-quote/ohlc | V2 | OHLC (deprecated) | DEPRECATED | Replaced by V3 |
| GET /v2/market-quote/ltp | V2 | LTP (deprecated) | DEPRECATED | Replaced by V3 |
| ~~GET /v2/market-quote/quotes~~ | ~~V2~~ | ~~Full quote (deprecated)~~ | ~~DEPRECATED~~ | Migrated to V3 April 2025 |

### Option Chain

| API Route | Version | Description | Classification | Reason |
|-----------|---------|-------------|----------------|--------|
| GET /v2/option/chain | V2 | Option chain with Greeks | USED | Primary option chain source |
| GET /v2/option/contract | V2 | Active option contracts | USED | Contract metadata |

### Market Information

| API Route | Version | Description | Classification | Reason |
|-----------|---------|-------------|----------------|--------|
| GET /v2/market/status/{exchange} | V2 | Exchange trading status | USED | Real-time session status |
| GET /v2/market/holidays | V2 | Market holidays | USED | Calendar population |
| GET /v2/market/timings/{date} | V2 | Session timings | USED | Pre/post market boundaries |
| GET /v2/market/oi | V2 | OI distribution per underlying | **IMPLEMENTED** | `fetch_oi_data()` — May 2026 |
| GET /v2/market/pcr | V2 | Put-Call Ratio time-series | **IMPLEMENTED** | `fetch_pcr_data()` — May 2026 |
| GET /v2/market/max-pain | V2 | Max Pain analysis | **IMPLEMENTED** | `fetch_max_pain()` — May 2026 |
| GET /v2/market/change-oi | V2 | Change in OI | **IMPLEMENTED** | `fetch_change_oi()` — May 2026 |
| GET /v2/market/fii | V2 | FII activity data | **IMPLEMENTED** | `fetch_fii_data()` — May 2026 |
| GET /v2/market/dii | V2 | DII activity data | **IMPLEMENTED** | `fetch_dii_data()` — May 2026 |
| GET /v2/market/smartlist/futures | V2 | Futures smartlist | **IMPLEMENTED** | `fetch_smartlist_futures()` — May 2026 |
| GET /v2/market/smartlist/options | V2 | Options smartlist | **IMPLEMENTED** | `fetch_smartlist_options()` — May 2026 |
| GET /v2/market/smartlist/mtf | V2 | MTF eligible stocks | **IMPLEMENTED** | `fetch_smartlist_mtf()` — May 2026 |

### Instruments

| API Route | Version | Description | Classification | Reason |
|-----------|---------|-------------|----------------|--------|
| GET /v2/instruments/search | V2 | Instrument search | NOT_IMPLEMENTED | Lower priority; static BOD file used |
| BOD Instrument JSON files | CDN | Complete instrument master | USED | `_build_instrument_record()` in `load_fno_instrument_master.py` derives ISINs and `NSE_FO|{token}` keys from Upstox BOD JSON; 35,940 mappings seeded into `instrument_provider_mapping` (2026-09-21) |

### Closing Auction (Sep 2026)

| Feature | Description | Classification | Reason |
|---------|-------------|----------------|--------|
| CAS fields in /v2/market-quote/quotes | indicative prices, quantities | USED | ClosingAuctionSnapshot table |
| CAS fields in WebSocket V3 | CAS indicative data in feed | USED | cas sub-dict in normalized tick |
| cas_eligible in instruments | Instrument CAS eligibility | NOT_IMPLEMENTED | Flag not yet read from instrument JSON |

### WebSocket V3

| Feature | Description | Classification | Reason |
|---------|-------------|----------------|--------|
| GET /v2/feed/market-data-feed/authorize | Auth URL for WSS | USED | Required per-reconnect |
| Mode: ltpc | LTP + close + timestamps | USED | Lightweight streaming |
| Mode: option_greeks | Greeks + OI + LTP | USED | F&O analytics streaming |
| Mode: full | Complete data + depth-5 | USED | Primary live feed |
| Mode: full_d30 (Plus) | Complete data + depth-30 | USED | High-frequency depth (Plus plan) |

### Deprecated / Removed

| API Route | Status | Action Taken |
|-----------|--------|-------------|
| V2 WebSocket URL (wss://...v2/feed/...) | DISCONTINUED (Aug 2025) | Removed from codebase; V3 authorized URL flow implemented |
| ~~GET /v2/historical-candle/...~~ | ~~V2~~ | ~~Historical candle (deprecated)~~ | ~~DEPRECATED~~ | All production code migrated to V3; backward-compat alias in tests only |
| GET /v2/market-quote/ltp | V2 | LTP (deprecated) | DEPRECATED | All calls migrated to V3 |
| GET /v2/market-quote/ohlc | V2 | OHLC (deprecated) | DEPRECATED | All calls migrated to V3 |

---

## NSE BHAVCOPY (new in v2.2.0)

The 5-year backfill campaign (2026-09-21) introduced a direct NSE Bhavcopy loader for F&O daily candles. This is a standalone data source, not a REST provider, but is listed here for completeness.

| Source | Description | Classification | Tables Written |
|--------|-------------|----------------|----------------|
| NSE Bhavcopy (pre-2024 via `pybhav`) | F&O daily OHLCV files | USED | `futures_candle`, `options_candle` |
| NSE Bhavcopy (2024+ direct URL) | `BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | USED | `futures_candle`, `options_candle` |

**Coverage:** Sep 2021 → live. 246,986 futures rows, 242,255 options rows. Continuous futures series (`continuous_futures` table) computed from front-month roll logic over bhavcopy data.

---

## UNEXPLAINED UNUSED APIS

The following documented Angel One APIs are not implemented. Explanations are provided for each:

| API | Classification | Explanation |
|----|----------------|-------------|
| BSE intraday (bseIntraday) | NOT_IMPLEMENTED | Low priority; BSE data available from Upstox |
| searchScrip | NOT_IMPLEMENTED | Instrument lookup; static instrument_master table used instead |
| Logout | NOT_IMPLEMENTED | Server-side service; JWT expiry via TTL is sufficient |

The following Upstox APIs are not implemented:

| API | Classification | Explanation |
|----|----------------|-------------|
| ~~Market Info APIs (May 2026)~~ | **IMPLEMENTED** | All 6 APIs implemented 2026-09-17 (`fetch_oi_data`, `fetch_pcr_data`, etc.) |
| cas_eligible in instruments | NOT_IMPLEMENTED | CAS eligibility useful but not blocking |
| Instrument Search (V2) | NOT_IMPLEMENTED | `_UPSTOX_INSTRUMENT_KEYS` map (301 symbols) + BOD JSON used instead |
| Orders, Holdings, Portfolio | NOT_NEEDED | Not a market data service function |
| Logout | NOT_IMPLEMENTED | Server-side; token expiry handled by TTL |

---

*Last updated: 2026-09-22. Zero unexplained unused relevant APIs. NSE_INDEX intraday routing fix (UDAPI100011) documented. 301 NSE_EQ|ISIN keys and NSE Bhavcopy loader added.*
