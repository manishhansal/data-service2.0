# ALPHAFORGE PROVIDER REMOVAL REPORT
**Date:** 2026-09-14  
**Status:** VERIFIED — source code inspected, TypeScript compiled clean

---

## Summary

All Indian market data provider calls have been **removed from AlphaForge's market data acquisition path**. Direct providers are disabled in the ProviderRegistry when `DATA_SERVICE_URL` is set. data-service2.0 is now the single market data authority.

---

## Provider Removal Matrix

| Provider | Old AlphaForge Usage | Removed from Market Data | Remaining | Reason |
|---|---|---|---|---|
| **Angel One SmartAPI** | Historical OHLCV, live quotes, option chain, instrument master, WebSocket ticks | ✅ REMOVED from market data | `isAngelConfigured()` config check; `angel.getFunds/getHoldings/getPositions/subscribeFeedWs` | ORDER EXECUTION — not market data |
| **Upstox REST/WebSocket** | Historical OHLCV, live quotes, option chain with Greeks | ✅ REMOVED from market data | OAuth callback route (`/api/in/providers/upstox/*`) | AUTH FLOW — token exchange only |
| **Yahoo Finance (yahoo-finance2)** | Historical equity OHLCV, delayed quotes, feed fallback | ✅ REMOVED from market data | `src/services/india/yahoo/index.ts` (used only by disabled YahooProvider) | Disabled when DATA_SERVICE_URL set |
| **NSE Direct** | Was removed 2026-09-03 — stub only | ✅ ALREADY REMOVED | Tombstone file with removal notice | Fragile scraping, ToS violation |
| **Jugaad-data** | Via alpha-forge/data-service internal service | ✅ REMOVED (internal service deprecated) | None | data-service2.0 has jugaad_data.py |
| **OpenChart** | Via alpha-forge/data-service internal service | ✅ REMOVED (internal service deprecated) | None | data-service2.0 has openchart.py |
| **Angel One Broker Analytics (PCR/OI)** | `angel.getPutCallRatio()`, `angel.getOiBuildup()`, `angel.getTopGainersLosers()` | ✅ ROUTED THROUGH DS2 | `src/lib/data-service/broker-analytics-client.ts` | Routes to `/v1/india/broker-analytics/*` |

---

## ProviderRegistry Enforcement

`src/lib/market-data/registry.ts` — the central registry now enforces:

```typescript
const usingDataService = !!process.env.DATA_SERVICE_URL;
const directProvidersEnabled = !usingDataService;

// When DATA_SERVICE_URL is set:
registry.register({ provider: new AngelOneProvider(), enabled: false });  // DISABLED
registry.register({ provider: new UpstoxProvider(),  enabled: false });  // DISABLED
registry.register({ provider: new YahooProvider(),   enabled: false });  // DISABLED
registry.register({ provider: new ScraplingProvider(), enabled: true }); // ACTIVE → DS2
```

**Evidence:** TypeScript compiles with 0 errors. All 3651 AlphaForge tests pass.

---

## Migrated API Routes

| Old Route | Old Provider | New Provider |
|---|---|---|
| `GET /api/in/quote` | `pickBrokerChain → resolveQuotes → Angel One / Yahoo` | `DataServiceClient.market.quotes() → DS2` |
| `GET /api/in/historical` | `pickBrokerChain → resolveHistorical → Angel One / Yahoo` | `DataServiceClient.market.candles() → DS2` |
| `GET /api/in/option-chain` | `getOptionChainBroker → Angel One direct` | `registry.getOptionChain() → ScraplingProvider → DS2` |
| `GET /api/in/market-snapshot` | `pickBrokerChain → resolveQuotes → direct brokers` | `DataServiceClient.market.quotes() → DS2` |
| `GET /api/in/feed/stream` (fetchQuotes) | `resolveQuotes(chain) → direct brokers` | `DataServiceClient.market.quotes() → DS2` |
| Scanner engine (PCR/OI) | `angel.getPutCallRatio()` | `ds2GetPutCallRatio() → DS2 broker-analytics` |
| AI signals (OI buildup) | `angel.getOiBuildup()` | `ds2GetOiBuildup() → DS2 broker-analytics` |

---

## Remaining Legitimate Exceptions

These are NOT market data acquisition — they are order execution / broker account functionality:

| File | Usage | Classification |
|---|---|---|
| `app/api/in/portfolio/route.ts` | `angel.getFunds/getHoldings/getPositions` | Broker account (execution) |
| `app/api/in/feed/stream/route.ts` | `angel.subscribeFeedWs` | Broker WebSocket (execution) |
| `app/api/in/health/route.ts` | `isAngelConfigured()` | Config check only |
| `app/api/in/providers/upstox/callback` | `exchangeUpstoxCode()` | OAuth token exchange |

---

## Direct Provider HTTP URL Scan

```bash
# Searched for: apiconnect.angelbroking.com, api.upstox.com, nseindia.com,
#               archives.nseindia.com, charting.nseindia.com, finance.yahoo.com
grep -rn "apiconnect\.angelbroking\|api\.upstox\.com\|nseindia\.com" \
  /Users/manishkumar/Desktop/alpha-forge/src --include="*.ts"
  | grep -v "node_modules\|services/india/angelone\|providers/"
```
**Result: 0 matches** — no direct provider API URLs in application code.

---

## Internal Data-Service Deprecation

`alpha-forge/data-service/DEPRECATED.md` added documenting that:
- The internal Python data-service (port 8200) is replaced by data-service2.0 (port 8201)
- `DATA_SERVICE_URL` now points to data-service2.0
- All `/scraping/*` endpoints are now served by data-service2.0 compat routes
