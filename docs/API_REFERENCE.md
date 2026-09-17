# DATA-SERVICE 2.0 — API Reference
**Version:** 2.1.0  
**Base URL:** `http://localhost:8200` (local dev) · `https://<host>` (production)  
**Updated:** 2026-09-17 (Upstox V3 migration; historical candle response now includes `provider` and `sourceType` per candle)

---

## Contents

1. [Overview](#overview)
2. [Authentication](#authentication)
3. [Response Envelopes](#response-envelopes)
4. [Error Codes](#error-codes)
5. [Health & Observability](#health--observability)
6. [Compatibility Routes (AlphaForge)](#compatibility-routes-alphaforge)
7. [India Markets — Live Data](#india-markets--live-data)
8. [India Markets — Historical Data](#india-markets--historical-data)
9. [India Markets — Broker Analytics](#india-markets--broker-analytics)
10. [Instruments & F&O Universe](#instruments--fo-universe)
11. [Crypto — Binance](#crypto--binance)
12. [Crypto — Delta Exchange](#crypto--delta-exchange)
13. [Deribit Options](#deribit-options)
14. [Quality Engine](#quality-engine)
15. [Provider Health & Analytics](#provider-health--analytics)
16. [Streaming (WebSocket)](#streaming-websocket)
17. [Provenance & Lineage](#provenance--lineage)
18. [Replay Engine](#replay-engine)
19. [Internal / Event Bus](#internal--event-bus)
20. [Canonical Types Reference](#canonical-types-reference)

---

## Overview

DATA-SERVICE 2.0 is the single authoritative market-data platform for AlphaForge. It aggregates, normalises, stores, and serves live and historical market data from multiple providers (Angel One, Upstox, Binance, Delta Exchange, Deribit, Yahoo Finance, Jugaad-data, OpenChart).

### Key design principles

- **All market data flows through this service** — no consumer calls external providers directly.
- **3m interval is permanently blocked** for Indian market data at every layer (adapter, engine, DB, API). It is allowed for Binance/Delta crypto.
- **No fabricated data** — when a provider cannot supply data, fields are `null`; zero is never substituted.
- **Provenance on every row** — every stored candle carries `provider` and `source_type`.

### Canonical interval strings (Indian markets)

`1m` `5m` `10m` `15m` `30m` `1h` `1d` `1w` `1M` — **not `3m`**

### source_type values

| Value | Meaning |
|---|---|
| `BROKER_AUTHENTICATED` | Live broker OAuth/JWT session (Angel One, Upstox) |
| `OPEN_SOURCE_NSE_DERIVED` | Credential-free NSE data (Yahoo Finance, Jugaad, OpenChart) |
| `CREDENTIAL_FREE` | Open-source data feed |

---

## Authentication

### Public endpoints (no auth required)

```
GET  /v1/health/live
GET  /v1/health/ready
GET  /v1/health/data
GET  /metrics
GET  /v1/auth/token
GET  /scraping/*
POST /data/gate
```

### Authenticated endpoints (all `/v1/*` data routes)

Two schemes are accepted — pick either:

**Option A — API key header**
```
X-API-KEY: <your-api-key>
```

**Option B — JWT bearer** (obtain a token first via `/v1/auth/token`)
```
Authorization: Bearer <JWT>
```

### `GET /v1/auth/token`

Exchange an API key for a short-lived HS256 JWT.

**Query params**

| Param | Type | Required | Description |
|---|---|---|---|
| `api_key` | string | ✅ | A key from the `CONSUMER_API_KEYS` allowlist |

**Response `200`**
```json
{
  "accessToken": "eyJ...",
  "tokenType": "Bearer",
  "expiresIn": 3600
}
```

**Errors**

| Status | Code | When |
|---|---|---|
| 401 | `UNAUTHORIZED` | `api_key` missing |
| 401 | `INVALID_API_KEY` | Key not in allowlist |

---

## Response Envelopes

### Success envelope

All authenticated data endpoints return:

```json
{
  "data": <payload>,
  "metadata": {
    "requestedAt":    "2026-01-15T09:15:00.000Z",
    "dataAsOf":       "2026-01-15T09:14:58.000Z",
    "dataSourceType": "LIVE | HISTORICAL | DERIVED | CACHED",
    "provider":       "angel_one | upstox | binance | ..."
  }
}
```

Some endpoints augment `metadata` with additional fields (e.g. `marketStatus`, `quality`, `truncated`, `gaps`).

### Error envelope

```json
{
  "error": {
    "code":         "SOME_ERROR_CODE",
    "message":      "Human-readable description",
    "provider":     "angel_one | null",
    "retryAfterMs": 5000,
    "requestId":    "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

---

## Error Codes

| Code | HTTP | Meaning |
|---|---|---|
| `UNAUTHORIZED` | 401 | Missing or invalid authentication |
| `INVALID_API_KEY` | 401 | API key not in allowlist |
| `INTERVAL_NOT_SUPPORTED` | 400 | Interval string not recognised or `3m` for Indian data |
| `INVALID_PARAMETER` | 400 | Malformed query param or date |
| `INVALID_FILTER` | 400 | Unrecognised enum filter value |
| `INVALID_BODY` | 400 | Request body is not valid JSON |
| `MISSING_FIELD` | 400 | Required body field absent |
| `INVALID_FIELD` | 400 | Field value fails validation |
| `NOT_FOUND` | 404 | Resource not found |
| `TRADE_NOT_FOUND` | 404 | Trade forensics record not found |
| `OBSERVATION_NOT_FOUND` | 404 | Provenance record not found |
| `PROVIDER_NOT_FOUND` | 404 | Provider not in registry |
| `CURRENCY_NOT_SUPPORTED` | 400 | Deribit currency not BTC/ETH/SOL |
| `RESOLUTION_NOT_SUPPORTED` | 400 | Invalid Deribit resolution |
| `METHOD_NOT_ALLOWED` | 405 | Mutation attempted on immutable resource |
| `PROVENANCE_IMMUTABLE` | 405 | PUT/PATCH/DELETE on provenance record |
| `FNO_UNIVERSE_UNAVAILABLE` | 503 | F&O snapshot not yet loaded |
| `PROVIDER_NOT_CONFIGURED` | 503 | Broker adapter not initialised (credentials missing) |
| `SERVICE_UNAVAILABLE` | 503 | Required service not available |
| `PROVIDER_UNAVAILABLE` | 502 | External provider call failed |
| `EVENT_PUBLISH_FAILED` | 502 | Redis stream publish failed |
| `STREAMING_ENGINE_UNAVAILABLE` | 503 | Streaming engine not running |
| `EVALUATION_ERROR` | 500 | Quality engine internal error |

---

## Health & Observability

### `GET /v1/health/live`

Liveness probe. Always returns HTTP 200 within 200 ms. Never blocks on external dependencies.

**Response `200`**
```json
{
  "status":    "alive",
  "version":   "2.0.0",
  "uptimeMs":  84600000,
  "timestamp": "2026-01-15T09:15:00.000Z"
}
```

---

### `GET /v1/health/ready`

Readiness probe. Checks Redis and PostgreSQL concurrently (2 s timeout each).

**Response `200`** — all dependencies healthy
```json
{
  "status":       "ready",
  "version":      "2.0.0",
  "timestamp":    "2026-01-15T09:15:00.000Z",
  "capabilities": { "redis": true, "postgres": true }
}
```

**Response `503`** — one or more dependencies unhealthy
```json
{
  "status":       "degraded",
  "version":      "2.0.0",
  "timestamp":    "...",
  "capabilities": { "redis": false, "postgres": true }
}
```

---

### `GET /v1/health/data`

Operational health snapshot for dashboards.

**Response `200`**
```json
{
  "status":        "ok",
  "version":       "2.0.0",
  "timestamp":     "...",
  "uptimeMs":      84600000,
  "marketSession": { "sessionPhase": "CLOSED" },
  "freshness": {
    "p50Ms": 80,
    "p99Ms": 450,
    "successRate": 0.998
  },
  "gaps":          { "low": 2, "medium": 0, "high": 0 },
  "duplicateRate": 0.001,
  "circuitBreakers": {
    "angel_one:historical_ohlcv": "CLOSED"
  },
  "clockDegraded": false
}
```

---

### `GET /metrics`

Prometheus metrics scrape endpoint. Returns `text/plain` Prometheus exposition format. No authentication required.

---

## Compatibility Routes (AlphaForge)

These routes provide backward-compatible APIs for AlphaForge's `ScraplingProvider`. They are **unauthenticated** (internal network only) and do not use the success envelope — they return shapes ScraplingProvider parses directly.

---

### `GET /scraping/historical`

Historical OHLCV candles. Queries the DB first; triggers a live backfill if DB is empty. Strips `NSE:` exchange prefix from `symbol` automatically.

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `symbol` | string | ✅ | — | NSE trading symbol. `NSE:HDFCBANK` and `HDFCBANK` both work. |
| `exchange` | string | | `NSE` | Exchange identifier |
| `interval` | string | | `1d` | Candle interval (`3m` blocked) |
| `from` | string | | 90 days ago | ISO-8601 start date/datetime |
| `to` | string | | now | ISO-8601 end date/datetime |

**Response `200`**
```json
{
  "candles":  [ { "time": 1704067200, "open": 1470.0, "high": 1485.5, "low": 1462.0, "close": 1478.3, "volume": 4215000, "oi": null, "volumeUnavailable": false, "provider": "upstox", "sourceType": "BROKER_AUTHENTICATED" } ],
  "count":    1,
  "truncated": false,
  "provider": "angel_one",
  "symbol":   "HDFCBANK",
  "exchange": "NSE",
  "interval": "1d"
}
```

**Errors:** `400 INTERVAL_NOT_SUPPORTED`, `400 INVALID_PARAMETER`

---

### `GET /scraping/quotes`

Live quotes for multiple NSE symbols.

**Query params**

| Param | Type | Required | Description |
|---|---|---|---|
| `symbols` | string | ✅ | Comma-separated symbols, e.g. `NIFTY,RELIANCE,HDFCBANK` |

**Response `200`**
```json
{
  "quotes": [
    {
      "symbol": "NIFTY",
      "token": null,
      "exchange": "NSE",
      "name": null,
      "ltp": null,
      "change": null,
      "changePct": null,
      "prevClose": null,
      "open": null,
      "high": null,
      "low": null,
      "volume": 0,
      "oi": null,
      "weekHigh52": null,
      "weekLow52": null,
      "upperCircuit": null,
      "lowerCircuit": null,
      "totalBuyQty": null,
      "totalSellQty": null,
      "lastTradeTime": null,
      "provider": "angel_one",
      "fetchedAt": "2026-01-15T09:15:00.000Z",
      "marketStatus": "CLOSED"
    }
  ]
}
```

> `ltp` is `null` when the market is closed or data is unavailable — never fabricated.

**Errors:** `400 INVALID_PARAMETER` (empty symbols)

---

### `GET /scraping/option-chain`

Option chain snapshot for an underlying.

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `underlying` | string | ✅ | — | e.g. `NIFTY`, `BANKNIFTY` |
| `expiry` | string | | nearest | `YYYY-MM-DD` |
| `exchange` | string | | `NSE` | |

**Response `200`** (raw OptionChain, no envelope)
```json
{
  "underlying":  "NIFTY",
  "spot":        24918.45,
  "expiry":      "2026-09-25",
  "expiries":    ["2026-09-25", "2026-10-30"],
  "rows":        [],
  "analytics":   {},
  "marketStatus": "CLOSED",
  "provider":    "angel_one",
  "fetchedAt":   "2026-01-15T09:15:00.000Z"
}
```

> `rows: []` when market is closed. Returns a safe empty chain on error (never HTTP 5xx).

**Errors:** `400 INVALID_PARAMETER` (bad expiry format)

---

### `GET /scraping/instruments`

NSE instrument master list from the DB.

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `exchange` | string | | `NSE` | `NSE`, `NFO`, `BSE` |
| `type` | string | | all | Instrument type filter: `EQ`, `IDX`, `OPTIDX`, etc. |

**Response `200`**
```json
{
  "instruments": [
    {
      "token": "1333",
      "tradingSymbol": "HDFCBANK",
      "name": "HDFC Bank Limited",
      "exchange": "NSE",
      "segment": "EQ",
      "instrumentType": "EQ",
      "isin": "INE040A01034",
      "expiry": null,
      "strike": null,
      "optionType": null,
      "lotSize": 1,
      "tickSize": 0.05,
      "upstoxKey": "NSE_EQ|INE040A01034"
    }
  ],
  "count": 1,
  "cached": true,
  "exchange": "NSE"
}
```

> Returns `count: 0` when `instrument_master` table is empty (requires a backfill job first).

---

### `POST /data/gate`

Data quality gate check. Returns `signalEngineAllowed` for the AlphaForge signal engine.

**Request body**
```json
{
  "symbol":               "NIFTY",
  "quoteAgeMs":           5000,
  "completenessPercent":  100,
  "timestampValid":       true,
  "crossSourceAgreement": 1.0,
  "sequenceIntegrity":    true,
  "maxQuoteAgeMs":        15000,
  "minConfidenceScore":   60,
  "providerAvailable":    true
}
```

**Response `200`**
```json
{
  "signalEngineAllowed": true,
  "confidenceScore":     85,
  "quality":             "HIGH",
  "quoteAgeMs":          5000,
  "gates": {
    "dataFresh":          true,
    "dataComplete":       true,
    "dataTimestampValid": true,
    "dataProviderHealthy": true,
    "dataSemanticallyValid": true
  },
  "blockReasons": null,
  "circuitBreakers": {},
  "evaluatedAt": "2026-01-15T09:15:00.000Z"
}
```

**Errors:** `400 INVALID_BODY`

---

## India Markets — Live Data

All authenticated. Use `X-API-KEY` header or `Authorization: Bearer`.

---

### `GET /v1/india/quotes/batch`

Live quotes for up to 200 NSE symbols in a single request.

> **Route ordering note:** This endpoint is registered before `/v1/india/quotes/{symbol}` to prevent the literal path segment `batch` being matched as a symbol name.

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `symbols` | string | ✅ | — | Comma-separated symbols, max 200 |
| `exchange` | string | | `NSE` | Exchange identifier |

**Response `200`**
```json
{
  "data": {
    "quotes": [
      {
        "symbol":      "NIFTY",
        "token":       null,
        "exchange":    "NSE",
        "name":        null,
        "ltp":         null,
        "change":      null,
        "changePct":   null,
        "prevClose":   null,
        "open":        null,
        "high":        null,
        "low":         null,
        "volume":      0,
        "oi":          null,
        "weekHigh52":  null,
        "weekLow52":   null,
        "upperCircuit": null,
        "lowerCircuit": null,
        "totalBuyQty": null,
        "totalSellQty": null,
        "lastTradeTime": null,
        "provider":    "angel_one",
        "fetchedAt":   "2026-01-15T09:15:00.000Z",
        "marketStatus": "CLOSED"
      }
    ],
    "count": 1
  },
  "metadata": { "requestedAt": "...", "dataAsOf": "...", "dataSourceType": "CACHED", "marketStatus": "CLOSED" }
}
```

**Errors:** `400 INVALID_PARAMETER` (empty or >200 symbols)

---

### `GET /v1/india/quotes/{symbol}`

Live quote for a single NSE instrument.

**Path param:** `symbol` — NSE trading symbol, e.g. `NIFTY`, `HDFCBANK`

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `exchange` | string | | `NSE` | Exchange identifier |

**Response `200`**
```json
{
  "data": {
    "instrumentId":      "NIFTY",
    "symbol":            "NIFTY",
    "exchange":          "NSE",
    "ltp":               null,
    "open":              null,
    "high":              null,
    "low":               null,
    "prevClose":         null,
    "change":            null,
    "changePct":         null,
    "volume":            0,
    "volumeUnavailable": true,
    "oi":                null,
    "oiMissing":         true,
    "tradedValue":       null,
    "totalBuyQty":       null,
    "totalSellQty":      null,
    "upperCircuit":      null,
    "lowerCircuit":      null,
    "weekHigh52":        null,
    "weekLow52":         null,
    "lastTradeTime":     null,
    "bid":               null,
    "ask":               null,
    "bidAskMissing":     true,
    "marketStatus":      "CLOSED",
    "provider":          "none",
    "normalisationVersion": "2.0.0",
    "provenance":        { "source": "none", "timestamp": "...", "sourceType": null }
  },
  "metadata": { "requestedAt": "...", "dataAsOf": "...", "dataSourceType": "CACHED", "marketStatus": "CLOSED" }
}
```

> During market hours (REGULAR session): `ltp`, `open`, `high`, `low` will be non-null.  
> When market is CLOSED: all price fields are `null`. Never fabricated.

---

### `GET /v1/india/option-chain`

Option chain snapshot.

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `underlying` | string | ✅ | — | e.g. `NIFTY`, `BANKNIFTY` |
| `expiry` | string | | nearest expiry | `YYYY-MM-DD` |
| `exchange` | string | | `NSE` | |

**Response `200`**
```json
{
  "data": {
    "underlying":   "NIFTY",
    "spot":         24918.45,
    "expiry":       "2026-09-25",
    "expiries":     ["2026-09-25", "2026-10-30", "2026-12-31"],
    "rows": [
      {
        "strike":      24900,
        "ce": { "ltp": 152.5, "oi": 1845000, "volume": 23400, "iv": 12.4, "delta": 0.52 },
        "pe": { "ltp": 135.0, "oi": 2100000, "volume": 18200, "iv": 11.8, "delta": -0.48 }
      }
    ],
    "analytics":    { "pcr": 1.14, "maxPainStrike": 24900 },
    "chainQuality": "LIVE",
    "marketStatus": "CLOSED",
    "provider":     "angel_one",
    "fetchedAt":    "2026-01-15T09:15:00.000Z"
  },
  "metadata": { "requestedAt": "...", "dataAsOf": "...", "dataSourceType": "LIVE", "marketStatus": "CLOSED" }
}
```

> `rows: []` when market is closed. `chainQuality: "DEGRADED"` when spot price is >60s old.

**Errors:** `400 INVALID_PARAMETER` (malformed expiry)

---

### `GET /v1/india/market/status`

Current NSE session phase and calendar information.

**Response `200`**
```json
{
  "data": {
    "sessionPhase":      "CLOSED",
    "nextSessionChange": "2026-09-15T03:45:00.000Z",
    "tradingDay":        false,
    "nextTradingDay":    "2026-09-15",
    "holidays":          [ { "date": "2026-10-02", "name": "Gandhi Jayanti" } ],
    "calendarStatus":    "WEEKEND"
  },
  "metadata": { "requestedAt": "...", "dataAsOf": "...", "dataSourceType": "DERIVED" }
}
```

**Session phases:** `PRE_OPEN` · `REGULAR` · `POST_MARKET` · `AFTER_HOURS` · `CLOSED` · `UNKNOWN`

---

## India Markets — Historical Data

---

### `GET /v1/india/historical`

Query persisted OHLCV candles from the database.

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `symbol` | string | ✅ | — | NSE trading symbol |
| `exchange` | string | | `NSE` | Exchange identifier |
| `interval` | string | | `1d` | Candle interval. `3m` always returns HTTP 400. |
| `from` | string | | interval's min depth | ISO-8601 start (e.g. `2024-01-01` or `2024-01-01T09:15:00Z`) |
| `to` | string | | now | ISO-8601 end (exclusive) |

**Supported intervals:** `1m` `5m` `10m` `15m` `30m` `1h` `1d` `1w` `1M`

**Response `200`**
```json
{
  "data": [
    {
      "time":              1722483900,
      "open":              811.25,
      "high":              814.30,
      "low":               808.98,
      "close":             813.25,
      "volume":            1101936,
      "oi":                null,
      "volumeUnavailable": false
    }
  ],
  "metadata": {
    "requestedAt":  "...",
    "dataAsOf":     "...",
    "dataSourceType": "HISTORICAL",
    "provider":     "angel_one",
    "truncated":    false,
    "gaps":         [],
    "quality":      { "candleCount": 75, "truncated": false }
  }
}
```

> `time` is UTC epoch seconds. Up to 10,000 records returned; `truncated: true` when limit reached.

**Errors:**
- `400 INTERVAL_NOT_SUPPORTED` — `3m` or unknown interval
- `400 INVALID_PARAMETER` — bad from/to dates or `from >= to`

---

### `GET /v1/india/historical/status`

Historical data layer operational status.

**Response `200`**
```json
{
  "data": {
    "supportedTimeframes": ["1m","5m","10m","15m","30m","1h","1d","1w","1M"],
    "gapSummary": {
      "total": 3,
      "byStatus": { "PENDING": 1, "RECOVERING": 0, "RECOVERED": 2, "EXHAUSTED": 0 }
    },
    "reconciliationStatus": {
      "totalCompared": 8450,
      "matched": 8430,
      "matchRatePct": 99.76
    },
    "backfillJobs": { "PENDING": 0, "RUNNING": 1, "COMPLETED": 47, "FAILED": 0 },
    "providerActivity": ["angel_one", "upstox", "yahoo_finance"]
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "DERIVED" }
}
```

---

### `POST /v1/india/historical/backfill`

Submit an asynchronous backfill job. The engine fetches missing candles from the appropriate provider and persists them to the DB.

**Provider routing logic:**

| instrument_class | interval | Primary provider |
|---|---|---|
| EQ | intraday (1m–1h) | Angel One |
| EQ | 1d/1w/1M | Upstox (if token) → Jugaad-data → Angel One |
| IDX | 1m/30m | Upstox (if token + plan supports) → Angel One |
| IDX | 5m/10m/15m/1h | Angel One (Upstox basic plan doesn't support) |
| IDX | 1d/1w/1M | Upstox → Yahoo Finance |
| FO | 1d | Jugaad-data (for dates < 2024-07-08) |
| any | 1d fallback | Yahoo Finance |

**Request body**
```json
{
  "symbol":           "HDFCBANK",
  "exchange":         "NSE",
  "interval":         "5m",
  "from_date":        "2024-08-01",
  "to_date":          "2024-08-31",
  "instrument_class": "EQ"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `symbol` | string | ✅ | NSE trading symbol |
| `exchange` | string | ✅ | `NSE`, `NFO`, `BSE` |
| `interval` | string | ✅ | Candle interval. `3m` blocked. |
| `from_date` | string | ✅ | `YYYY-MM-DD` |
| `to_date` | string | | `YYYY-MM-DD` (defaults to today) |
| `instrument_class` | string | ✅ | `EQ` / `FO` / `IDX` |

**Response `202`** — job accepted
```json
{
  "data": {
    "jobId":           "ab0e4845-6f37-42f1-ae78-c2f4de1077bf",
    "symbol":          "HDFCBANK",
    "exchange":        "NSE",
    "interval":        "5m",
    "instrumentClass": "EQ",
    "fromDate":        "2024-08-01T00:00:00.000Z",
    "toDate":          "2024-08-31T00:00:00.000Z",
    "status":          "PENDING",
    "createdAt":       "2026-01-15T09:15:00.000Z",
    "startedAt":       null,
    "completedAt":     null,
    "result":          null,
    "error":           null
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "DERIVED" }
}
```

**Errors:** `400 INVALID_PARAMETER` (bad dates), `422` (invalid interval or instrument_class)

---

### `GET /v1/india/historical/backfill/{job_id}`

Poll the status of a backfill job.

**Path param:** `job_id` — UUID returned by the POST endpoint

**Response `200`**
```json
{
  "data": {
    "jobId":           "ab0e4845-6f37-42f1-ae78-c2f4de1077bf",
    "symbol":          "HDFCBANK",
    "exchange":        "NSE",
    "interval":        "5m",
    "instrumentClass": "EQ",
    "fromDate":        "2024-08-01T00:00:00.000Z",
    "toDate":          "2024-08-31T00:00:00.000Z",
    "status":          "COMPLETED",
    "createdAt":       "2026-01-15T09:15:00.000Z",
    "startedAt":       "2026-01-15T09:15:01.000Z",
    "completedAt":     "2026-01-15T09:15:08.000Z",
    "result": {
      "symbol":                  "HDFCBANK",
      "exchange":                "NSE",
      "interval":                "5m",
      "chunks_attempted":        4,
      "chunks_succeeded":        4,
      "candles_persisted":       4727,
      "resumed_from_checkpoint": false,
      "checkpoint_ts":           "2024-08-30T15:29:00.000Z",
      "incidents":               []
    },
    "error": null
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "DERIVED" }
}
```

**Job statuses:** `PENDING` → `RUNNING` → `COMPLETED` / `FAILED`

**Errors:** `404 NOT_FOUND`

---

### `GET /v1/india/historical/gaps`

List detected candle-sequence gaps with optional filters.

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `symbol` | string | | all | Filter by symbol substring match |
| `interval` | string | | all | Filter by interval |
| `status` | string | | all | `PENDING` / `RECOVERING` / `RECOVERED` / `EXHAUSTED` |
| `limit` | int | | 100 | Max records (1–1000) |
| `exchange` | string | | all | Filter by exchange |

**Response `200`**
```json
{
  "data": [
    {
      "gapId":             "c2fe58f3-caf8-4fc0-a426-ed94569abfe2",
      "instrumentId":      "NSE:HDFCBANK",
      "exchange":          "NSE",
      "intervalStr":       "5m",
      "gapStart":          1722483900000,
      "gapEnd":            1722570300000,
      "durationSec":       86400,
      "recoveryStatus":    "PENDING",
      "recoveryAttempts":  0,
      "expectedProvider":  "angel_one",
      "recoveryProvider":  null
    }
  ],
  "metadata": { "requestedAt": "...", "dataSourceType": "HISTORICAL", "quality": { "count": 1 } }
}
```

**Errors:** `400 INVALID_PARAMETER` (invalid status)

---

### `GET /v1/india/historical/reconciliation`

Cross-provider reconciliation statistics.

**Response `200`**
```json
{
  "data": {
    "totalCompared":  8450,
    "matched":        8430,
    "matchRatePct":   99.76,
    "distribution": {
      "CONFIRMED":          8430,
      "MINOR_DISCREPANCY":   18,
      "MAJOR_DISCREPANCY":    2
    },
    "byProviderPair": {
      "angel_one_vs_upstox": { "confirmed": 1200, "minor": 8, "major": 1 }
    }
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "DERIVED" }
}
```

---

## India Markets — Broker Analytics

These endpoints serve Angel One broker analytics (PCR, OI buildup, gainers/losers). They return `503 PROVIDER_NOT_CONFIGURED` when Angel One adapter is not initialised (missing credentials).

---

### `GET /v1/india/broker-analytics/pcr`

Put/Call Ratio from Angel One.

**Response `200`**
```json
{
  "data": {
    "putCallRatio": 1.14,
    "provider":     "angel_one",
    "sourceType":   "BROKER_AUTHENTICATED",
    "fetchedAt":    "2026-01-15T09:15:00.000Z"
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE", "provider": "angel_one" }
}
```

**Errors:** `503 PROVIDER_NOT_CONFIGURED`, `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/india/broker-analytics/oi-buildup`

Open Interest buildup signals from Angel One.

**Response `200`**
```json
{
  "data": [
    {
      "symbol":      "NIFTY",
      "oi":          12384650,
      "oiChange":    284500,
      "buildupType": "LONG_BUILDUP",
      "provider":    "angel_one",
      "sourceType":  "BROKER_AUTHENTICATED",
      "fetchedAt":   "..."
    }
  ],
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE" }
}
```

**buildupType values:** `LONG_BUILDUP` · `SHORT_BUILDUP` · `LONG_UNWINDING` · `SHORT_COVERING`

**Errors:** `503 PROVIDER_NOT_CONFIGURED`, `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/india/broker-analytics/gainers-losers`

Top OI and price gainers/losers from Angel One.

**Response `200`**
```json
{
  "data": {
    "oiGainers":    [ { "symbol": "RELIANCE", "oi": 8432100, "oiChangePct": 12.4 } ],
    "oiLosers":     [ { "symbol": "INFY",     "oi": 2100000, "oiChangePct": -8.1 } ],
    "priceGainers": [ { "symbol": "HDFCBANK", "ltp": 1678.5, "changePct": 2.1 } ],
    "priceLosers":  [ { "symbol": "TCS",      "ltp": 4480.0, "changePct": -1.3 } ],
    "provider":     "angel_one",
    "sourceType":   "BROKER_AUTHENTICATED",
    "fetchedAt":    "..."
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE" }
}
```

**Errors:** `503 PROVIDER_NOT_CONFIGURED`, `502 PROVIDER_UNAVAILABLE`

---

## Instruments & F&O Universe

---

### `GET /v1/instruments`

List instruments from the in-memory `InstrumentMaster`. All filters are optional.

**Query params**

| Param | Type | Description |
|---|---|---|
| `exchange` | string | `NSE` / `NFO` / `BSE` / `BFO` / `MCX` |
| `instrumentType` | string | `EQ` / `FUTIDX` / `FUTSTK` / `OPTIDX` / `OPTSTK` / `ETF` / `IDX` |
| `underlying` | string | Underlying symbol for derivatives, e.g. `NIFTY` |
| `segment` | string | `EQ` / `FO` / `CD` / `COM` / `CDS` |
| `expiry` | string | `YYYY-MM-DD` |

**Response `200`**
```json
{
  "data": [
    {
      "instrumentId":   "NSE:HDFCBANK:EQ",
      "tradingSymbol":  "HDFCBANK",
      "displaySymbol":  "HDFC Bank Limited",
      "isin":           "INE040A01034",
      "exchange":       "NSE",
      "segment":        "EQ",
      "instrumentType": "EQ",
      "underlying":     null,
      "expiry":         null,
      "strike":         null,
      "optionType":     null,
      "lotSize":        1,
      "tickSize":       0.05,
      "activeFrom":     "2020-01-01",
      "activeTo":       null
    }
  ],
  "metadata": { "requestedAt": "...", "dataSourceType": "HISTORICAL" }
}
```

> Provider tokens (angel_token, upstox_key) are excluded by default.

**Errors:** `400 INVALID_FILTER`

---

### `GET /v1/instruments/{instrument_id}`

Single instrument lookup by canonical instrument ID.

**Path param:** `instrument_id` — e.g. `NSE:HDFCBANK:EQ`

**Query params**

| Param | Type | Description |
|---|---|---|
| `include` | string | Pass `providerTokens` to include `angel_token` and `upstox_key` fields |

**Response `200`** — same shape as single item from the list endpoint

**Errors:** `404 NOT_FOUND`, `503 SERVICE_UNAVAILABLE`

---

### `GET /v1/instruments/fno-universe`

Current NSE F&O eligible universe snapshot (refreshed at 08:45 IST each trading day).

**Response `200`**
```json
{
  "data": {
    "universeVersion":  3,
    "checksum":         "a3b4c5d6...",
    "generatedAt":      "2026-01-15T03:15:00.000Z",
    "effectiveFrom":    "2026-01-15",
    "effectiveTo":      null,
    "fnoEquityCount":   182,
    "fnoIndexCount":    8,
    "constituentCount": 190,
    "status":           "ACTIVE",
    "constituents":     [ { "symbol": "NIFTY", "lotSize": 25, "exchange": "NSE" } ]
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "HISTORICAL" }
}
```

**Errors:** `503 FNO_UNIVERSE_UNAVAILABLE`

---

### `GET /v1/instruments/fno-universe/history`

Paginated F&O universe snapshot history.

**Query params**

| Param | Type | Default | Description |
|---|---|---|---|
| `status` | string | all | `ACTIVE` or `SUPERSEDED` |
| `version` | int | all | Filter to exact version number |
| `page` | int | 1 | Page number (1-indexed) |
| `limit` | int | 20 | Records per page (max 100) |

**Response `200`**
```json
{
  "data": {
    "snapshots": [
      {
        "universeVersion":  3,
        "checksum":         "a3b4c5d6...",
        "generatedAt":      "2026-01-15T03:15:00.000Z",
        "effectiveFrom":    "2026-01-15",
        "effectiveTo":      null,
        "fnoEquityCount":   182,
        "fnoIndexCount":    8,
        "constituentCount": 190,
        "status":           "ACTIVE"
      }
    ],
    "pagination": { "page": 1, "limit": 20, "total": 45, "totalPages": 3 }
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "HISTORICAL" }
}
```

**Errors:** `400 INVALID_FILTER`

---

## Crypto — Binance

> 3m interval **is allowed** for Binance crypto data.

---

### `GET /v1/crypto/{symbol}/ohlcv`

Binance OHLCV candlestick data.

**Path param:** `symbol` — Binance pair, e.g. `BTCUSDT`, `ETHUSDT`

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `interval` | string | ✅ | — | Binance kline interval. 3m is valid. |
| `limit` | int | | 500 | Number of candles (1–1000) |
| `from` | string | | — | ISO-8601 start datetime |
| `to` | string | | — | ISO-8601 end datetime |

**Binance intervals:** `1m` `3m` `5m` `15m` `30m` `1h` `2h` `4h` `6h` `8h` `12h` `1d` `3d` `1w` `1M`

**Response `200`**
```json
{
  "data": [
    {
      "symbol":       "BTCUSDT",
      "openTime":     1705276800000,
      "closeTime":    1705280399999,
      "interval":     "1h",
      "open":         "42815.00",
      "high":         "43250.00",
      "low":          "42750.00",
      "close":        "43100.00",
      "volume":       "1234.567",
      "quoteVolume":  "53200000.00",
      "trades":       8450,
      "takerBuyBase": "612.345",
      "takerBuyQuote":"26400000.00"
    }
  ],
  "metadata": { "requestedAt": "...", "dataAsOf": "...", "dataSourceType": "HISTORICAL", "provider": "binance" }
}
```

**Errors:** `400 INTERVAL_NOT_SUPPORTED`, `400 INVALID_PARAMETER`, `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/crypto/{symbol}/ticker`

Current Binance spot price.

**Path param:** `symbol`

**Response `200`**
```json
{
  "data":     { "symbol": "BTCUSDT", "price": "43100.00000000" },
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE", "provider": "binance" }
}
```

**Errors:** `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/crypto/{symbol}/stats`

Binance 24-hour rolling statistics.

**Path param:** `symbol`

**Response `200`** — full Binance `/api/v3/ticker/24hr` dict inside envelope:
```json
{
  "data": {
    "symbol":             "BTCUSDT",
    "priceChange":        "1285.00",
    "priceChangePercent": "3.07",
    "weightedAvgPrice":   "42500.00",
    "openPrice":          "41815.00",
    "highPrice":          "43500.00",
    "lowPrice":           "41500.00",
    "lastPrice":          "43100.00",
    "volume":             "45231.456",
    "quoteVolume":        "1923000000.00",
    "count":              284500
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE", "provider": "binance" }
}
```

**Errors:** `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/crypto/exchange-info`

Binance exchange metadata.

**Query params**

| Param | Type | Description |
|---|---|---|
| `symbol` | string | Optional — filter to single symbol to reduce response size |

**Response `200`** — Binance exchange info dict (can be large without symbol filter)

**Errors:** `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/crypto/futures/overview`

Perpetual futures snapshot covering `BTCUSDT`, `ETHUSDT`, `SOLUSDT`.

**Response `200`**
```json
{
  "data": [
    {
      "symbol":                "BTCUSDT",
      "markPrice":             "43080.00",
      "indexPrice":            "43095.00",
      "fundingRate":           0.00012,
      "fundingRateAnnualized": 0.1314,
      "nextFundingTime":       "2026-01-15T16:00:00.000Z",
      "openInterest":          105003.45,
      "openInterestNotionalUsd":"4524000000",
      "oiChangePct1h":         0.42,
      "longShortRatio":        1.681,
      "longAccount":           0.627,
      "shortAccount":          0.373
    }
  ],
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE", "provider": "binance" }
}
```

> Null fields on partial per-symbol failures — overall response always 200.

---

## Crypto — Delta Exchange

Delta Exchange India perpetuals. Use USD-quoted symbols: `BTCUSD`, `ETHUSD`, `SOLUSD`.

> 3m interval **is allowed** for Delta crypto.

---

### `GET /v1/delta/{symbol}/ohlcv`

Delta Exchange OHLCV candles.

**Path param:** `symbol` — e.g. `BTCUSD`

**Query params**

| Param | Type | Required | Default | Description |
|---|---|---|---|---|
| `interval` | string | ✅ | — | Delta interval |
| `limit` | int | | 100 | Number of candles (1–2000) |
| `from` | string | | — | ISO-8601 start |
| `to` | string | | — | ISO-8601 end |

**Delta intervals:** `1m` `3m` `5m` `10m` `15m` `30m` `1h` `2h` `4h` `6h` `12h` `1d` `1w`

**Response `200`** — same shape as Binance OHLCV but `provider: "delta"` and USD-quoted prices

**Errors:** `400 INTERVAL_NOT_SUPPORTED`, `400 INVALID_PARAMETER`, `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/delta/{symbol}/ticker`

Delta Exchange current ticker.

**Response `200`**
```json
{
  "data": {
    "symbol":     "BTCUSD",
    "price":      77265.5,
    "exchange":   "DELTA",
    "markPrice":  77250.0,
    "indexPrice": 77280.0,
    "fundingRate": 0.000069,
    "volume24h":  12500,
    "openInterest": 85000
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE", "provider": "delta" }
}
```

**Errors:** `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/delta/futures/overview`

Delta perpetuals snapshot covering `BTCUSD`, `ETHUSD`, `SOLUSD`.

**Response `200`** — same shape as Binance futures overview but with `"exchange": "DELTA"`. Note: `longShortRatio`, `longAccount`, `shortAccount` are always `null` (not available on Delta India).

---

## Deribit Options

Supported currencies: **BTC** · **ETH** · **SOL**

---

### `GET /v1/deribit/{currency}/overview`

Aggregated options overview including per-contract summaries, total OI, and put/call ratio.

**Path param:** `currency` — `BTC`, `ETH`, or `SOL`

**Response `200`**
```json
{
  "data": {
    "currency":    "BTC",
    "indexPrice":  77139.0,
    "computedAt":  "2026-09-14T06:00:00.000Z",
    "totalCallOI": 12450,
    "totalPutOI":  14200,
    "pcrOi":       1.14,
    "contracts": [
      {
        "instrument":  "BTC-27DEC24-70000-C",
        "markIv":      67.48,
        "openInterest": 524,
        "bestBid":     0.1185,
        "bestAsk":     0.1195
      }
    ]
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE" }
}
```

> `pcrOi` is `null` (not zero) when total call OI is zero. `markIv`, `openInterest`, `bestBid`, `bestAsk` are preserved as `null` — never substituted with zero.

**Errors:** `400 CURRENCY_NOT_SUPPORTED`, `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/deribit/{currency}/instruments`

List Deribit instruments.

**Query params**

| Param | Type | Default | Description |
|---|---|---|---|
| `kind` | string | `option` | `option`, `future`, or `spot` |

**Response `200`** — `{"data": [<Deribit instrument dicts>], "metadata": {...}}`

**Errors:** `400 CURRENCY_NOT_SUPPORTED`, `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/deribit/{currency}/index-price`

Current Deribit index price.

**Response `200`**
```json
{
  "data":     { "index_name": "btc_usd", "index_price": 77139.0 },
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE" }
}
```

**Errors:** `400 CURRENCY_NOT_SUPPORTED`, `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/deribit/ticker/{instrument_name}`

Full ticker for a single Deribit instrument. Null fields preserved exactly as Deribit returns them.

**Path param:** `instrument_name` — e.g. `BTC-27DEC24-100000-C`, `BTC-PERPETUAL`

**Response `200`** — raw Deribit ticker dict inside envelope; `mark_iv`, `open_interest`, `best_bid_price`, `best_ask_price` are **never zero-substituted**

**Errors:** `502 PROVIDER_UNAVAILABLE`

---

### `GET /v1/deribit/ohlcv/{instrument_name}`

Deribit OHLCV candles (TradingView format normalised to canonical shape).

**Path param:** `instrument_name` — e.g. `BTC-PERPETUAL`

**Query params**

| Param | Type | Required | Description |
|---|---|---|---|
| `resolution` | string | ✅ | `1` `3` `5` `10` `15` `30` `60` `120` `180` `360` `720` `1D` |
| `start_ts` | int | ✅ | UTC epoch milliseconds (must be >0) |
| `end_ts` | int | ✅ | UTC epoch milliseconds (must be >0) |

**Response `200`**
```json
{
  "data": [
    { "time": 1705276800000, "open": 42815.0, "high": 43250.0, "low": 42750.0, "close": 43100.0, "volume": 1234.567 }
  ],
  "metadata": { "requestedAt": "...", "dataSourceType": "HISTORICAL" }
}
```

**Errors:** `400 RESOLUTION_NOT_SUPPORTED`, `400 INVALID_PARAMETER` (start_ts ≥ end_ts), `502 PROVIDER_UNAVAILABLE`

---

## Quality Engine

---

### `POST /v1/quality/evaluate`

Evaluate a market data observation through the quality gate.

**Query params**

| Param | Type | Default | Description |
|---|---|---|---|
| `min_confidence_score` | float | 60.0 | Gate threshold (30–95) |

**Request body** (any JSON dict, all fields optional)
```json
{
  "symbol":          "NIFTY",
  "timestamp":       1705276800000,
  "open":            22000.0,
  "high":            22100.0,
  "low":             21900.0,
  "close":           22050.0,
  "volume":          1200000,
  "eventTimeMs":     1705276800000,
  "quoteAgeMs":      500,
  "providerAvailable": true,
  "source":          "angel_one"
}
```

**Response `200`**
```json
{
  "data": {
    "gate": {
      "signalEngineAllowed": true,
      "confidenceScore":     85,
      "blockReasons":        [],
      "gates": {
        "dataFresh":           true,
        "dataComplete":        true,
        "dataTimestampValid":  true,
        "dataProviderHealthy": true,
        "dataSemanticallyValid": true
      }
    },
    "classification": {
      "grade":               "HIGH",
      "signalEngineAllowed": true,
      "score":               85,
      "reasons":             []
    },
    "signalEngineAllowed": true
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "DERIVED" }
}
```

**Errors:** `400 INVALID_REQUEST_BODY`, `500 EVALUATION_ERROR`

---

### `GET /v1/quality/score`

Compute a DataConfidenceScore from individual quality dimensions without storing an observation.

**Query params**

| Param | Type | Required | Description |
|---|---|---|---|
| `freshness` | string | ✅ | `FRESH` / `AGING` / `STALE` / `EXPIRED` / `UNKNOWN` |
| `completeness` | float | ✅ | Completeness percentage (0–100) |
| `provider_healthy` | bool | ✅ | Whether provider is healthy |
| `timestamp_valid` | bool | ✅ | Whether data timestamp is valid |
| `agreement` | float | ✅ | Cross-source agreement score (0.0–1.0) |
| `sequence_ok` | bool | `true` | Whether sequence integrity holds |

**Response `200`**
```json
{
  "data": {
    "score": 85,
    "grade": "HIGH",
    "signalEngineAllowed": true,
    "components": {
      "freshness":               "FRESH",
      "freshnessScore":          25,
      "completenessScore":       20,
      "providerHealthScore":     20,
      "timestampScore":          10,
      "agreementScore":          10,
      "sequencePenaltyApplied":  false
    }
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "DERIVED" }
}
```

**Grade thresholds:** `HIGH` (≥80) · `MEDIUM` (50–79) · `LOW` (30–49) · `BLOCKED` (<30)

**Errors:** `400 INVALID_PARAMETER`, `500 COMPUTATION_ERROR`

---

## Provider Health & Analytics

---

### `GET /v1/providers/health`

Current health status of all registered data providers.

**Response `200`** (not wrapped in success envelope — raw dict)
```json
{
  "providers": {
    "angel_one:HISTORICAL_OHLCV": {
      "provider":          "angel_one",
      "capability":        "HISTORICAL_OHLCV",
      "status":            "UP",
      "circuitState":      "CLOSED",
      "availability":      0.998,
      "latencyP50Ms":      45,
      "latencyP99Ms":      180,
      "errorRate":         0.002,
      "lastSuccessAt":     "2026-01-15T09:14:50.000Z",
      "lastFailureReason": null,
      "semanticIntegrity": true
    }
  }
}
```

**Status values:** `UP` · `DOWN` · `DEGRADED` · `UNKNOWN`  
**circuitState values:** `CLOSED` · `HALF_OPEN` · `OPEN`

---

### `GET /v1/analytics/providers`

List all providers with health summary from the analytics registry.

**Response `200`**
```json
{
  "data": [
    {
      "providerId":           "angel_one",
      "providerHealthy":      true,
      "circuitBreakerState":  "CLOSED",
      "failureRate":          0.002,
      "lastSuccessAt":        "2026-01-15T09:14:50.000Z",
      "lastFailureAt":        null,
      "avgResponseTimeMs":    45,
      "totalRequests":        8000,
      "successfulRequests":   7984,
      "failedRequests":       16
    }
  ],
  "metadata": { "requestedAt": "...", "dataSourceType": "LIVE" }
}
```

---

### `GET /v1/analytics/providers/{provider_id}`

Detailed health for a single provider. Case-insensitive (`angel_one` and `ANGEL_ONE` both work).

**Errors:** `404 PROVIDER_NOT_FOUND`

---

### `GET /v1/analytics/quality`

Aggregate DataConfidenceScore distribution across all active symbols.

**Response `200`**
```json
{
  "data": {
    "averageScore":   82.5,
    "blockedCount":   1,
    "lowCount":       3,
    "mediumCount":    12,
    "highCount":      45,
    "stalemaskCount": 0,
    "totalSymbols":   61
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "DERIVED" }
}
```

---

### `GET /v1/analytics/health`

Platform degradation check. Returns `503` when any critical component is unhealthy.

**Response `200`** — all healthy
```json
{
  "data": { "healthy": true, "degradedComponents": [] },
  "metadata": { "requestedAt": "...", "dataSourceType": "DERIVED" }
}
```

**Response `503`** — degraded
```json
{
  "data": {
    "healthy": false,
    "degradedComponents": ["redis_unavailable", "circuit_breaker_open:angel_one:historical_ohlcv"]
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "DERIVED" }
}
```

---

## Streaming (WebSocket)

---

### `WS /v1/stream/ticks`

Real-time market tick stream over WebSocket. Authentication is inherited from the HTTP upgrade request — include the API key or JWT in the upgrade headers.

**Control messages (client → server)**

```json
// Subscribe
{ "action": "subscribe", "symbols": ["NIFTY", "BANKNIFTY"], "market": "india" }

// Unsubscribe
{ "action": "unsubscribe", "symbols": ["NIFTY"] }
```

**Server messages (server → client)**

```json
// Acknowledgement
{ "type": "ack", "action": "subscribe", "symbols": ["NIFTY", "BANKNIFTY"] }

// Heartbeat (every 10 seconds)
{ "type": "heartbeat", "timestamp": "2026-01-15T09:15:00.000Z" }

// Error
{ "type": "error", "code": "MAX_SUBSCRIPTIONS_EXCEEDED", "message": "..." }

// Tick delivery
{
  "tickId":      "uuid-v4",
  "symbol":      "NIFTY",
  "exchange":    "NSE",
  "ltp":         24918.45,
  "change":      118.3,
  "changePct":   0.48,
  "volume":      45000000,
  "oi":          12384650,
  "timestamp":   "2026-01-15T09:15:00.000Z",
  "marketStatus": "REGULAR"
}
```

**Error codes**

| Code | Meaning |
|---|---|
| `MAX_SUBSCRIPTIONS_EXCEEDED` | Platform-wide symbol limit (500) reached |
| `INVALID_JSON` | Non-JSON control message |
| `INVALID_MESSAGE` | Not a JSON object |
| `INVALID_SYMBOLS` | `symbols` is not a JSON array |

**Connection lifecycle:** Server sends heartbeats every 10 s. If 3 consecutive heartbeats cannot be delivered (30 s timeout), the connection is forcibly closed with code 1001.

---

### `GET /v1/stream/status`

WebSocket streaming engine status.

**Response `200`**
```json
{
  "subscribedSymbols":  12,
  "activeConnections":  4,
  "ticksPublished":     84321,
  "validationFailures": { "schema_validation": 2 },
  "lastPublishedAt":    "2026-01-15T09:15:00.000Z",
  "brokerConnections": {
    "angelOne": true,
    "upstox":   false,
    "binance":  true
  },
  "timestamp": "2026-01-15T09:15:00.000Z"
}
```

---

## Provenance & Lineage

---

### `GET /v1/provenance/{observation_id}`

Fetch a single provenance record.

**Path param:** `observation_id` — UUID

**Response `200`**
```json
{
  "data": {
    "dataObservationId":    "550e8400-e29b-41d4-a716-446655440000",
    "instrumentId":         "NSE:NIFTY50:IDX",
    "exchange":             "NSE",
    "primaryProvider":      "ANGEL_ONE",
    "sourceVersion":        "v2",
    "isFallback":           false,
    "fallbackReason":       null,
    "eventTimeMs":          1705276800000,
    "receivedAtMs":         1705276800100,
    "normalisationVersion": "2.0.0",
    "intervalStr":          "1m",
    "sessionDate":          "2024-01-15",
    "sourceType":           "BROKER_AUTHENTICATED",
    "authenticated":        true,
    "datasetKey":           "NSE:NIFTY50:1m:2024-01-15",
    "datasetVersion":       1,
    "rowCount":             390
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "HISTORICAL" }
}
```

**Errors:** `404 OBSERVATION_NOT_FOUND`

---

### `GET /v1/provenance/instrument/{instrument_id}`

Recent provenance records for an instrument (newest first).

**Query params:** `limit` (int, default 100, 1–1000)

**Response `200`** — `{"data": [DataProvenance, ...], "metadata": {..., "storeSize", "totalRecorded", "count"}}`

---

### `POST /v1/provenance`

Create a provenance record (internal use). `dataObservationId` is always assigned by the platform.

**Request body**
```json
{
  "instrumentId":         "NSE:NIFTY50:IDX",
  "exchange":             "NSE",
  "primaryProvider":      "ANGEL_ONE",
  "sourceVersion":        "v2",
  "isFallback":           false,
  "fallbackReason":       null,
  "eventTimeMs":          1705276800000,
  "normalisationVersion": "2.0.0",
  "intervalStr":          "1m",
  "sessionDate":          "2024-01-15",
  "sourceType":           "BROKER_AUTHENTICATED",
  "authenticated":        true,
  "datasetKey":           "NSE:NIFTY50:1m:2024-01-15",
  "datasetVersion":       1,
  "rowCount":             390
}
```

**Required fields:** `instrumentId`, `exchange`, `primaryProvider` (or `source`)

**Response `201`** — created provenance record

**Errors:** `400 INVALID_BODY`, `400 MISSING_FIELD`, `400 INVALID_FIELD`

> **Immutability:** `PUT /v1/provenance/{id}`, `PATCH /v1/provenance/{id}`, and `DELETE /v1/provenance/{id}` all return `405 PROVENANCE_IMMUTABLE`. Provenance records can never be modified after creation.

---

### `GET /v1/lineage/trade/{trade_id}`

Trade forensics record — the data observations, quality-gate snapshot, and market state at time of trade execution.

**Path param:** `trade_id`

**Response `200`** — `{"data": TradeForensicsRecord, "metadata": {...}}`

**Errors:** `404 TRADE_NOT_FOUND`

---

### `GET /v1/lineage/strategy/{strategy_id}`

Recent forensics records for a strategy (newest first).

**Query params:** `limit` (int, default 50, 1–1000)

**Response `200`** — `{"data": [TradeForensicsRecord, ...], "metadata": {..., "strategy_id", "count"}}`

---

### `GET /v1/lineage/instrument/{instrument_id}`

Recent forensics records for an instrument (newest first).

**Query params:** `limit` (int, default 50, 1–1000)

**Response `200`** — `{"data": [TradeForensicsRecord, ...], "metadata": {..., "instrument_id", "count"}}`

---

## Replay Engine

---

### `POST /v1/replay/sessions`

Create a historical data replay session.

**Request body**
```json
{
  "symbol":          "NIFTY",
  "speedMultiplier": 2.0,
  "tickData": [
    { "time": 1705276800000, "ltp": 22050.0, "volume": 1200 },
    { "time": 1705276860000, "ltp": 22065.5, "volume": 800 }
  ]
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `symbol` | string | ✅ | — | NSE trading symbol |
| `speedMultiplier` | float | | 1.0 | Playback speed (must be >0) |
| `tickData` | array | ✅ | — | Array of tick dicts, each must have `time` (epoch ms) |

**Response `201`**
```json
{
  "data": {
    "sessionId":       "550e8400-e29b-41d4-a716-446655440000",
    "symbol":          "NIFTY",
    "startTs":         1705276800000,
    "endTs":           1705280400000,
    "speedMultiplier": 2.0,
    "status":          "CREATED",
    "tickCount":       60,
    "ticksSent":       0,
    "createdAt":       "2026-01-15T09:15:00.000Z",
    "startedAt":       null,
    "completedAt":     null
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "HISTORICAL" }
}
```

**Errors:** `422` (Pydantic validation — missing `time` field, `speedMultiplier ≤ 0`)

---

### `GET /v1/replay/sessions/{session_id}/status`

Replay session status.

**Path param:** `session_id` — UUID from the create response

**Response `200`** — same shape as create, with `status` one of `CREATED` / `PLAYING` / `COMPLETED` / `FAILED` and `ticksSent` updated in real time

**Errors:** `404 NOT_FOUND`

---

## Internal / Event Bus

---

### `POST /v1/internal/dataset-ready`

Publish a "dataset ready" event to the Redis stream. Used internally after a backfill completes to trigger downstream consumers (quality engine, signal engine).

**Request body**
```json
{
  "market":       "NSE",
  "symbol":       "HDFCBANK",
  "interval":     "5m",
  "date":         "2024-08-01",
  "record_count": 75
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `market` | string | ✅ | Exchange/market identifier |
| `symbol` | string | ✅ | Trading symbol |
| `interval` | string | ✅ | Candle interval (`3m` blocked) |
| `date` | string | ✅ | `YYYY-MM-DD` format |
| `record_count` | int | ✅ | Number of records in the dataset |

**Response `200`**
```json
{
  "data": {
    "msgId":        "1705276800000-0",
    "market":       "NSE",
    "symbol":       "HDFCBANK",
    "interval":     "5m",
    "date":         "2024-08-01",
    "record_count": 75
  },
  "metadata": { "requestedAt": "...", "dataSourceType": "INTERNAL" }
}
```

**Errors:** `400` (Pydantic — bad date format, `3m` interval), `502 EVENT_PUBLISH_FAILED`, `503 STREAMING_ENGINE_UNAVAILABLE`

---

## Canonical Types Reference

### Candle bar (OHLCV)

```typescript
interface CandleBar {
  time:              number;   // UTC epoch seconds
  open:              number;
  high:              number;
  low:               number;
  close:             number;
  volume:            number;
  oi:                number | null;   // open interest; null when unavailable
  volumeUnavailable: boolean;
}
```

### MDQuote (live market quote)

```typescript
interface MDQuote {
  instrumentId:        string;
  symbol:              string;
  exchange:            string;
  ltp:                 number | null;   // null when market closed or data unavailable
  open:                number | null;
  high:                number | null;
  low:                 number | null;
  prevClose:           number | null;
  change:              number | null;
  changePct:           number | null;
  volume:              number;
  volumeUnavailable:   boolean;
  oi:                  number | null;
  oiMissing:           boolean;
  tradedValue:         number | null;
  totalBuyQty:         number | null;
  totalSellQty:        number | null;
  upperCircuit:        number | null;
  lowerCircuit:        number | null;
  weekHigh52:          number | null;
  weekLow52:           number | null;
  lastTradeTime:       string | null;
  bid:                 number | null;
  ask:                 number | null;
  bidAskMissing:       boolean;
  marketStatus:        "REGULAR" | "PRE_OPEN" | "POST_MARKET" | "AFTER_HOURS" | "CLOSED" | "UNKNOWN";
  provider:            string;
  normalisationVersion: string;
  provenance:          { source: string; timestamp: string; sourceType: string | null };
}
```

### DataSourceType

| Value | Meaning |
|---|---|
| `LIVE` | Real-time market data during active session |
| `HISTORICAL` | Data retrieved from DB / historical archive |
| `CACHED` | Served from cache (last known value) |
| `DERIVED` | Computed / aggregated (not raw market data) |
| `INTERNAL` | Internal event / control message |

### InstrumentClass (for backfill)

| Value | Meaning |
|---|---|
| `EQ` | NSE cash equity |
| `IDX` | NSE index (NIFTY, BANKNIFTY, etc.) |
| `FO` | NSE futures & options (F&O) |

---

*For Prometheus metrics format, see `/metrics`. For interactive API docs, the FastAPI `/docs` UI is available in development mode (`ENVIRONMENT=development`).*
