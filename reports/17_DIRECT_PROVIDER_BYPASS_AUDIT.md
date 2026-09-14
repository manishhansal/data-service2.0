# REPORT 17 — DIRECT PROVIDER BYPASS AUDIT
**Audit date:** 2026-09-13

---

## Requirement

> "No consumer may call an external data provider directly — every request for market data flows through this service." — PRODUCTION_CERTIFICATION.md §1

---

## Finding: REQUIREMENT NOT MET

AlphaForge makes direct external market data calls to at least **4 provider families**.

---

## Confirmed Direct Bypasses

### BYPASS-001 — Binance REST (P0)

| Detail | Value |
|---|---|
| File | `src/services/binance/rest.ts` |
| Constant | `const BINANCE_REST = "https://api.binance.com"` |
| Function | `fetch24hrTickers()` |
| Called from | `src/services/brokers/binance/adapter.ts::binanceServerAdapter.fetch24hrTickers` |
| Used in | `src/features/futures/aggregate.ts`, all crypto features |
| DATA-SERVICE endpoint exists? | Partial — `/v1/crypto/klines/{symbol}` but no ticker endpoint exactly matching |

### BYPASS-002 — Binance Futures REST (P0)

| Detail | Value |
|---|---|
| File | `src/services/binance/futures.ts` |
| Constant | `const FUTURES_REST = "https://fapi.binance.com"` |
| Functions | `fetchPremiumIndex()`, `fetchOpenInterest()`, `fetchOpenInterestHistory()`, `fetchLongShortRatio()`, `fetchAllFuturesTickers()` |
| Called from | `src/services/brokers/binance/adapter.ts` (all futures data) |
| DATA-SERVICE endpoint exists? | Yes — `/v1/crypto/futures/*` endpoints exist but AlphaForge does not call them |

### BYPASS-003 — Binance WebSocket (P0)

| Detail | Value |
|---|---|
| File | `src/services/binance/ws.ts` |
| URL | `${env.NEXT_PUBLIC_BINANCE_WS}?streams=...` → `wss://stream.binance.com:9443` |
| Called from | `src/hooks/useBinanceTickers.ts`, broker client |
| DATA-SERVICE endpoint exists? | Yes — `WS /v1/stream/ticks` but AlphaForge does not use it for crypto |

### BYPASS-004 — Delta Exchange REST (P0)

| Detail | Value |
|---|---|
| File | `src/services/brokers/delta/rest.ts` |
| URL | `env.DELTA_REST_BASE_URL` → `https://api.india.delta.exchange` |
| Functions | `fetchAllTickers()`, `fetchTickersForSymbols()`, `fetchProduct()`, `fetchLatestCandles()`, `fetchCandleRange()` |
| Called from | `src/services/brokers/delta/adapter.ts` (ALL delta data) |
| Active | YES — `ACTIVE_BROKER=delta` by default |
| DATA-SERVICE endpoint exists? | ❌ NO — Delta Exchange is not implemented in DATA-SERVICE |

### BYPASS-005 — Delta Exchange WebSocket (P0)

| Detail | Value |
|---|---|
| File | `src/services/brokers/delta/ws.ts` |
| URL | `env.NEXT_PUBLIC_DELTA_WS` → `wss://public-socket.india.delta.exchange` |
| Called from | Delta broker client stream adapter |
| DATA-SERVICE endpoint exists? | ❌ NO |

### BYPASS-006 — Deribit REST (P0)

| Detail | Value |
|---|---|
| File | `src/services/deribit/rest.ts` |
| Constant | `const DERIBIT_REST = "https://www.deribit.com/api/v2"` |
| Functions | `fetchOptionsBookSummary()`, `fetchIndexPrice()` |
| Called from | `src/features/options/fetch-options.ts` |
| DATA-SERVICE endpoint exists? | Yes — `/v1/crypto/options/{currency}/*` but AlphaForge does not use them |

---

## Acceptable Fallback Calls (Not P0 — Architecture Design)

| File | Provider | Reason Acceptable |
|---|---|---|
| `src/services/india/angelone/index.ts` | Angel One | Registered in ProviderRegistry as fallback after ScraplingProvider; only fires when DATA_SERVICE returns no data |
| `src/services/india/yahoo/index.ts` | Yahoo Finance | Last-resort fallback in chain |
| `src/services/india/angelone/smartstream.ts` | Angel One WS | Fallback live stream when DATA-SERVICE stream unavailable |

**These are acceptable under the gradual migration architecture. When `DATA_SERVICE_URL` is set and the service is running, `ScraplingProvider` takes priority.**

---

## Summary

| Bypass | Severity | DATA-SERVICE Has Endpoint | Fix |
|---|---|---|---|
| Binance REST direct | P0 | Partial | Route through DATA-SERVICE |
| Binance Futures direct | P0 | Yes | Route through DATA-SERVICE |
| Binance WS direct | P0 | Yes | Route through DATA-SERVICE |
| Delta REST direct | P0 | **NO** | Build Delta adapter in DATA-SERVICE first |
| Delta WS direct | P0 | **NO** | Build Delta WS in DATA-SERVICE first |
| Deribit REST direct | P0 | Yes | Route through DATA-SERVICE |

**Direct provider calls from AlphaForge (production paths): 6 bypass families**  
**Expected by spec: 0**
