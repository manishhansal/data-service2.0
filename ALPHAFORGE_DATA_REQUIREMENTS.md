# AlphaForge Data Requirements — DATA-SERVICE 2.0

> **Document status:** Production baseline  
> **Platform version:** 2.0.0  
> **Last updated:** 2026-01-15  
> **Spec references:** Requirements 21.1, 21.2, 21.3

This document is the primary reference for AlphaForge engineers integrating with DATA-SERVICE 2.0. It covers every endpoint, data shape, null-semantic rule, and migration obligation that AlphaForge consumers must know.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Base URLs and Connectivity](#2-base-urls-and-connectivity)
3. [Authentication](#3-authentication)
4. [Response Envelopes](#4-response-envelopes)
5. [Indian Market Data (NSE/NFO)](#5-indian-market-data-nsenfo)
   - 5.1 [Live Quotes](#51-live-quotes)
   - 5.2 [Option Chain](#52-option-chain)
   - 5.3 [Historical OHLCV](#53-historical-ohlcv)
   - 5.4 [Gap Recovery Status](#54-gap-recovery-status)
   - 5.5 [Reconciliation Statistics](#55-reconciliation-statistics)
   - 5.6 [Historical Coverage Status](#56-historical-coverage-status)
   - 5.7 [Market Status and Session](#57-market-status-and-session)
   - 5.8 [Instrument Master](#58-instrument-master)
   - 5.9 [F&O Universe](#59-fo-universe)
   - 5.10 [Broker Analytics (Angel One)](#510-broker-analytics-angel-one)
6. [Crypto Market Data (Binance/Deribit)](#6-crypto-market-data-binancederibit)
   - 6.1 [Binance OHLCV Klines](#61-binance-ohlcv-klines)
   - 6.2 [Binance Ticker](#62-binance-ticker)
   - 6.3 [Binance 24h Stats](#63-binance-24h-stats)
   - 6.4 [Binance Futures Overview](#64-binance-futures-overview)
   - 6.5 [Deribit Options Overview](#65-deribit-options-overview)
   - 6.6 [Deribit Instruments List](#66-deribit-instruments-list)
   - 6.7 [Deribit Index Price](#67-deribit-index-price)
   - 6.8 [Deribit Ticker](#68-deribit-ticker)
   - 6.9 [Deribit OHLCV](#69-deribit-ohlcv)
7. [Data Quality](#7-data-quality)
8. [Null Semantics — Critical Rules](#8-null-semantics--critical-rules)
9. [Prohibited Intervals — Indian Market](#9-prohibited-intervals--indian-market)
10. [WebSocket Streaming](#10-websocket-streaming)
11. [Provider Health](#11-provider-health)
12. [Data Lineage and Provenance](#12-data-lineage-and-provenance)
13. [Rate Limits](#13-rate-limits)
14. [Error Format](#14-error-format)
15. [HTTP Status Code Reference](#15-http-status-code-reference)
16. [Cache-Control Headers](#16-cache-control-headers)
17. [AlphaForge Consumer Feature Matrix](#17-alphaforge-consumer-feature-matrix)
18. [AlphaForge Migration Obligations](#18-alphaforge-migration-obligations)
19. [Changelog — 2.0 vs Prior Versions](#19-changelog--20-vs-prior-versions)

---

## 1. Overview

DATA-SERVICE 2.0 is the **single market-data authority** for AlphaForge and all future consumers. It centralises all external market-data acquisition, normalisation, validation, caching, persistence, and streaming.

**AlphaForge MUST NOT call any external provider directly.** Every request for market data must flow through DATA-SERVICE 2.0.

### What the platform guarantees

| Guarantee | Detail |
|---|---|
| Canonical schema | Every response uses a typed, validated Pydantic schema; no provider-specific types leak out |
| Provenance tracking | Every observation carries a UUID and a full provenance chain |
| Quality scoring | Every dataset has a `DataConfidenceScore` in `[0, 95]` and a `DataQualityGate` |
| Null integrity | Absent values are `null` — never `0`, never fabricated from a different field |
| No 3m Indian data | The `3m` interval is permanently blocked for NSE/NFO data at every layer |
| No look-ahead | Backtest mode enforces `availableAtMs ≤ T` for every returned record |

### Supported market verticals

| Vertical | Instruments | Primary providers |
|---|---|---|
| Indian Markets | NSE equities, F&O (equity + index derivatives), NSE indices | Scrapling/NSE, Angel One SmartAPI, Upstox V2/V3, Jugaad-data, OpenChart |
| Crypto Markets | BTC, ETH, SOL — spot, perpetual futures, options | Binance REST/WebSocket, Deribit REST |

---

## 2. Base URLs and Connectivity

| Environment | Base URL |
|---|---|
| Internal (Docker Compose) | `http://data-service:8200` |
| Development (host) | `http://localhost:8200` |
| All API paths | Base URL + `/v1/` prefix |

> **All market data endpoints are under `/v1/`.** Breaking changes require a new `/v2/` prefix.

---

## 3. Authentication

All production API endpoints require authentication via one of two methods:

### Option A — API Key header

```http
X-API-KEY: key-consumer-alphaforge-prod
```

### Option B — JWT Bearer token

```http
Authorization: Bearer <JWT>
```

JWTs are issued by the platform at `GET /v1/auth/token?api_key=<key>` and expire after the configured `JWT_EXPIRY_SECONDS` (default: 3600 s).

### Failure responses

| Condition | Status | Error code |
|---|---|---|
| Missing credentials | 401 | `UNAUTHORIZED` |
| Invalid API key | 401 | `INVALID_API_KEY` |
| Malformed or expired JWT | 401 | `TOKEN_EXPIRED` / `INVALID_TOKEN` |

Token details and internal state are **never** disclosed in error responses.

---

## 4. Response Envelopes

### 4.1 Success envelope

All successful responses use:

```json
{
  "data": { },
  "metadata": {
    "requestedAt":    "2026-01-15T09:15:00.000Z",
    "dataAsOf":       "2026-01-15T09:14:59.750Z",
    "dataSourceType": "LIVE",
    "provider":       "angel_one",
    "provenance":     { },
    "marketStatus":   "REGULAR",
    "quality": {
      "score": 87,
      "grade": "VALID"
    }
  }
}
```

**`dataSourceType` values:**

| Value | Meaning |
|---|---|
| `LIVE` | Fresh from the provider WebSocket or REST feed |
| `HISTORICAL` | Retrieved from the candle database |
| `DERIVED` | Computed from other datasets (e.g. analytics, PCR) |
| `CACHED` | Served from L1 or L2 cache; original provider is in `provenance.sourceChain[0]` |

### 4.2 Error envelope

All error responses use:

```json
{
  "error": {
    "code":         "INTERVAL_NOT_SUPPORTED",
    "message":      "interval 3m is permanently unsupported",
    "provider":     null,
    "retryAfterMs": null,
    "requestId":    "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

- No stack traces, no credentials, no internal file paths in any error response.
- `requestId` is always a non-empty UUID v4.
- Error responses always include `Cache-Control: no-store`.

---

## 5. Indian Market Data (NSE/NFO)

### 5.1 Live Quotes

```
GET /v1/india/quotes/{symbol}?exchange=NSE
```

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `symbol` | `string` (path) | — | Trading symbol, e.g. `NIFTY`, `RELIANCE` |
| `exchange` | `string` | `NSE` | Exchange: `NSE`, `NFO`, `BSE`, `BFO` |

**Response `data` schema:**

```json
{
  "instrumentId":  "NSE:NIFTY:IDX",
  "symbol":        "NIFTY",
  "exchange":      "NSE",
  "ltp":           22150.50,
  "open":          22100.00,
  "high":          22200.00,
  "low":           22080.00,
  "prevClose":     22105.25,
  "change":        45.25,
  "changePct":     0.205,
  "volume":        1234567,
  "oi":            null,
  "tradedValue":   56789012345.00,
  "totalBuyQty":   980000,
  "totalSellQty":  1020000,
  "upperCircuit":  null,
  "lowerCircuit":  null,
  "weekHigh52":    23000.00,
  "weekLow52":     19500.00,
  "lastTradeTime": "2026-01-15T09:14:59.000Z",
  "bid":           22150.00,
  "ask":           22151.00,
  "marketStatus":  "REGULAR",
  "provenance":    { }
}
```

**Important field rules:**

- `oi` — `null` for equity and index instruments; populated only for F&O instruments. **Never populated from `tradedValue`.**
- `bid` / `ask` — `null` when the provider does not supply them. Never `0`.
- `marketStatus` — reflects the actual session phase; never HTTP 5xx when the market is closed.

**Behaviour when market is closed:**
Returns the last available quote (≤ 24 hours old) with `marketStatus: "CLOSED"`. Does not increment circuit-breaker counters.

---

### 5.2 Option Chain

```
GET /v1/india/option-chain?underlying=NIFTY&expiry=2026-01-30&exchange=NSE
```

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `underlying` | `string` | ✓ | Underlying symbol, e.g. `NIFTY`, `BANKNIFTY`, `FINNIFTY` |
| `expiry` | `string` | — | ISO-8601 date (`YYYY-MM-DD`). Defaults to nearest expiry |
| `exchange` | `string` | — | Default: `NSE` |

**Response `data` schema:**

```json
{
  "underlying":    "NIFTY",
  "expiry":        "2026-01-30",
  "chainQuality":  "LIVE",
  "spotAgeMs":     1250,
  "pcrOi":         0.92,
  "pcrVolume":     0.87,
  "maxCeOiStrike": 22500,
  "maxPeOiStrike": 22000,
  "totalCeOi":     12500000,
  "totalPeOi":     11500000,
  "atmIv":         14.5,
  "maxPain":       22200,
  "rows": [
    {
      "strike":          22000,
      "optionType":      "CE",
      "ltp":             310.50,
      "bid":             310.00,
      "ask":             311.00,
      "oi":              2500000,
      "oiChange":        50000,
      "volume":          450000,
      "tradedValue":     139725000000,
      "iv":              14.8,
      "delta":           0.42,
      "gamma":           0.0012,
      "theta":          -8.5,
      "vega":            18.2,
      "rho":             0.05,
      "oiMissing":       false,
      "oiChangeMissing": false,
      "volumeMissing":   false,
      "ivMissing":       false
    }
  ]
}
```

**Null/missing flag rules for option chain fields:**

| Field | Rule |
|---|---|
| `iv` | `null` + `ivMissing: true` when provider does not supply it. **Zero is never a substitute.** |
| `delta`, `gamma`, `theta`, `vega`, `rho` | `null` when not provided. **Placeholder zeros are prohibited.** |
| `bid`, `ask` | `null` when not provided. **Never `0`.** |
| `oi` | `null` + `oiMissing: true` when absent. **Never populated from `tradedValue`.** |
| `oiChange` | `null` + `oiChangeMissing: true` when absent |

**`chainQuality` values:**

| Value | Condition |
|---|---|
| `LIVE` | Spot price is ≤ 60 seconds old |
| `DEGRADED` | Spot price is > 60 seconds old; `spotAgeMs` is included in the response |

**Closed/no-data conditions (never HTTP 5xx):**

| Condition | Response |
|---|---|
| Market is `CLOSED` | `rows: []`, `marketStatus: "CLOSED"`, HTTP 200 |
| No contracts listed for the underlying/expiry | `rows: []`, `marketStatus: "NO_DATA"`, HTTP 200 |

---

### 5.3 Historical OHLCV

```
GET /v1/india/historical?symbol=RELIANCE&exchange=NSE&interval=1d&from=2025-01-01&to=2026-01-01
```

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `symbol` | `string` | ✓ | Trading symbol, e.g. `RELIANCE`, `NIFTY` |
| `exchange` | `string` | — | Default: `NSE` |
| `interval` | `string` | — | Default: `1d`. See [Canonical Timeframes](#canonical-timeframes-indian-markets) below |
| `from` | `string` | — | UTC ISO-8601 start (inclusive). Defaults to minimum history depth for interval |
| `to` | `string` | — | UTC ISO-8601 end (exclusive). Defaults to now |

#### Canonical Timeframes — Indian Markets

`1m`, `5m`, `10m`, `15m`, `30m`, `1h`, `1d`, `1w`, `1M`

> ⛔ **`3m` is permanently unsupported for Indian market data.** Sending `interval=3m` always returns HTTP 400. See [Section 9](#9-prohibited-intervals--indian-market).

**Minimum history depths:**

| Interval | Minimum depth |
|---|---|
| `1m` | 60 calendar days |
| `5m` – `30m` | 180 calendar days |
| `1h` | 365 calendar days |
| `1d` – `1M` | 10 years |

**Response `data`:** Array of OHLCV candle objects, maximum 10,000 per response.

```json
[
  {
    "time":               "2026-01-15T00:00:00.000Z",
    "open":               2450.00,
    "high":               2465.00,
    "low":                2440.00,
    "close":              2460.00,
    "volume":             1500000,
    "oi":                 null,
    "volumeUnavailable":  false,
    "provenance": {
      "dataObservationId":  "550e8400-e29b-41d4-a716-446655440000",
      "source":             "angel_one",
      "sourceType":         "BROKER_AUTHENTICATED",
      "normalisationVersion": "2.0.0"
    }
  }
]
```

**Response `metadata` includes:**

```json
{
  "provider":   "angel_one",
  "provenance": { },
  "quality":    { "candleCount": 250, "truncated": false },
  "gaps":       [ ],
  "dataAsOf":   "2026-01-15T00:00:00.000Z",
  "truncated":  false
}
```

- When `truncated: true`, the response contains exactly 10,000 candles and more data exists; narrow your date range.
- `gaps` lists any detected data gaps within the requested window.
- `volumeUnavailable: true` when the source did not supply a volume value; the `volume` field is `0` to distinguish from a genuine zero-volume bar.

---

### 5.4 Gap Recovery Status

```
GET /v1/india/historical/gaps?symbol=RELIANCE&interval=1m&status=PENDING&limit=100
```

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `symbol` | `string` | — | Filter by symbol (case-insensitive substring match) |
| `interval` | `string` | — | Filter by interval |
| `exchange` | `string` | — | Filter by exchange |
| `status` | `string` | — | `PENDING` \| `RECOVERING` \| `RECOVERED` \| `EXHAUSTED` |
| `limit` | `int` | `100` | Max records (1–1000) |

**Response `data`:** Array of gap records:

```json
[
  {
    "gapId":            "a1b2c3d4-...",
    "instrumentId":     "NSE:RELIANCE:EQ",
    "exchange":         "NSE",
    "intervalStr":      "1m",
    "gapStart":         1705300000000,
    "gapEnd":           1705303600000,
    "durationSec":      3600,
    "recoveryStatus":   "PENDING",
    "recoveryAttempts": 1,
    "expectedProvider": "angel_one",
    "recoveryProvider": null
  }
]
```

**`recoveryStatus` lifecycle:** `PENDING → RECOVERING → RECOVERED` or `EXHAUSTED` (after max retries, default 5).

---

### 5.5 Reconciliation Statistics

```
GET /v1/india/historical/reconciliation
```

**Response `data`:**

```json
{
  "totalCompared":  12500,
  "matched":        12200,
  "matchRatePct":   97.6,
  "distribution": {
    "CONFIRMED":         12200,
    "MINOR_DISCREPANCY": 250,
    "MAJOR_DISCREPANCY": 50
  },
  "byProviderPair": {
    "angel_one/openchart": {
      "totalCompared":  8000,
      "matched":        7900,
      "matchRatePct":   98.75,
      "distribution": { }
    }
  }
}
```

**Deviation thresholds:**

| Deviation | Status |
|---|---|
| `|A − B| / max(|A|, |B|) × 100 ≤ 0.5%` for all OHLCV fields | `CONFIRMED` |
| Any field between `0.5%` and `2.0%` | `MINOR_DISCREPANCY` |
| Any field exceeds `2.0%` | `MAJOR_DISCREPANCY` → generates a `DataIncident` |

---

### 5.6 Historical Coverage Status

```
GET /v1/india/historical/status
```

**Response `data`:**

```json
{
  "supportedTimeframes": ["1m","5m","10m","15m","30m","1h","1d","1w","1M"],
  "gapSummary": {
    "PENDING":    3,
    "RECOVERING": 1,
    "RECOVERED":  450,
    "EXHAUSTED":  0
  },
  "providerActivity": {
    "angel_one":  { "lastFetchAt": "2026-01-15T09:10:00.000Z", "status": "UP" },
    "upstox":     { "lastFetchAt": "2026-01-15T09:11:00.000Z", "status": "UP" }
  },
  "reconciliationStatus": {
    "lastRunAt": "2026-01-15T09:00:00.000Z",
    "matchRatePct": 97.6
  }
}
```

---

### 5.7 Market Status and Session

```
GET /v1/india/market/status
```

**Response `data`:**

```json
{
  "sessionPhase":     "REGULAR",
  "nextSessionChange": "2026-01-15T10:00:00.000Z",
  "tradingDay":        true,
  "nextTradingDay":    "2026-01-16",
  "holidays":          ["2026-01-26"],
  "calendarStatus":    "2026-01-01"
}
```

**`sessionPhase` values (IST boundaries, inclusive start / exclusive end):**

| Phase | IST window | Notes |
|---|---|---|
| `PRE_OPEN` | 09:00–09:08 | — |
| `PRE_OPEN_CALL_AUCTION` | 09:08–09:15 | — |
| `REGULAR` | 09:15–15:30 | Ends 13:00 on half-days |
| `POST_MARKET` | 15:30–16:00 | 13:00–13:30 on half-days |
| `CLOSED` | All other times | Weekends, full holidays |
| `MUHURAT` | Diwali only | Special Muhurat session |

- `calendarStatus` is the date of the last successful holiday calendar refresh (`YYYY-MM-DD`), or `null` if never refreshed.
- `holidays` lists remaining NSE holiday dates in the current IST calendar month.

---

### 5.8 Instrument Master

```
GET /v1/instruments?exchange=NSE&instrumentType=OPTIDX&underlying=NIFTY&expiry=2026-01-30
GET /v1/instruments/{instrumentId}
GET /v1/instruments/{instrumentId}?include=providerTokens
```

**Filter parameters for list endpoint:**

| Parameter | Values |
|---|---|
| `exchange` | `NSE`, `NFO`, `BSE`, `BFO` |
| `instrumentType` | `EQ`, `FUTIDX`, `FUTSTK`, `OPTIDX`, `OPTSTK`, `ETF`, `IDX` |
| `underlying` | e.g. `NIFTY`, `BANKNIFTY`, `RELIANCE` |
| `segment` | `EQ`, `FO`, `CD`, `COM` |
| `expiry` | ISO-8601 date `YYYY-MM-DD` |

Returns empty list with HTTP 200 when no instruments match.

**Response `data` (single instrument):**

```json
{
  "instrumentId":   "NSE:NIFTY25JANFUT:FUTIDX",
  "tradingSymbol":  "NIFTY25JANFUT",
  "displaySymbol":  "NIFTY Jan 2025 Fut",
  "isin":           null,
  "exchange":       "NFO",
  "segment":        "FO",
  "instrumentType": "FUTIDX",
  "underlying":     "NIFTY",
  "expiry":         "2025-01-30",
  "strike":         null,
  "optionType":     null,
  "lotSize":        50,
  "tickSize":       0.05,
  "activeFrom":     "2024-07-01",
  "activeTo":       null
}
```

- Provider tokens (`angelToken`, `angelSymbol`, `upstoxKey`, `upstoxSymbol`) are **excluded by default**. Pass `?include=providerTokens` to receive them.
- `activeTo: null` means the instrument is currently active.

---

### 5.9 F&O Universe

```
GET /v1/instruments/fno-universe
GET /v1/instruments/fno-universe/history?status=ACTIVE&version=42&page=1&limit=20
```

**Current universe response `data`:**

```json
{
  "universeVersion":  42,
  "checksum":         "sha256:abcdef...",
  "generatedAt":      "2026-01-15T03:15:00.000Z",
  "effectiveFrom":    "2026-01-15",
  "effectiveTo":      null,
  "fnoEquityCount":   182,
  "fnoIndexCount":    6,
  "constituentCount": 188,
  "status":           "ACTIVE"
}
```

**Error when no snapshot loaded:**

```json
{
  "error": {
    "code":    "FNO_UNIVERSE_UNAVAILABLE",
    "message": "No F&O universe snapshot has been initialised. The snapshot is refreshed at 08:45 IST on every trading day."
  }
}
```

HTTP 503.

- The universe is refreshed at **08:45 IST** on every trading day.
- If checksum matches the current snapshot, no new snapshot is written.

---

### 5.10 Broker Analytics (Angel One)

These endpoints source data from the Angel One SmartAPI and are provided exclusively for AlphaForge features that require broker-specific analytics.

```
GET /v1/india/broker-analytics/pcr
GET /v1/india/broker-analytics/oi-buildup
GET /v1/india/broker-analytics/gainers-losers
```

All three return the standard success envelope with `provenance` and `quality` metadata.

**Failure behaviour:** If the Angel One SmartAPI call fails, the platform returns HTTP 502 with `PROVIDER_UNAVAILABLE`. Stale data is **never** returned without an explicit `stale: true` flag in the metadata.

---

## 6. Crypto Market Data (Binance/Deribit)

> **3m interval exception:** The `3m` interval is allowed for Binance crypto data. The ban applies exclusively to Indian market (NSE/NFO) data. See [Section 9](#9-prohibited-intervals--indian-market).

### 6.1 Binance OHLCV Klines

```
GET /v1/crypto/{symbol}/klines?interval=1h&from=2026-01-01&to=2026-01-15&limit=500
```

**Path parameter:** `symbol` — e.g. `BTCUSDT`, `ETHUSDT`, `SOLUSDT`

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `interval` | `string` | ✓ | Binance interval (see table below) |
| `limit` | `int` | — | Number of candles (1–1000, default 500) |
| `from` | `string` | — | UTC ISO-8601 start |
| `to` | `string` | — | UTC ISO-8601 end |

**Supported Binance intervals:**

`1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d`

> ✅ `3m` is valid here. This is a Binance native interval and the Indian-market ban does not apply.

**Response `data`:** Array of candle objects:

```json
[
  {
    "openTime":  1705276800000,
    "open":      "42150.00000000",
    "high":      "42500.00000000",
    "low":       "41900.00000000",
    "close":     "42300.00000000",
    "volume":    "1234.56789000",
    "closeTime": 1705280399999
  }
]
```

All OHLC invariants are validated before persistence. Invalid candles are rejected and never returned.

---

### 6.2 Binance Ticker

```
GET /v1/crypto/{symbol}/ticker
```

**Response `data`:**

```json
{
  "symbol": "BTCUSDT",
  "price":  "42300.00000000"
}
```

---

### 6.3 Binance 24h Stats

```
GET /v1/crypto/{symbol}/stats
```

**Response `data`:** The full Binance 24hr ticker dict, including:

| Field | Description |
|---|---|
| `openPrice` | 24h open price |
| `highPrice` | 24h high |
| `lowPrice` | 24h low |
| `lastPrice` | Last trade price |
| `volume` | Base asset volume |
| `priceChange` | Absolute price change |
| `priceChangePercent` | Percentage price change |
| `weightedAvgPrice` | Volume-weighted average price |
| `count` | Number of trades in the period |

---

### 6.4 Binance Futures Overview

```
GET /v1/crypto/futures/overview
```

Returns perpetual futures data for all tracked symbols (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`).

**Response `data`:** Array of symbol objects:

```json
[
  {
    "symbol":                "BTCUSDT",
    "markPrice":             "42300.00",
    "indexPrice":            "42295.00",
    "fundingRate":           0.0001,
    "fundingRateAnnualized": 0.1095,
    "nextFundingTime":       1705305600000,
    "openInterest":          "12345.678",
    "openInterestNotionalUsd": 522040000,
    "oiChangePct1h":         0.5,
    "longShortRatio":        1.12,
    "longAccount":           0.528,
    "shortAccount":          0.472
  }
]
```

- Fields are `null` when the sub-fetch for that provider endpoint fails. The platform degrades gracefully per symbol.
- `fundingRateAnnualized` = `fundingRate × 3 × 365` (three funding periods per day).

---

### 6.5 Deribit Options Overview

```
GET /v1/deribit/{currency}/overview
```

**Supported currencies:** `BTC`, `ETH`, `SOL`

**Response `data`:**

```json
{
  "currency":         "BTC",
  "underlyingPrice":  42300.0,
  "totalCallOi":      125000,
  "totalPutOi":       115000,
  "totalCallVolume":  8500,
  "totalPutVolume":   7800,
  "pcrOi":            0.92,
  "pcrVolume":        0.917,
  "atmIv":            55.3,
  "expiryStats": [
    {
      "expiry":        "2026-01-30",
      "daysToExpiry":  15,
      "maxPainStrike": 42000,
      "pcrOi":         0.88,
      "pcrVolume":     0.85,
      "topStrikes":    [42000, 42500, 43000, 41500, 41000]
    }
  ],
  "computed_at": "2026-01-15T09:15:00.000Z"
}
```

**Null rules for this endpoint:**

- `pcrOi` — `null` when total call OI is zero or null. **Never `0` or `∞`.**
- `pcrVolume` — `null` when total call volume is zero or null.
- `atmIv` — `null` when no contract with non-null IV is available near the ATM strike.

**Error — unsupported currency:**

```json
{
  "error": {
    "code":    "CURRENCY_NOT_SUPPORTED",
    "message": "Currency 'XRP' is not supported. Supported currencies: BTC, ETH, SOL."
  }
}
```

HTTP 400.

---

### 6.6 Deribit Instruments List

```
GET /v1/deribit/{currency}/instruments?kind=option
```

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `kind` | `option` | Instrument kind: `option`, `future`, `spot` |

Returns raw Deribit instrument list for the currency.

---

### 6.7 Deribit Index Price

```
GET /v1/deribit/{currency}/index-price
```

**Response `data`:**

```json
{
  "index_name":  "btc_usd",
  "index_price": 42300.0
}
```

**Currency → index name mapping:**

| Currency | Index name |
|---|---|
| `BTC` | `btc_usd` |
| `ETH` | `eth_usd` |
| `SOL` | `sol_usd` |

---

### 6.8 Deribit Ticker

```
GET /v1/deribit/ticker/{instrument_name}
```

**Examples:** `BTC-27DEC24-100000-C`, `BTC-PERPETUAL`

**Response `data`:** Raw Deribit ticker dict. **Critical null semantics apply:**

| Field | Rule |
|---|---|
| `mark_iv` | `null` when Deribit does not supply it. **Never `0`.** |
| `open_interest` | `null` when not supplied. **Never `0`.** |
| `best_bid_price` | `null` when not supplied. **Never `0`.** |
| `best_ask_price` | `null` when not supplied. **Never `0`.** |
| `underlying_price` | `null` when not supplied. **Never `0`.** |

---

### 6.9 Deribit OHLCV

```
GET /v1/deribit/ohlcv/{instrument_name}?resolution=60&start_ts=1705276800000&end_ts=1705363200000
```

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `resolution` | `string` | ✓ | Minutes as string or `1D`. Supported: `1`, `3`, `5`, `10`, `15`, `30`, `60`, `120`, `180`, `360`, `720`, `1D` |
| `start_ts` | `int` | ✓ | UTC epoch milliseconds |
| `end_ts` | `int` | ✓ | UTC epoch milliseconds (must be > `start_ts`) |

**Response `data`:** Array of `{time, open, high, low, close, volume}` objects with `time` as UTC epoch milliseconds.

---

## 7. Data Quality

Every market data response includes quality metadata. AlphaForge signal engines must check `signalEngineAllowed` before acting on data.

### 7.1 Quality Gate Evaluation

```
POST /v1/quality/evaluate?min_confidence_score=60
```

**Request body:**

```json
{
  "symbol":          "RELIANCE",
  "timestamp":       1705300000000,
  "open":            2450.0,
  "high":            2460.0,
  "low":             2440.0,
  "close":           2455.0,
  "volume":          1000000,
  "source":          "angel_one",
  "confidenceScore": 85
}
```

**Query parameter:** `min_confidence_score` — Minimum score to pass (30–95, default 60).

**Response `data`:**

```json
{
  "gate": {
    "dataFresh":            true,
    "dataComplete":         true,
    "dataTimestampValid":   true,
    "dataProviderHealthy":  true,
    "dataSemanticallyValid": true,
    "signalEngineAllowed":  true,
    "confidenceScore":      87,
    "blockReasons":         [],
    "gates": { }
  },
  "classification": {
    "grade":               "HIGH",
    "signalEngineAllowed": true,
    "score":               87,
    "reasons":             []
  },
  "signalEngineAllowed": true
}
```

### 7.2 Quality Score Lookup

```
GET /v1/quality/score?freshness=FRESH&completeness=100&provider_healthy=true&timestamp_valid=true&agreement=1.0
```

### 7.3 DataConfidenceScore

The `DataConfidenceScore` is computed as a weighted sum of five components:

| Component | Weight | Max contribution |
|---|---|---|
| Freshness | 35% | 35 |
| Completeness | 25% | 25 |
| Provider health | 20% | 20 |
| Timestamp validity | 10% | 10 |
| Cross-source agreement | 10% | 10 |

**The score is always in `[0, 95]`. It can never reach 100** — this is intentional and reflects inherent market-data uncertainty.

If sequence integrity is broken, a 20% penalty is applied: `score = int(score × 0.8)`.

### 7.4 Quality grades and `signalEngineAllowed`

| Grade | Score range | `signalEngineAllowed` | Meaning |
|---|---|---|---|
| `HIGH` / `VALID` | ≥ 80 | `true` (if all gates pass) | Data is fresh, complete, and semantically valid |
| `MEDIUM` / `DEGRADED` | 50–79 | Depends on all 5 gates | Data quality is reduced; use with caution |
| `LOW` | 30–49 | `false` | Data quality is poor; signal generation not recommended |
| `BLOCKED` | < 30 | `false` always | Data is critically degraded; signal generation is hard-blocked |

> **`signalEngineAllowed = true` if and only if all five gate conditions are `true` AND `confidenceScore ≥ min_confidence_score`.** A `BLOCKED` score (`< 30`) prevents signal generation with no exceptions.

### 7.5 Freshness thresholds

During `REGULAR` session:

| Instrument tier | FRESH threshold |
|---|---|
| Index (NIFTY, BANKNIFTY, etc.) | ≤ 10 seconds |
| F&O liquid instruments | ≤ 10 seconds |
| F&O normal instruments | ≤ 15 seconds |
| Equity instruments | ≤ 30 seconds |

Outside `REGULAR` session (extended windows):

| Instrument tier | FRESH threshold |
|---|---|
| Index | ≤ 60 seconds |
| F&O liquid | ≤ 60 seconds |
| F&O normal | ≤ 90 seconds |
| Equity | ≤ 120 seconds |

---

## 8. Null Semantics — Critical Rules

These rules are correctness invariants enforced at every layer of the platform (Normaliser, semantic validator, persistence, API). AlphaForge consumers must handle `null` correctly.

| Field | Rule | Violation |
|---|---|---|
| `oi` (open interest) | `null` + `oiMissing: true` when provider does not supply it | **Fabricating `oi` from `tradedValue` is a critical bug** |
| `tradedValue` | Total traded value in INR. Never interchangeable with `oi` | Semantic integrity failure |
| `iv` (implied volatility) | `null` when absent. Zero IV is **not** a valid substitute | IV = 0 when not provided is wrong |
| `delta`, `gamma`, `theta`, `vega`, `rho` | `null` when absent. Placeholder zeros prohibited | Greeks = 0 when not provided is wrong |
| `bid`, `ask` | `null` when not provided by the source. Placeholder zeros prohibited | bid/ask = 0 when not provided is wrong |
| `volume` | When source does not supply volume: `volume = 0` AND `volumeUnavailable: true` | Genuine zero-volume bars also have `volume = 0`; use `volumeUnavailable` to distinguish |

**Canonical null rule: `null` means the value is genuinely absent. `0` means the value is zero. These are different things.**

---

## 9. Prohibited Intervals — Indian Market

`3m` is **permanently unsupported** for all Indian market data (NSE/NFO) at every processing layer:

- API handler → HTTP 400
- Normaliser → rejects the dataset
- Acquisition planner → `ValueError` before any I/O
- Database → `CHECK (interval_str <> '3m')` constraint on `candle_bar`

**Any request to any Indian market endpoint with `interval=3m` always returns:**

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json
Cache-Control: no-store
```

```json
{
  "error": {
    "code":    "INTERVAL_NOT_SUPPORTED",
    "message": "interval 3m is permanently unsupported"
  }
}
```

> ✅ Exception: `3m` is valid for **Binance crypto endpoints** (`GET /v1/crypto/{symbol}/klines`). Binance natively supports `3m` and the ban applies only to Indian market data.

---

## 10. WebSocket Streaming

```
WS /v1/stream/ticks
```

### 10.1 Connection

Connect with a valid API key or JWT. The connection receives heartbeat frames every 10 seconds.

### 10.2 Control messages (client → server)

**Subscribe:**

```json
{"action": "subscribe", "symbols": ["NIFTY", "BANKNIFTY"], "market": "india"}
```

**Unsubscribe:**

```json
{"action": "unsubscribe", "symbols": ["NIFTY"]}
```

### 10.3 Server frames (server → client)

**Acknowledgement:**

```json
{"type": "ack", "action": "subscribe", "symbols": ["NIFTY", "BANKNIFTY"]}
```

**Heartbeat (every 10 seconds):**

```json
{"type": "heartbeat", "timestamp": "2026-01-15T09:15:00.000Z"}
```

**Error:**

```json
{"type": "error", "code": "MAX_SUBSCRIPTIONS_EXCEEDED", "message": "..."}
```

**Tick delivery:**

```json
{
  "tickId":       "550e8400-e29b-41d4-a716-446655440000",
  "instrumentId": "NSE:NIFTY:IDX",
  "symbol":       "NIFTY",
  "exchange":     "NSE",
  "eventTimeMs":  1705300000123,
  "receivedAtMs": 1705300000234,
  "ltp":          22150.50,
  "change":       45.25,
  "changePct":    0.20,
  "volume":       1234567,
  "oi":           null,
  "tradedValue":  5678901234,
  "source":       "angel_one",
  "quality":      0.92,
  "isDuplicate":  false
}
```

### 10.4 Connection lifecycle rules

| Rule | Detail |
|---|---|
| Heartbeat timeout | If no heartbeat response for 3 consecutive beats within a 30-second window, the server closes the connection |
| Max subscriptions | 500 concurrent symbol tick streams platform-wide; exceeded → `MAX_SUBSCRIPTIONS_EXCEEDED` error frame |
| Cleanup on close | All symbol subscriptions are released within 5 seconds of connection close |
| Duplicate ticks | Published with `isDuplicate: true` rather than silently dropped (audit trail) |

### 10.5 Stream status

```
GET /v1/stream/status
```

```json
{
  "subscribedSymbols":  12,
  "activeConnections":  4,
  "ticksPublished":     84321,
  "validationFailures": {"schema_validation": 2},
  "lastPublishedAt":    "2026-01-15T09:15:00.000Z",
  "brokerConnections": {
    "angelOne": true,
    "upstox":   false,
    "binance":  true
  },
  "timestamp": "2026-01-15T09:15:01.000Z"
}
```

---

## 11. Provider Health

```
GET /v1/providers/health
```

Returns per-provider, per-capability health:

```json
{
  "providers": {
    "angel_one": {
      "live_quote": {
        "status":             "UP",
        "circuitState":       "CLOSED",
        "availability":       0.998,
        "latencyP50Ms":       85,
        "latencyP99Ms":       420,
        "errorRate":          0.002,
        "lastSuccessAt":      "2026-01-15T09:14:58.000Z",
        "lastFailureReason":  null,
        "semanticIntegrity":  true
      }
    }
  }
}
```

**`status` values:** `UP`, `DOWN`, `DEGRADED`, `UNKNOWN`  
**`circuitState` values:** `CLOSED` (normal), `OPEN` (all requests rejected), `HALF_OPEN` (probe in progress)

---

## 12. Data Lineage and Provenance

Every market data observation has an immutable provenance record.

### Single observation

```
GET /v1/lineage/{observationId}
```

Returns full lineage record within 500ms. HTTP 404 if not found.

### Instrument lineage history

```
GET /v1/lineage/instrument/{instrumentId}?limit=100
```

Returns the most recent N records ordered by `receivedAtMs` DESC. N must be 1–1000.

**Response includes:**

```json
{
  "data": [ ],
  "storeSize":     95000,
  "totalRecorded": 12500000
}
```

### Trade forensics

```
GET /v1/lineage/forensics/{tradeId}
```

Returns a joined response within 1000ms comprising the trade record, signal record, provenance record, and lineage entry. HTTP 404 with identification of the missing record if any constituent is absent.

### Read-only rule

All provenance fields are immutable. Any attempt to modify a provenance record returns HTTP 405.

---

## 13. Rate Limits

| Limit | Value |
|---|---|
| Requests per minute per consumer | 10,000 (configurable) |
| WebSocket symbol subscriptions (platform-wide) | 500 |

**Rate limit exceeded:**

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 30
```

```json
{
  "error": {
    "code":         "RATE_LIMIT_EXCEEDED",
    "message":      "Rate limit exceeded. Try again in 30 seconds.",
    "retryAfterMs": 30000
  }
}
```

---

## 14. Error Format

All error responses use the canonical envelope:

```json
{
  "error": {
    "code":         "<ErrorCode>",
    "message":      "<human-readable string>",
    "provider":     "<provider_id or null>",
    "retryAfterMs": "<milliseconds or null>",
    "requestId":    "<UUID v4>"
  }
}
```

**Complete list of error codes:**

| Code | HTTP | Meaning |
|---|---|---|
| `UNAUTHORIZED` | 401 | Missing or invalid credentials |
| `INVALID_API_KEY` | 401 | API key is not recognised |
| `TOKEN_EXPIRED` | 401 | JWT has expired |
| `INVALID_TOKEN` | 401 | JWT is malformed |
| `RATE_LIMIT_EXCEEDED` | 429 | Consumer rate limit hit |
| `NOT_FOUND` | 404 | Resource not found |
| `OBSERVATION_NOT_FOUND` | 404 | Lineage observation not found |
| `TRADE_NOT_FOUND` | 404 | Trade forensics record not found |
| `INTERVAL_NOT_SUPPORTED` | 400 | Interval is not supported (includes 3m for Indian markets) |
| `CURRENCY_NOT_SUPPORTED` | 400 | Crypto currency not in BTC/ETH/SOL |
| `RESOLUTION_NOT_SUPPORTED` | 400 | Deribit resolution not supported |
| `INVALID_PARAMETER` | 400 | Query parameter value is invalid |
| `MISSING_FIELD` | 400 | Required field absent in request |
| `INVALID_FIELD` | 400 | Field value fails validation |
| `PROVIDER_UNAVAILABLE` | 502 | All provider fallbacks exhausted |
| `PROVIDER_QUEUE_FULL` | 503 | Provider request queue overflow |
| `FNO_UNIVERSE_UNAVAILABLE` | 503 | F&O universe snapshot not yet loaded |
| `SERVICE_UNAVAILABLE` | 503 | Platform dependency (Redis/DB) not ready |
| `PROVENANCE_IMMUTABLE` | 405 | Attempt to modify a provenance record |
| `EVALUATION_ERROR` | 422 | Quality gate evaluation failed |

---

## 15. HTTP Status Code Reference

| Condition | Status |
|---|---|
| Success | 200 |
| Invalid parameter, unsupported interval, malformed date | 400 |
| Unauthenticated | 401 |
| Authenticated but unauthorised | 403 |
| Resource not found | 404 |
| Method not allowed (e.g. modifying provenance) | 405 |
| Rate limit exceeded | 429 |
| Provider failures exhaust all fallbacks | 502 |
| Platform unavailable (Redis/DB not ready), F&O universe unavailable | 503 |

---

## 16. Cache-Control Headers

| Response type | Cache-Control header |
|---|---|
| Live quote | `public, s-maxage=3, stale-while-revalidate=5` |
| Intraday candles | `public, s-maxage=30, stale-while-revalidate=60` |
| Daily candles | `public, s-maxage=300` |
| Option chain | `public, s-maxage=15, stale-while-revalidate=20` |
| All error responses | `no-store` |

---

## 17. AlphaForge Consumer Feature Matrix

The table below maps every AlphaForge consumer feature to its data dependencies and the corresponding DATA-SERVICE 2.0 endpoints. This replaces all direct provider calls listed in [Section 18](#18-alphaforge-migration-obligations).

| Consumer Feature | Data Types | Instruments | Canonical Intervals | Platform Endpoints |
|---|---|---|---|---|
| **India Scalping** | LTP, OI, Option Chain, Ticks | NIFTY, BANKNIFTY, F&O equities | `1m`, `5m` | `GET /v1/india/quotes/{symbol}`, `GET /v1/india/option-chain`, `WS /v1/stream/ticks` |
| **India Daily Picks** | OHLCV, OI, Option Chain, Greeks, Sentiment | F&O universe + indices | `1d`, `5m`, `15m` | `GET /v1/india/historical`, `GET /v1/india/option-chain` |
| **India Expiry Trades** | Option Chain, OI, IV, Greeks | Index derivatives (NIFTY, BANKNIFTY, FINNIFTY) | `5m`, `15m` | `GET /v1/india/option-chain`, `GET /v1/india/historical` |
| **India Options Workbench** | Option Chain, OI, IV, Greeks, bid/ask, PCR | Index + equity options | `live` | `GET /v1/india/option-chain`, `GET /v1/india/broker-analytics/pcr` |
| **India F&O Trend History** | OHLCV with OI, EOD | All F&O underlyings | `1d` | `GET /v1/india/historical?interval=1d` |
| **India Best Time** | OHLCV, Volume, Session stats | F&O equities + indices | `5m`, `1h` | `GET /v1/india/historical`, `GET /v1/india/market/status` |
| **India Paper Trading** | LTP, OI, ticks, signal gate | Signal-specific | `1m`, `5m` | `GET /v1/india/quotes/{symbol}`, `WS /v1/stream/ticks`, `POST /v1/quality/evaluate` |
| **Crypto Futures Overview** | Mark price, Funding rate, OI, L/S ratio | BTC, ETH, SOL | `live` | `GET /v1/crypto/futures/overview` |
| **Crypto Options** | Mark IV, OI, Volume, Max Pain | BTC, ETH, SOL | `live` | `GET /v1/deribit/{currency}/overview` |
| **Crypto Signals** | OHLCV klines, Funding rate, OI, L/S, Fear&Greed | BTC, ETH, SOL | `1h`, `1d` | `GET /v1/crypto/{symbol}/klines`, `GET /v1/crypto/futures/overview` |
| **Strategy Lab Backtest** | OHLCV klines | BTC, ETH, SOL | `1h`, `4h`, `1d` | `GET /v1/crypto/{symbol}/klines` |
| **Market Heatmap** | Live quotes, changePct, volume | NSE equities | `live` | `GET /v1/india/quotes/{symbol}`, `WS /v1/stream/ticks` |
| **AI Signals India** | OI buildup, PCR, gainers/losers, Option Chain | F&O universe | `live` | `GET /v1/india/broker-analytics/oi-buildup`, `GET /v1/india/broker-analytics/pcr`, `GET /v1/india/broker-analytics/gainers-losers`, `GET /v1/india/option-chain` |
| **Signal Quality Gate** | DataQualityGate, DataConfidenceScore | All active instruments | `live` | `POST /v1/quality/evaluate`, `GET /v1/quality/score` |

---

## 18. AlphaForge Migration Obligations

The following AlphaForge source files currently call external provider APIs directly. This is **prohibited** in the DATA-SERVICE 2.0 architecture. Each file must be migrated to the corresponding Platform endpoint.

### `src/services/india/angelone/derivatives.ts`

| Current direct call | Data type | Replacement Platform endpoint |
|---|---|---|
| Angel One SmartAPI PCR | Put/call ratio | `GET /v1/india/broker-analytics/pcr` |
| Angel One SmartAPI OI buildup | OI long/short buildup, covering, unwinding | `GET /v1/india/broker-analytics/oi-buildup` |
| Angel One SmartAPI gainers/losers | Top OI/price gainers & losers | `GET /v1/india/broker-analytics/gainers-losers` |

### `src/features/india/scanner/engine.ts`

| Current direct call | Data type | Replacement Platform endpoint |
|---|---|---|
| Scrapling/NSE live quotes | LTP, OI, change | `GET /v1/india/quotes/{symbol}` |
| Angel One live feed | Tick stream | `WS /v1/stream/ticks` |
| Upstox historical OHLCV | 1m/5m candles for scanner | `GET /v1/india/historical?interval=1m` |

### `src/features/india/daily-picks/builder.ts`

| Current direct call | Data type | Replacement Platform endpoint |
|---|---|---|
| Angel One OHLCV history | EOD + intraday OHLCV | `GET /v1/india/historical?interval=1d` |
| Scrapling/NSE option chain | Option chain for Greeks and OI | `GET /v1/india/option-chain` |

### `src/features/india/expiry-trades/builder.ts`

| Current direct call | Data type | Replacement Platform endpoint |
|---|---|---|
| Angel One option chain | IV, OI, Greeks for index derivatives | `GET /v1/india/option-chain?underlying=NIFTY` |
| Scrapling/NSE expiry OI | Strike-level OI for expiry analysis | `GET /v1/india/option-chain` |
| Angel One historical 5m/15m | Pre-expiry OHLCV | `GET /v1/india/historical?interval=5m` |

### `src/features/ai-signals/india-builder.ts`

| Current direct call | Data type | Replacement Platform endpoint |
|---|---|---|
| Angel One SmartAPI PCR | Put/call ratio (SmartAPI-specific) | `GET /v1/india/broker-analytics/pcr` |
| Angel One SmartAPI OI buildup | OI trend analysis | `GET /v1/india/broker-analytics/oi-buildup` |
| Angel One SmartAPI gainers/losers | Top gainers/losers by OI | `GET /v1/india/broker-analytics/gainers-losers` |
| Angel One live option chain | Option chain for AI signal features | `GET /v1/india/option-chain` |

### Migration notes

1. All replacement endpoints return data in the canonical success envelope with `provenance`, `quality`, and `dataSourceType` metadata — consuming code must handle this wrapper.
2. Timestamp fields in API responses are UTC ISO-8601 strings with `Z` suffix. IST conversion, if needed for display, must happen in the AlphaForge UI layer.
3. Provider tokens (`angelToken`, `upstoxKey`) are not included in API responses by default. Do not attempt to pass them through — use `instrumentId` to identify instruments.
4. When `signalEngineAllowed: false` is returned by `POST /v1/quality/evaluate`, the consuming signal engine must not generate a trade signal regardless of other data conditions.

---

## 19. Changelog — 2.0 vs Prior Versions

### Breaking changes from 1.x / direct-provider architecture

| Change | Impact on AlphaForge |
|---|---|
| **No direct provider calls** | All `angelone`, `upstox`, `scrapling`, `jugaad`, `binance`, `deribit` SDK imports must be removed from AlphaForge |
| **`3m` interval blocked for Indian markets** | Any existing AlphaForge code using `interval=3m` for NSE data will receive HTTP 400 and must be updated |
| **`oi` never populated from `tradedValue`** | Existing code that checked `oi` and fell back to `tradedValue` will now receive `oi: null` with `oiMissing: true`; update option chain consumers accordingly |
| **`iv`, Greeks, bid/ask are `null` when absent** | Code that compared these fields to `0` to detect missing data must switch to `null` checks |
| **All responses wrapped in canonical envelope** | `response.data` is the payload; `response.metadata` contains quality and provenance; direct field access patterns must be updated |
| **Timestamps are UTC ISO-8601 `Z` strings** | IST interpretation removed from the data layer; UI/display conversion is AlphaForge's responsibility |
| **`DataConfidenceScore` maximum is 95** | Any code that checked `score === 100` for perfect data will never see that; `score >= 80` is the `VALID` threshold |
| **`POOR_QUALITY` datasets not delivered** | Datasets with score < 60 are persisted for audit but not returned via API or Event Bus; consumers should not rely on low-quality data availability |

### New capabilities in 2.0

| Capability | Endpoint |
|---|---|
| Signal quality gate with per-strategy overrides | `POST /v1/quality/evaluate` |
| Full data lineage and trade forensics | `GET /v1/lineage/*` |
| Cross-provider OHLCV reconciliation | `GET /v1/india/historical/reconciliation` |
| Automated gap detection and recovery | `GET /v1/india/historical/gaps` |
| F&O universe snapshots and lifecycle events | `GET /v1/instruments/fno-universe` |
| Deribit crypto options analytics with null-safe IV/OI | `GET /v1/deribit/{currency}/overview` |
| Real-time streaming tick deduplication | `WS /v1/stream/ticks` (ticks with `isDuplicate: true`) |
| Prometheus metrics and OpenTelemetry tracing | `GET /metrics` |
| Data parity contract (live = paper = backtest) | `GET /v1/parity/contract` |

---

*This document is produced from the DATA-SERVICE 2.0 specification. The AlphaForge repository at `/Users/manishkumar/Desktop/alpha-forge` is referenced only for migration analysis — it is not accessed at runtime by the platform.*
