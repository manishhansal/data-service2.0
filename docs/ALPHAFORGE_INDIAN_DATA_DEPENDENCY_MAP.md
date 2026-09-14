# ALPHAFORGE INDIAN MARKET DATA DEPENDENCY MAP
**Date:** 2026-09-13  
**Auditor:** Forensic inspection — actual source code  
**Status:** VERIFIED (code read, not claimed)

---

## Methodology

Every entry below was derived by:
1. Traversing the full AlphaForge source tree (`src/`, `worker/`, `data-service/`)
2. Reading every file that imports from or calls a provider
3. Tracing call chains from API routes → services → providers

**Not** derived from documentation, tests, or claims.

---

## Summary Finding

AlphaForge has **two independent data architectures** in the same repository:

| Architecture | Path | Port | Status |
|---|---|---|---|
| Internal Python data-service | `alpha-forge/data-service/` | 8200 | ACTIVE (currently serves AlphaForge) |
| data-service2.0 | `../data-service2.0/` | 8200 | NOT CONNECTED (API prefix mismatch) |

AlphaForge's `ScraplingProvider` (priority 0) routes to `/scraping/*` endpoints — which are served by the **internal** data-service, **not** data-service2.0. data-service2.0 exposes `/v1/india/*` endpoints and is currently only contacted by `gate-client.ts` for the `/data/gate` endpoint.

---

## Dependency Table

| AlphaForge Module | File | Provider/API | Data Consumed | Live/Historical | Consumer | Current Path | Target Replacement API |
|---|---|---|---|---|---|---|---|
| ProviderRegistry ScraplingProvider | `src/lib/market-data/providers/scrapling.ts` | Internal data-service via HTTP | Historical OHLCV, live quotes, option chain, instruments | Both | All market data consumers | `DATA_SERVICE_URL/scraping/*` → alpha-forge/data-service | `DATA_SERVICE_URL/v1/india/*` → data-service2.0 |
| ProviderRegistry AngelOneProvider | `src/lib/market-data/providers/angel-one.ts` | Angel One SmartAPI REST + SmartStream WebSocket | Historical OHLCV 1m-1d, live quotes, option chain, instrument master | Both | All market data consumers (priority 1 fallback) | Direct `apiconnect.angelbroking.com` | data-service2.0 via ScraplingProvider |
| ProviderRegistry UpstoxProvider | `src/lib/market-data/providers/upstox.ts` | Upstox REST API v2/v3 + WebSocket | Historical OHLCV, live quotes, option chain with Greeks | Both | All market data consumers (priority 2 fallback) | Direct `api.upstox.com` | data-service2.0 via ScraplingProvider |
| ProviderRegistry YahooProvider | `src/lib/market-data/providers/yahoo.ts` | yahoo-finance2 npm | Historical equity OHLCV, delayed live quotes | Both | All market data consumers (priority 3 fallback) | Direct Yahoo Finance API | data-service2.0 via ScraplingProvider |
| InstrumentMasterService | `src/lib/market-data/providers/angel-one.ts` | Angel One ScripMaster CSV | Instrument master dump (EQ/IDX/OPTIDX/OPTSTK/FUTIDX/FUTSTK) | On-demand | Instrument resolution, option chain | Direct SmartAPI | data-service2.0 /v1/india/instruments |
| Historical service | `src/lib/market-data/services/historical.service.ts` | ProviderRegistry | OHLCV candles all intervals except 3m | Historical | Signal engine, ML, backtester, API routes | Registry→ScraplingProvider→AngelOne→Upstox→Yahoo | data-service2.0 /v1/india/historical |
| Live feed service | `src/lib/market-data/services/live-feed.service.ts` | ProviderRegistry | LiveTick (LTP, OHLC, volume, OI) | Live | Worker realtime candles, signal engine | Registry→AngelOne SmartStream→Upstox WS→Yahoo poll | data-service2.0 WebSocket / SSE |
| Option chain service | `src/lib/market-data/services/option-chain.service.ts` | ProviderRegistry | Full option chain, Greeks, OI, IV | Live | Signal engine, daily picks, scalper | Registry→AngelOne→Upstox | data-service2.0 /v1/india/option-chain |
| Instrument master service | `src/lib/market-data/services/instrument-master.service.ts` | ProviderRegistry | Instrument list by exchange/type | On-demand | Universe, backtester, instrument search | Registry→ScraplingProvider→AngelOne | data-service2.0 /v1/india/instruments |
| DataServiceClient (SDK) | `src/lib/data-service/client.ts` | ProviderRegistry | All market data operations | Both | All server-side consumers | ScraplingProvider (priority 0) | data-service2.0 (already correct architecture, wrong URL) |
| DataQualityGate client | `src/lib/data-service/gate-client.ts` | data-service2.0 `/data/gate` | Signal quality gate evaluation | Live | Signal engine | data-service2.0 POST /data/gate | ✅ Already correct |
| AI Signals builder | `src/features/ai-signals/india-builder.ts` | angel (broker analytics only) | PCR, OI buildup, gainers/losers | Live | AI signal generation | Direct `@/services/india/angelone` | data-service2.0 /v1/india/broker-analytics/* |
| Daily picks builder | `src/features/india/daily-picks/builder.ts` | angel (broker analytics only) | PCR, OI buildup; market quotes via registry | Both | Daily pick generation | Direct `@/services/india/angelone` | data-service2.0 /v1/india/broker-analytics/* |
| Feed stream SSE | `src/app/api/in/feed/stream/route.ts` | angel.subscribeFeedWs | WebSocket tick stream | Live | Browser quote feed | Direct Angel One SmartStream | data-service2.0 WebSocket/SSE endpoint |
| Portfolio | `src/app/api/in/portfolio/route.ts` | angel (broker account) | Funds, holdings, positions | Live | Portfolio UI | Direct Angel One | **EXCEPTION**: order/account data, not market data |
| Health check | `src/app/api/in/health/route.ts` | isAngelConfigured() | Angel One config status | Status | Health UI | Direct config check | Keep (config check only) |
| Upstox OAuth | `src/app/api/in/providers/upstox/callback/route.ts` | Upstox OAuth | Access token exchange | Auth | OAuth flow | Direct Upstox OAuth | Route through data-service2.0 credential store |
| Upstox status | `src/app/api/in/providers/upstox/status/route.ts` | Upstox token state | Connection status | Status | Settings UI | Direct token state | data-service2.0 /v1/providers/upstox/status |
| Worker: Realtime candles | `worker/src/jobs/india-realtime-candles.ts` | ProviderRegistry (subscribeLiveFeed) | Live ticks → candle builder → DB | Live | Realtime candle persistence | Registry→AngelOne SmartStream | data-service2.0 Redis pub/sub via scraping-tick-listener |
| Worker: Scraping tick listener | `worker/src/jobs/scraping-tick-listener.ts` | Redis pub/sub `af:ticks:*` | Live ticks published by internal data-service | Live | Realtime candle pipeline | Internal data-service Redis publish | data-service2.0 Redis pub/sub |
| FNO Backfill runner | `src/lib/market-data/services/fno-backfill-runner.service.ts` | ProviderRegistry | Historical F&O OHLCV + OI | Historical | F&O data coverage | Registry→ScraplingProvider→AngelOne→Upstox | data-service2.0 /v1/india/historical/backfill |
| Candle builder | `src/lib/market-data/services/candle-builder.service.ts` | ProviderRegistry + Redis | Real-time candle construction | Live | Worker, signal engine | Internal data-service tick stream | data-service2.0 tick stream |
| Option chain capture | `src/features/india/scalping/option-chain-capture.ts` | ProviderRegistry getOptionChain | Option chain snapshot | Live | Scalper strategy | Registry→AngelOne→Upstox | data-service2.0 /v1/india/option-chain |
| India internal data-service: jugaad | `data-service/src/providers/jugaad/adapter.py` | jugaad-data PyPI package | NSE F&O EOD bhavcopy (OHLCV + OI) | Historical | Internal data-service historical endpoint | Direct `archives.nseindia.com` | data-service2.0 (already has jugaad_data.py) |
| India internal data-service: openchart | `data-service/src/providers/openchart/adapter.py` | OpenChart NSE charting | Historical OHLCV all timeframes | Historical | Internal data-service historical endpoint | Direct `charting.nseindia.com` | data-service2.0 (already has openchart.py) |
| India internal data-service: Upstox broker | `data-service/src/brokers/upstox_client.py` | Upstox REST API | Quotes + historical candles | Both | Internal data-service broker endpoints | Direct `api.upstox.com` | data-service2.0 (already has upstox.py) |
| India internal data-service: NSE scraping | `data-service/src/scrapers/historical.py` | NSE/BSE charting endpoints | NSE intraday + daily candles | Both | Internal data-service /scraping/historical | Direct NSE charting API | data-service2.0 scrapling_nse.py |
| India internal data-service: NSE live | `data-service/src/scrapers/live_quotes.py` | NSE API (nextapi) | Live quotes, LTP | Live | Internal data-service /scraping/quotes | Direct NSE nextapi | data-service2.0 via Angel One / Upstox |
| India internal data-service: option chain | `data-service/src/scrapers/option_chain.py` | NSE option chain | Full option chain | Live | Internal data-service /scraping/option-chain | Direct NSE option chain URL | data-service2.0 /v1/india/option-chain |
| Sector stocks | `src/app/api/in/sector-stocks/route.ts` | ProviderRegistry | Sector constituent quotes | Live | Dashboard | Registry | data-service2.0 /v1/india/quotes batch |
| Scanner | `src/services/india/scanner/engine.ts` | ProviderRegistry | Live quotes, historical candles | Both | Signal engine, scanner UI | Registry | data-service2.0 batch endpoints |

---

## Interfaces That Must Be Preserved (Not Market Data)

| Module | Purpose | Exception Reason |
|---|---|---|
| `angel.getFunds()` | Broker account margin | ORDER EXECUTION — not market data |
| `angel.getHoldings()` | Demat holdings | ORDER EXECUTION — not market data |
| `angel.getPositions()` | Net positions | ORDER EXECUTION — not market data |
| `angel.subscribeFeedWs()` | Real-time quote feed for execution | BROKER FEED (acknowledged in DATA_SERVICE_PRE_REFACTOR_AUDIT.md) |
| Upstox OAuth callback | Access token exchange for user authentication | AUTH flow — not market data |

---

## Direct Provider Dependency Inventory

| Dependency | Type | Where Used | Action Required |
|---|---|---|---|
| `@/services/india/angelone` (SmartAPI REST) | Direct broker | `providers/angel-one.ts`, `features/ai-signals/india-builder.ts`, `features/india/daily-picks/builder.ts`, `app/api/in/feed/stream`, `app/api/in/portfolio` | Remove market-data usage; retain broker analytics + execution |
| `@/services/india/upstox` (Upstox REST) | Direct broker | `providers/upstox.ts`, OAuth callback route | Remove market-data usage; retain auth flow |
| `@/services/india/yahoo` | Direct Yahoo Finance | `providers/yahoo.ts` | Remove; route through data-service2.0 |
| `yahoo-finance2` npm package | Direct Yahoo Finance | `src/services/india/yahoo/index.ts` | Remove after migration |
| `data-service/` internal Python service | Internal data-service | AlphaForge ScraplingProvider (`/scraping/*`) | Migrate to data-service2.0 + remove |
| `charting.nseindia.com` | NSE scraping | `data-service/src/scrapers/` | Already in data-service2.0 scrapling_nse.py |
| `archives.nseindia.com` | NSE bhavcopy | `data-service/src/providers/jugaad/` | Already in data-service2.0 jugaad_data.py |
| `api.upstox.com` | Upstox REST | `data-service/src/brokers/upstox_client.py` | Already in data-service2.0 upstox.py |

---

## Critical Path Discovery

The central architectural problem is:

```
AlphaForge ScraplingProvider (priority 0)
  calls: /scraping/historical, /scraping/quotes, /scraping/option-chain, /scraping/instruments
  these endpoints are served by: alpha-forge/data-service/ (internal Python service)
  NOT by: data-service2.0

data-service2.0 exposes:
  /v1/india/historical
  /v1/india/quotes/{symbol}
  /v1/india/option-chain
  /v1/india/market/status
  /v1/india/historical/gaps

The ScraplingProvider URL paths (/scraping/*) do not match data-service2.0 paths (/v1/india/*).
```

**Fix required:** Either:
1. Add `/scraping/*` compatibility routes to data-service2.0 (recommended — allows zero change to AlphaForge registry logic), OR
2. Update `ScraplingProvider.dsGet()` to use `/v1/india/*` paths

Option 1 is preferred: non-breaking, doesn't change AlphaForge's provider-selection logic.
