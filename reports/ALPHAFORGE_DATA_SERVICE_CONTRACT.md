# ALPHAFORGE ↔ DATA-SERVICE 2.0 CONTRACT
**Version:** 1.0  
**Date:** 2026-09-14  
**Status:** IMPLEMENTED AND VERIFIED

This document is the authoritative shared contract between AlphaForge (consumer) and data-service2.0 (provider). Both repositories implement this contract. Breaking changes to these APIs must be versioned.

---

## 1. Connection

| Parameter | Value |
|---|---|
| Service URL (dev) | `http://localhost:8201` |
| Service URL (prod) | `http://data-service:8200` (internal Docker network) |
| Authentication | `X-API-KEY: {key}` header for `/v1/*` endpoints |
| Compat routes | `/scraping/*`, `/data/gate` — unauthenticated |
| AlphaForge env var | `DATA_SERVICE_URL=http://localhost:8201` |
| AlphaForge env var | `DATA_SERVICE_API_KEY=dev-key-local-1` |

---

## 2. Canonical Timeframes

Both repositories use exactly these intervals. No others.

```
1m | 5m | 10m | 15m | 30m | 1h | 1d | 1w | 1M
```

`3m` is **permanently prohibited**. Requests with `interval=3m` return HTTP 400. The database has a CHECK constraint `interval_str <> '3m'`.

---

## 3. Canonical Symbol Format

| Type | Format | Examples |
|---|---|---|
| NSE equity | Plain symbol, no suffix | `RELIANCE`, `HDFCBANK` |
| NSE index | Plain name | `NIFTY`, `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY` |
| NSE futures | Trading symbol | `NIFTY25JANFUT` |
| NSE options | Trading symbol | `NIFTY25JAN24000CE` |

Yahoo suffix (`.NS`), `^NSEI` etc. are **internal** to data-service2.0. AlphaForge never sends these.

---

## 4. Canonical Fields

### 4.1 OHLCVCandle

```typescript
{
  time: number;           // Unix epoch SECONDS, UTC
  open: number;           // Rupees
  high: number;
  low: number;
  close: number;
  volume: number;         // Contracts/shares; 0 when unavailable
  oi: number | null;      // Open interest; null = not available (NOT 0)
  volumeUnavailable?: boolean;  // true when volume is a 0-placeholder
}
```

### 4.2 MDQuote

```typescript
{
  symbol: string;
  token: string | null;         // Provider token — may be redacted
  exchange: string;             // "NSE", "NFO", "BSE"
  name: string | null;
  ltp: number | null;           // Last traded price
  change: number | null;
  changePct: number | null;
  prevClose: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  volume: number | null;
  oi: number | null;
  provider: string;             // e.g. "angel_one", "yahoo_finance"
  fetchedAt: string;            // ISO-8601 UTC
  marketStatus?: string;        // REGULAR | CLOSED | PRE_OPEN | POST_MARKET
}
```

### 4.3 Timestamp Rules

- All `time` fields in candles are **Unix epoch seconds (UTC)**
- All ISO datetime strings are **UTC with Z suffix** (`2024-01-15T09:15:00.000Z`)
- Live tick `exchangeTimestampMs` is **epoch milliseconds**
- NSE session: 09:15–15:30 IST = 03:45–10:00 UTC

### 4.4 Null Semantics

| Value | Meaning |
|---|---|
| `oi = null` | OI not available from this provider |
| `oi = 0` | OI is genuinely zero |
| `volume = 0, volumeUnavailable = true` | Volume not provided (placeholder) |
| `ltp = null` | No quote available (market closed, no cached data) |

---

## 5. API Endpoints

### 5.1 Compat Routes (Unauthenticated)

Used by `ScraplingProvider` in AlphaForge.

| Method | Path | Description |
|---|---|---|
| GET | `/scraping/historical` | Historical OHLCV candles |
| GET | `/scraping/quotes` | Batch live quotes |
| GET | `/scraping/option-chain` | Option chain snapshot |
| GET | `/scraping/instruments` | Instrument master |
| POST | `/data/gate` | Data quality gate (AlphaForge GateRequest format) |

**Query params for /scraping/historical:**

| Param | Type | Required | Notes |
|---|---|---|---|
| `symbol` | string | Yes | NSE trading symbol |
| `exchange` | string | No | Default: NSE |
| `interval` | string | Yes | Canonical timeframe |
| `from` | ISO date | No | Default: 90 days ago |
| `to` | ISO date | No | Default: today |

**Response shape:**
```json
{"candles": [...], "count": 7, "provider": "yahoo_finance", "symbol": "TCS"}
```

### 5.2 Canonical API Endpoints (Authenticated — X-API-KEY required)

| Method | Path | Description |
|---|---|---|
| GET | `/v1/india/historical` | Historical candles (canonical) |
| GET | `/v1/india/quotes/{symbol}` | Single live quote |
| GET | `/v1/india/quotes/batch` | Batch quotes (up to 200 symbols) |
| GET | `/v1/india/option-chain` | Option chain |
| GET | `/v1/india/market/status` | NSE session status |
| GET | `/v1/india/historical/gaps` | Gap detection |
| GET | `/v1/india/historical/status` | Coverage status |
| POST | `/v1/india/historical/backfill` | Trigger async backfill |
| GET | `/v1/india/historical/backfill/{jobId}` | Poll backfill status |
| GET | `/v1/india/broker-analytics/pcr` | Put-Call Ratio |
| GET | `/v1/india/broker-analytics/oi-buildup` | OI buildup |
| GET | `/v1/india/broker-analytics/gainers-losers` | Gainers/losers |
| GET | `/v1/health/live` | Service health |
| GET | `/v1/quality/evaluate` | Quality gate (DS2 native format) |

### 5.3 Response Envelope

All `/v1/*` responses use:

```json
{"data": <payload>, "metadata": {"requestedAt": "...", "provider": "...", "dataSourceType": "..."}}
```

Error responses:
```json
{"error": {"code": "INTERVAL_NOT_SUPPORTED", "message": "...", "requestId": "..."}}
```

---

## 6. Error Semantics

| Condition | HTTP Status | Code |
|---|---|---|
| 3m interval | 400 | `INTERVAL_NOT_SUPPORTED` |
| Invalid parameter | 400 | `INVALID_PARAMETER` |
| Unauthorized | 401 | `UNAUTHORIZED` |
| Provider unavailable | 503 | `PROVIDER_UNAVAILABLE` |
| Rate limited | 429 | `RATE_LIMITED` |
| Market closed (live quote) | 200 | `marketStatus: CLOSED` (not an error) |
| No data in range | 200 | Empty `candles: []` (not an error) |

AlphaForge MUST NOT convert these into fabricated data or silently drop errors.

---

## 7. Freshness Thresholds

| Data type | Max age | AlphaForge behavior on exceed |
|---|---|---|
| Live quote (REGULAR session) | 5s | Use cached or degrade |
| Live tick | 60s | Mark synthetic |
| Intraday candle | 30s | Re-fetch |
| Daily candle | 4h | Use cached |
| Option chain | 15s | Re-fetch |
| Instrument master | 12h | Use cached |

---

## 8. Provider Hierarchy (Internal to DS2)

AlphaForge does NOT select providers. DS2 handles all routing:

```
Historical equity 1d:    Angel One → Yahoo Finance
Historical equity 1m-1h: Angel One → Upstox
Historical index 1d:     Upstox → Yahoo Finance
Historical F&O EOD:      Jugaad-data (OI) → OpenChart (OHLCV)
Historical reconcile:    OpenChart (secondary source)
Live quotes:             Angel One → Upstox → market status
Option chain:            Angel One → Upstox
Broker analytics:        Angel One SmartAPI only
```

---

## 9. Credential Flow

```
User → AlphaForge Profile → API Keys page
  → Stored in AlphaForge DB (encrypted)
  → Passed to DS2 via ANGEL_ONE_* env vars
  → DS2 authenticates with Angel One on startup
  → DS2 holds JWT token server-side
  → AlphaForge never calls Angel One directly
```

**Credentials never reach the browser.** All env vars with `NEXT_PUBLIC_` prefix for provider keys are prohibited.
