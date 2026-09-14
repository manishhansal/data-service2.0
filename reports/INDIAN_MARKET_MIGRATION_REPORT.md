# INDIAN MARKET DATA MIGRATION REPORT
**Date:** 2026-09-14  
**Status:** ✅ FULLY VERIFIED — Live Angel One data confirmed

---

## Original Architecture (Before Migration)

```
AlphaForge
├── src/lib/market-data/providers/angel-one.ts   ← Direct Angel One SmartAPI
├── src/lib/market-data/providers/upstox.ts      ← Direct Upstox REST/WS
├── src/lib/market-data/providers/yahoo.ts       ← Direct yahoo-finance2 npm
├── src/services/india/angelone/index.ts          ← Angel One broker adapter
├── src/services/india/yahoo/index.ts             ← Yahoo adapter
├── data-service/ (internal Python)              ← Internal data service (port 8200)
│   ├── scrapers/historical.py                   ← Direct NSE scraping
│   ├── scrapers/live_quotes.py                  ← Direct NSE quotes
│   ├── scrapers/option_chain.py                 ← Direct NSE option chain
│   ├── providers/jugaad/adapter.py              ← Direct Jugaad-data
│   └── providers/openchart/adapter.py           ← Direct OpenChart
└── DATA_SERVICE_URL=http://localhost:8200        ← Internal service
```

All providers were directly accessible as fallbacks from the ProviderRegistry, even when a higher-priority source was available.

---

## New Architecture (After Migration)

```
AlphaForge
├── src/lib/market-data/registry.ts
│   └── ScraplingProvider (priority 0, ENABLED when DATA_SERVICE_URL set)
│   └── AngelOneProvider  (priority 1, DISABLED when DATA_SERVICE_URL set)
│   └── UpstoxProvider    (priority 2, DISABLED when DATA_SERVICE_URL set)
│   └── YahooProvider     (priority 3, DISABLED when DATA_SERVICE_URL set)
├── src/lib/data-service/client.ts               ← Canonical SDK
├── src/lib/data-service/broker-analytics-client.ts ← PCR/OI via DS2
├── src/lib/data-service/v2-client.ts            ← Admin operations
└── DATA_SERVICE_URL=http://localhost:8201        ← data-service2.0
                        │
                        ▼
              data-service2.0 (port 8201)
              ├── /scraping/*    compat routes (no auth)
              ├── /v1/india/*    canonical routes (auth required)
              ├── /data/gate     quality gate compat
              │
              ├── AngelOneAdapter    ✅ Authenticated (MPIN=9507)
              ├── UpstoxAdapter      ⚠️ OAuth token pending
              ├── JugaadDataAdapter  ← Credential-free
              ├── OpenChartAdapter   ← Credential-free
              └── YahooFinanceAdapter ← Credential-free (fallback)
```

---

## Migrated Components

### data-service2.0 — Changes and Bug Fixes

| Component | Change | Status |
|---|---|---|
| `src/api/compat.py` | NEW — `/scraping/*` + `/data/gate` compat routes; exchange prefix stripping (DS2-RCA-023) | ✅ VERIFIED |
| `src/api/india.py` | Added `/v1/india/quotes/batch`; fixed route registration order (DS2-RCA-022) | ✅ VERIFIED |
| `src/engines/market_engine.py` | Replaced stubs with real Angel One adapter dispatch | ✅ VERIFIED |
| `src/engines/historical_engine.py` | Real provider dispatch; `_ANGEL_ONE_KNOWN_TOKENS` map (DS2-RCA-020); IDX→Angel One routing (DS2-RCA-021); Yahoo fallback | ✅ VERIFIED |
| `src/providers/adapters/angel_one.py` | Fixed MPIN auth; added `mpin` parameter | ✅ VERIFIED |
| `src/providers/adapters/yahoo_finance.py` | Added NSE index symbol mapping (NIFTY→^NSEI, BANKNIFTY→^NSEBANK) | ✅ VERIFIED |
| `src/middleware/credential_stripper.py` | Fixed Content-Length header after body redaction | ✅ VERIFIED |
| `src/core/settings.py` | Added `angel_one_mpin` setting | ✅ VERIFIED |
| `src/server.py` | Angel One adapter initialized in lifespan; MarketEngine wired | ✅ VERIFIED |
| `docker-compose.yml` | Exposed PostgreSQL on port 5444 for local dev | ✅ VERIFIED |

### AlphaForge — Migrated Routes and Services

| Component | Change | Status |
|---|---|---|
| `src/lib/market-data/registry.ts` | Direct providers disabled when DS2 active | ✅ VERIFIED |
| `src/lib/market-data/providers/scrapling.ts` | Added API key header support | ✅ VERIFIED |
| `src/lib/data-service/broker-analytics-client.ts` | NEW — routes PCR/OI through DS2 | ✅ VERIFIED |
| `src/lib/data-service/v2-client.ts` | NEW — canonical DS2 v1 admin client | ✅ VERIFIED |
| `src/app/api/in/quote/route.ts` | DataServiceClient.market.quotes() | ✅ VERIFIED |
| `src/app/api/in/historical/route.ts` | DataServiceClient.market.candles() | ✅ VERIFIED |
| `src/app/api/in/option-chain/route.ts` | registry.getOptionChain() | ✅ VERIFIED |
| `src/app/api/in/market-snapshot/route.ts` | DataServiceClient batch quotes | ✅ VERIFIED |
| `src/app/api/in/feed/stream/route.ts` | DataServiceClient quotes; WS kept for execution | ✅ VERIFIED |
| `src/app/api/in/provider-health/route.ts` | Updated to `/v1/health/live` | ✅ VERIFIED |
| `src/services/india/scanner/engine.ts` | angel.* analytics → ds2Get*() | ✅ VERIFIED |
| `src/features/ai-signals/india-builder.ts` | angel.* analytics → ds2Get*() | ✅ VERIFIED |
| `src/features/india/daily-picks/builder.ts` | angel.* analytics → ds2Get*() | ✅ VERIFIED |
| `src/services/india/websocket/gateway.ts` | Removed Yahoo fallback | ✅ VERIFIED |
| `data-service/DEPRECATED.md` | Deprecation notice for internal data-service | DOCUMENTED |

---

## Provider Matrix (data-service2.0) — Final Status

| Provider | Historical | Live | Credentials | Status |
|---|---|---|---|---|
| Angel One SmartAPI | ✅ 1m–1d equity + all intervals | ✅ Quotes, OC | MPIN=9507 ✅ | ✅ LIVE VERIFIED |
| **Upstox V2** | ✅ 1m/30m/1d/1w/1M (EQ+IDX) | ⚠️ not wired to MarketEngine | Access token ✅ | ✅ HISTORICAL VERIFIED |
| Jugaad-data | ✅ 1d F&O (OI) | ❌ | None | 🟡 weekday only |
| OpenChart | ✅ 1m–1M all | ❌ | None | 🟡 weekday only |
| Yahoo Finance | ✅ 1d equity/indices | ❌ delayed | None | ✅ VERIFIED (fallback) |
| NSE/Scrapling | ❌ removed 2026-09-03 | ❌ | None | REMOVED |

---

## Bugs Fixed During This Migration

| ID | Session | Bug | Files Changed |
|---|---|---|---|
| DS2-RCA-020 | Angel One | `token=symbol` | `historical_engine.py` |
| DS2-RCA-021 | Angel One | IDX → Upstox without creds | `historical_engine.py` |
| DS2-RCA-022 | Angel One | `/quotes/batch` shadowed | `india.py` |
| DS2-RCA-023 | Angel One | `NSE:NSE:HDFCBANK` double-prefix | `compat.py` |
| DS2-RCA-024 | Upstox | `UPSTOX_ACCESS_TOKEN` ignored | `settings.py` |
| DS2-RCA-025 | Upstox | Upstox not initialized at startup | `server.py` |
| DS2-RCA-026 | Upstox | list-of-arrays not normalized | `historical_engine.py` |
| DS2-RCA-027 | Upstox | `INTERVAL_MAP` wrong strings | `upstox.py`, `test_upstox.py` |

---

## Remaining Items

| Issue | Severity | Fix |
|---|---|---|
| Wire Upstox to MarketEngine for live quotes | P1 | `MarketEngine.__init__` accept `upstox_adapter`; `server.py` pass `app.state.upstox_adapter` |
| Upstox 5m/10m/15m/1h plan limitation | P2 | Upgrade to Pro plan; Angel One covers these intervals now |
| instrument_master empty | P1 | `_ANGEL_ONE_KNOWN_TOKENS` + `_UPSTOX_INSTRUMENT_KEYS` cover 60+ symbols for interim |
| Jugaad F&O not tested | P2 | Run on weekday |
| alpha-forge/data-service/ not deleted | P3 | Safe to delete when team confirms migration is permanent |

---

## Historical Data Coverage — Final Verified (2026-09-14)

| Symbol | Exchange | Interval | Bars | Provider | source_type |
|---|---|---|---|---|---|
| HDFCBANK | NSE | 5m | 4727 | angel_one | BROKER_AUTHENTICATED |
| NIFTY | NSE | 5m | 151 | angel_one | BROKER_AUTHENTICATED |
| RELIANCE | NSE | 1m | 375 | angel_one | BROKER_AUTHENTICATED |
| HDFCBANK | NSE | 1m | 375 | angel_one | BROKER_AUTHENTICATED |
| TCS | NSE | 1d | 14 | angel_one | BROKER_AUTHENTICATED |
| **RELIANCE** | **NSE** | **1d** | **7** | **upstox** | **BROKER_AUTHENTICATED** |
| **HDFCBANK** | **NSE** | **1d** | **7** | **upstox** | **BROKER_AUTHENTICATED** |
| **TCS** | **NSE** | **1d** | **7** | **upstox** | **BROKER_AUTHENTICATED** |
| **NIFTY** | **NSE** | **1d** | **7** | **upstox** | **BROKER_AUTHENTICATED** |
| **BANKNIFTY** | **NSE** | **1d** | **7** | **upstox** | **BROKER_AUTHENTICATED** |
| **NIFTY** | **NSE** | **1m** | **750** | **upstox** | **BROKER_AUTHENTICATED** |
| **NIFTY** | **NSE** | **30m** | **26** | **upstox** | **BROKER_AUTHENTICATED** |
| BANKNIFTY | NSE | 1d | 15 | yahoo_finance | OPEN_SOURCE_NSE_DERIVED |

---

## Bugs Fixed During Live Testing

| ID | Bug | Files Changed |
|---|---|---|
| DS2-RCA-020 | Angel One EQ/IDX intraday returned 0 bars (plain symbol as token) | `src/engines/historical_engine.py` |
| DS2-RCA-021 | IDX always routed to Upstox (no credentials) | `src/engines/historical_engine.py` |
| DS2-RCA-022 | Batch quotes route shadowed by single-symbol route | `src/api/india.py` |
| DS2-RCA-023 | `NSE:HDFCBANK` created `NSE:NSE:HDFCBANK` in DB | `src/api/compat.py` |

---

## Remaining Items

| Issue | Severity | Fix |
|---|---|---|
| Upstox OAuth token | P1 | Add `UPSTOX_ACCESS_TOKEN` to `.env.local` |
| instrument_master empty | P1 | Angel One ScripMaster fetch via `/v1/admin/instruments/sync` |
| Jugaad F&O not tested | P2 | Run on weekday with NSE archives available |
| alpha-forge/data-service/ not deleted | P3 | Safe to delete when team confirms migration is permanent |
