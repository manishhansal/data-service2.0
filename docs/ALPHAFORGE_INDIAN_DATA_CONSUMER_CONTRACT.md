# ALPHAFORGE INDIAN MARKET DATA CONSUMER CONTRACT
**Date:** 2026-09-13  
**Derived from:** Actual source code inspection  
**Status:** VERIFIED (code-traced, not assumed)

---

## Overview

This document specifies the exact data AlphaForge needs from the Indian Market Data Service. Every row was derived by tracing actual code paths. This document is the **authoritative input specification** for data-service2.0's Indian Market API.

---

## 1. Instruments

### 1.1 Instrument Master (NSE EQ + F&O)

| Field | Value |
|---|---|
| Consumer | `instrument-master.service.ts`, `fno-symbols.ts`, backfill orchestrator |
| Exchange | NSE (EQ), NFO (FO) |
| Instrument types | EQ, IDX, OPTIDX, OPTSTK, FUTIDX, FUTSTK, ETF |
| Required fields | `token`, `tradingSymbol`, `name`, `exchange`, `segment`, `instrumentType`, `lotSize`, `expiry` (nullable), `strike` (nullable), `optionType` (CE/PE/null), `tickSize` |
| Live/Historical | On-demand (master dump, refreshed daily) |
| Freshness | Max 24h stale |
| Missing acceptable | No — instrument master is required to resolve tokens |
| Current API | `GET /scraping/instruments?exchange=NSE&type=EQ` |
| Required API | `GET /v1/india/instruments?exchange=NSE&type=EQ` |

### 1.2 F&O Universe

| Field | Value |
|---|---|
| Consumer | `fno-symbols.ts`, `instrument-master-universe.service.ts` |
| Symbols | 170+ F&O eligible equities + 4 indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY) |
| Required fields | `tradingSymbol`, `exchange`, `instrumentType`, `lotSize` |
| Freshness | Refreshed weekly (SEBI publishes monthly) |

---

## 2. Quotes (Live / Near-Live)

### 2.1 Single Quote

| Field | Value |
|---|---|
| Consumer | Signal engine, scanner, daily picks, sector analysis |
| Symbols | NSE equities, NSE indices |
| Required fields | `symbol`, `ltp`, `change`, `changePct`, `prevClose`, `open`, `high`, `low`, `volume`, `provider`, `fetchedAt` |
| Optional fields | `oi`, `totalBuyQty`, `totalSellQty`, `upperCircuit`, `lowerCircuit` |
| Freshness | < 5s during REGULAR session |
| Live/Historical | Live |
| F&O required | No (quotes are cash market) |
| Current API | `GET /scraping/quotes?symbols=RELIANCE,NIFTY` |
| Required API | `GET /v1/india/quotes/{symbol}` or `GET /v1/india/quotes?symbols=A,B,C` |

### 2.2 Batch Quotes

| Field | Value |
|---|---|
| Consumer | Scanner (170 symbols), daily picks, sector-stocks |
| Batch size | Up to 170 symbols per request |
| Required fields | Same as single quote |
| Freshness | < 15s |
| Current API | `GET /scraping/quotes?symbols=SYM1,SYM2,...` |
| Required API | `GET /v1/india/quotes/batch?symbols=SYM1,SYM2,...` |

### 2.3 Live Tick Stream (WebSocket / SSE)

| Field | Value |
|---|---|
| Consumer | Worker realtime candle builder, browser feed, scalper |
| Symbols | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, top liquid F&O equities |
| Required fields | `token`, `symbol`, `exchange`, `ltp`, `change`, `changePct`, `volume`, `oi`, `exchangeTimestampMs`, `receivedAtMs`, `provider` |
| Freshness | Exchange-fresh (< 1s for REGULAR session) |
| Protocol | WebSocket preferred; SSE polling fallback |
| Current path | Angel One SmartStream direct WS → candle builder |
| Required path | data-service2.0 WebSocket endpoint → candle builder |

---

## 3. Historical OHLCV Candles

### 3.1 Intraday Candles

| Attribute | Value |
|---|---|
| Consumer | Signal engine (all strategies), ML features, backtester, scanner, charts |
| Instruments | NSE equities, NSE indices, NFO futures, NFO options |
| Intervals supported | `1m`, `5m`, `10m`, `15m`, `30m`, `1h` |
| Intervals NOT supported | `3m` (permanently removed; DB CHECK constraint enforces this) |
| Date range | Up to 2 years intraday history |
| Required fields | `time` (epoch seconds UTC), `open`, `high`, `low`, `close`, `volume` |
| Optional fields | `oi` (NFO only; NULL when unavailable, never 0-fabricated) |
| Null semantics | `oi=NULL` means not available; `oi=0` means genuinely zero |
| Freshness | For live session: < 60s for most recent bar |
| Market session | NSE regular (09:15–15:30 IST) |
| Current API | `GET /scraping/historical?symbol=NIFTY&exchange=NSE&interval=5m&from=...&to=...` |
| Required API | `GET /v1/india/historical?symbol=NIFTY&exchange=NSE&interval=5m&from=...&to=...` |

### 3.2 Daily Candles

| Attribute | Value |
|---|---|
| Instruments | NSE equities, NSE indices, NFO futures (for OI) |
| Intervals | `1d`, `1w`, `1M` |
| Date range | Up to 10 years (`1d`), 5 years (`1w`), full history (`1M`) |
| Required fields | `time`, `open`, `high`, `low`, `close`, `volume`, `oi` (NFO) |
| Consumer | ML training, backtester, long-term signal generation |
| Current API | `GET /scraping/historical?interval=1d` |
| Required API | `GET /v1/india/historical?interval=1d` |

### 3.3 Candle Response Contract

```typescript
interface OHLCVCandle {
  time: number;           // Unix epoch seconds, UTC
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;         // 0 when genuinely unavailable (volumeUnavailable=true)
  oi?: number | null;     // NULL = not available; only set for F&O instruments
  volumeUnavailable?: boolean;  // true when volume is placeholder-0
}
```

**Invariants AlphaForge already enforces:**
- `high >= max(open, close)`
- `low <= min(open, close)`  
- `open > 0, high > 0, low > 0, close > 0`
- Timestamps strictly ascending within a series
- No duplicate timestamps

---

## 4. Option Chain

### 4.1 Option Chain Snapshot

| Attribute | Value |
|---|---|
| Consumer | Scalper, signal engine, daily picks, AI signals, OI analysis |
| Underlyings | NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY + F&O eligible equities |
| Expiry | Nearest (default) or specific ISO date |
| Required fields (row) | `strike`, `ce.ltp`, `ce.oi`, `ce.volume`, `pe.ltp`, `pe.oi`, `pe.volume` |
| Optional fields | `ce.iv`, `ce.delta`, `ce.gamma`, `ce.theta`, `ce.vega`, `ce.bidPrice`, `ce.askPrice`, `pe.*` equivalents |
| Spot price | Required in response (`spot`) |
| Analytics | `pcr`, `maxPain`, `atmIV` expected in metadata |
| Freshness | < 30s during REGULAR session |
| Current API | `GET /scraping/option-chain?underlying=NIFTY&expiry=2024-01-25` |
| Required API | `GET /v1/india/option-chain?underlying=NIFTY&expiry=2024-01-25` |

### 4.2 Option Chain Response Contract

```typescript
interface OptionChain {
  underlying: string;
  spot: number | null;
  expiry: string;           // ISO date YYYY-MM-DD
  expiries: string[];       // All available expiries
  rows: OptionChainRow[];
  analytics: OptionChainAnalytics;
  provider: string;
  fetchedAt: string;        // ISO UTC
  marketStatus?: string;    // REGULAR | CLOSED | PRE_OPEN | POST_MARKET
}
interface OptionChainRow {
  strike: number;
  ce: OptionContract | null;
  pe: OptionContract | null;
}
interface OptionContract {
  ltp: number | null;
  oi: number | null;
  volume: number | null;
  iv: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  bidPrice: number | null;
  askPrice: number | null;
}
```

---

## 5. Market Status

| Attribute | Value |
|---|---|
| Consumer | Signal engine gate, candle builder session check, worker scheduling |
| Required fields | `sessionPhase` (REGULAR/PRE_OPEN/POST_MARKET/CLOSED), `tradingDay`, `nextTradingDay`, `nextSessionChange` |
| Freshness | < 60s |
| Current API | None in internal data-service (computed locally in AlphaForge) |
| Required API | `GET /v1/india/market/status` ✅ already exists in data-service2.0 |

---

## 6. Broker Analytics (PCR, OI Buildup, Gainers/Losers)

| Attribute | Value |
|---|---|
| Consumer | AI signals builder, daily picks builder, signal engine |
| Data | Put-Call Ratio (PCR), OI buildup (long/short), gainers/losers by OI/price |
| Provider | Angel One SmartAPI (authenticated) |
| Required fields | `pcr`, `putOI`, `callOI` for PCR; `symbol`, `oiBuildup`, `direction` for OI buildup |
| Freshness | < 5 minutes during REGULAR session |
| Current path | Direct `angel.fetchPcr()`, `angel.fetchOiBuildup()`, `angel.fetchGainersLosers()` |
| Required API | `GET /v1/india/broker-analytics/pcr`, `GET /v1/india/broker-analytics/oi-buildup`, `GET /v1/india/broker-analytics/gainers-losers` |

---

## 7. Data Quality Gate

| Attribute | Value |
|---|---|
| Consumer | Signal engine (`gate-client.ts`) — HARD BLOCK on gate failure |
| Interface | `POST /data/gate { symbol, quoteAgeMs, ... }` |
| Response | `{ signalEngineAllowed: bool, confidenceScore, quality, blockReasons }` |
| Fail behavior | Fail-closed (signalEngineAllowed=false when unreachable) |
| Current API | `POST /data/gate` ✅ already exists in data-service2.0 |

---

## 8. Historical Gap + Coverage

| Attribute | Value |
|---|---|
| Consumer | Backfill orchestrator, gap recovery service |
| Data | Gap detection, recovery status, coverage matrix |
| Current API | None in internal data-service |
| Required API | `GET /v1/india/historical/gaps`, `GET /v1/india/historical/status` ✅ already in data-service2.0 |

---

## 9. Timestamp / Timezone Contract

| Rule | Specification |
|---|---|
| All timestamps in API responses | UTC ISO-8601 (e.g. `2024-01-15T09:15:00.000Z`) |
| All candle `time` field | Unix epoch **seconds** UTC (not milliseconds) |
| All live tick `exchangeTimestampMs` | Unix epoch **milliseconds** UTC |
| IST offset | +05:30 (no DST in India) |
| Session boundaries | NSE: 09:15:00–15:30:00 IST |
| Pre-open | 09:00:00–09:15:00 IST |
| Post-market | 15:30:00–16:00:00 IST |

---

## 10. Symbol / Instrument Mapping Contract

| Rule | Specification |
|---|---|
| NSE equity symbols | Plain NSE symbol (e.g. `RELIANCE`, `HDFCBANK`) — no suffix |
| NSE index symbols | `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY` — no `^` prefix |
| NSE futures | Trading symbol format (e.g. `NIFTY25JANFUT`) |
| NSE options | Trading symbol format (e.g. `NIFTY25JAN24000CE`) |
| Exchange values | `NSE`, `NFO`, `BSE`, `BFO`, `MCX` |
| Angel One tokens | Numeric string (e.g. `"99926000"`) — internal; never exposed to AlphaForge consumers |
| Upstox instrument keys | `NSE_EQ|{ISIN}`, `NSE_INDEX|{name}`, `NSE_FO|{symbol}` — internal; never exposed |

---

## 11. Canonical Timeframe Contract

The following are the **only** valid interval values across both repositories:

| Interval | Description |
|---|---|
| `1m` | 1 minute |
| `5m` | 5 minutes |
| `10m` | 10 minutes |
| `15m` | 15 minutes |
| `30m` | 30 minutes |
| `1h` | 1 hour |
| `1d` | 1 day |
| `1w` | 1 week |
| `1M` | 1 month |

**`3m` is permanently prohibited.** The database enforces this with a CHECK constraint (`interval_str <> '3m'`). All adapters raise errors if `3m` is requested.

---

## 12. Provenance Contract

Every data response from data-service2.0 MUST include:

| Field | Description |
|---|---|
| `provider` | Source provider ID (e.g. `angel_one`, `upstox`, `jugaad_data`, `openchart`, `yahoo`) |
| `fetchedAt` | UTC ISO-8601 timestamp of data acquisition |
| `dataSourceType` | `LIVE`, `HISTORICAL`, `CACHED`, `DERIVED` |
| `quality` | Optional quality metadata (valid/degraded/poor) |

---

## 13. Error Semantics Contract

| Error Type | HTTP Status | Code | AlphaForge Behavior |
|---|---|---|---|
| Provider unavailable | 503 | `PROVIDER_UNAVAILABLE` | Failover to next provider |
| Auth failure | 401 | `AUTH_FAILURE` | Log + failover |
| Rate limit | 429 | `RATE_LIMITED` | Wait + retry with backoff |
| Invalid symbol | 400 | `INVALID_SYMBOL` | Return null/empty |
| Unsupported interval | 400 | `INTERVAL_NOT_SUPPORTED` | Return error to consumer |
| Market closed (live data) | 200 | `marketStatus: CLOSED` | Return last known with status |
| No data for range | 200 | Empty candles array `[]` | Treat as gap |
| Fabricated data | NEVER | — | **PROHIBITED** |

---

## 14. Freshness Thresholds Used in AlphaForge

| Data Type | Max Acceptable Age | Source |
|---|---|---|
| Live quote (REGULAR session) | 5s | `health.ts` STALE_THRESHOLDS |
| Live tick | 60s | `health.ts` isTickStale |
| Intraday candle (active bar) | 30s | Cache TTL |
| Daily candle | 4 hours | Cache TTL |
| Option chain | 15s | Cache TTL |
| Instrument master | 12 hours | Cache TTL |
| Data quality gate | 3s | gate-client cache |
