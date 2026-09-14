# REPORT 03 — ALPHAFORGE DATA CONSUMER MATRIX
**Audit date:** 2026-09-13  
**Method:** Full repository walk of alpha-forge/src/ and alpha-forge/worker/

---

## Section A: Crypto Data Consumers

| AF File | Module | Function/Consumer | Current Provider | Exchange | Asset | Data Type | Live/Hist | Fields | DATA-SERVICE Endpoint | Implemented? | Runtime Verified? | Bypass? | Severity |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `src/services/binance/rest.ts` | binance/rest | `fetch24hrTickers()` | **Binance REST direct** `https://api.binance.com` | Binance | Crypto spot | 24h ticker | Live | price,change,high,low,vol,quoteVol | `GET /v1/crypto/klines/{symbol}` | 🟡 | ⛔ | **YES P0** | P0 |
| `src/services/binance/futures.ts` | binance/futures | `fetchPremiumIndex()` | **Binance Futures direct** `https://fapi.binance.com` | Binance | Perp futures | mark price, funding | Live | markPrice,indexPrice,fundingRate | `GET /v1/crypto/futures/overview` | 🟡 | ⛔ | **YES P0** | P0 |
| `src/services/binance/futures.ts` | binance/futures | `fetchOpenInterest()` | **Binance Futures direct** | Binance | Perp futures | OI | Live | openInterest | `GET /v1/crypto/futures/oi-history/{symbol}` | 🟡 | ⛔ | **YES P0** | P0 |
| `src/services/binance/futures.ts` | binance/futures | `fetchOpenInterestHistory()` | **Binance Futures direct** | Binance | Perp futures | OI history | Historical | ts,OI,notionalUsd | `GET /v1/crypto/futures/oi-history/{symbol}` | 🟡 | ⛔ | **YES P0** | P0 |
| `src/services/binance/futures.ts` | binance/futures | `fetchLongShortRatio()` | **Binance Futures direct** | Binance | Perp futures | L/S ratio | Live | ts,longShortRatio | `GET /v1/crypto/futures/long-short/{symbol}` | 🟡 | ⛔ | **YES P0** | P0 |
| `src/services/binance/futures.ts` | binance/futures | `fetchAllFuturesTickers()` | **Binance Futures direct** | Binance | Perp futures | All tickers | Live | symbol,price,changePct,quoteVol | `GET /v1/crypto/futures/overview` | 🟡 | ⛔ | **YES P0** | P0 |
| `src/services/binance/ws.ts` | binance/ws | `BinanceWsClient` | **Binance WebSocket direct** `wss://stream.binance.com` | Binance | Crypto spot | Mini ticker | Live stream | symbol,close,open,high,low,vol | `WS /v1/stream/ticks` | 🟡 | ⛔ | **YES P0** | P0 |
| `src/services/binance/klines.ts` | binance/klines | `fetchKlines()` | Binance (via broker adapter) | Binance | Crypto | OHLCV | Historical | openTime,OHLCV,closeTime | `GET /v1/crypto/klines/{symbol}` | 🟡 | ⛔ | Indirect | P1 |
| `src/services/brokers/delta/rest.ts` | brokers/delta/rest | `fetchAllTickers()` | **Delta Exchange direct** `api.india.delta.exchange` | Delta | Perp futures | Tickers | Live | symbol,close,mark,OI,vol | ❌ NO ENDPOINT | ❌ | ❌ | **YES P0** | P0 |
| `src/services/brokers/delta/rest.ts` | brokers/delta/rest | `fetchTickersForSymbols()` | **Delta Exchange direct** | Delta | Perp futures | Specific tickers | Live | symbol,mark_price,spot_price,OI | ❌ NO ENDPOINT | ❌ | ❌ | **YES P0** | P0 |
| `src/services/brokers/delta/rest.ts` | brokers/delta/rest | `fetchProduct()` | **Delta Exchange direct** | Delta | Perp futures | Product info | Live | annualized_funding | ❌ NO ENDPOINT | ❌ | ❌ | **YES P0** | P0 |
| `src/services/brokers/delta/rest.ts` | brokers/delta/rest | `fetchLatestCandles()` | **Delta Exchange direct** | Delta | Perp futures/OI | OHLCV candles | Historical | time,OHLCV | ❌ NO ENDPOINT | ❌ | ❌ | **YES P0** | P0 |
| `src/services/brokers/delta/rest.ts` | brokers/delta/rest | `fetchCandleRange()` | **Delta Exchange direct** | Delta | Perp futures | OHLCV range | Historical | time,OHLCV | ❌ NO ENDPOINT | ❌ | ❌ | **YES P0** | P0 |
| `src/services/brokers/delta/ws.ts` | brokers/delta/ws | Delta WS client | **Delta WS direct** `wss://public-socket.india.delta.exchange` | Delta | Perp futures | Ticker stream | Live stream | price,mark,OI | ❌ NO ENDPOINT | ❌ | ❌ | **YES P0** | P0 |
| `src/services/deribit/rest.ts` | deribit/rest | `fetchOptionsBookSummary()` | **Deribit direct** `https://www.deribit.com/api/v2` | Deribit | Crypto options | Options book | Live | mark_price,mark_iv,OI,bid,ask | `GET /v1/crypto/options/{currency}/book` | 🟡 | ⛔ | **YES P0** | P0 |
| `src/services/deribit/rest.ts` | deribit/rest | `fetchIndexPrice()` | **Deribit direct** | Deribit | Crypto options | Index price | Live | index_price | `GET /v1/crypto/options/{currency}/overview` | 🟡 | ⛔ | **YES P0** | P0 |

## Section B: Indian Market Consumers — DataServiceClient Path (CORRECT)

| AF File | Module | Function | Provider Chain | DATA-SERVICE Endpoint | Bypass? |
|---|---|---|---|---|---|
| `src/lib/data-service/client.ts` | DataServiceClient | `market.quote()` | ScraplingProvider→AngelOne→Upstox→Yahoo | `GET /v1/india/quotes/{symbol}` | NO |
| `src/lib/data-service/client.ts` | DataServiceClient | `market.quotes()` | Same chain | `GET /v1/india/quotes` | NO |
| `src/lib/data-service/client.ts` | DataServiceClient | `market.candles()` | Same chain | `GET /v1/india/historical` | NO |
| `src/lib/data-service/client.ts` | DataServiceClient | `market.options()` | ScraplingProvider→AngelOne→Upstox | `GET /v1/india/option-chain` | NO |
| `src/lib/data-service/client.ts` | DataServiceClient | `market.instruments()` | ScraplingProvider→AngelOne | `GET /v1/instruments` | NO |
| `src/lib/data-service/client.ts` | DataServiceClient | `universe.fno()` | Same | `GET /v1/instruments/fno-universe` | NO |
| `src/lib/market-data/providers/scrapling.ts` | ScraplingProvider | All methods | DATA-SERVICE first | Various `/v1/india/*` | NO |

## Section C: Indian Market Consumers — Direct Provider Calls (REMAINING)

| AF File | Module | Function | Direct Target | Bypass? | Severity |
|---|---|---|---|---|---|
| `src/services/india/angelone/index.ts` | angelone adapter | `login()`, `smartApiPost()` | `https://apiconnect.angelone.in` | YES — but only as fallback when DATA_SERVICE_URL not set | P1 |
| `src/services/india/yahoo/index.ts` | YahooAdapter | `getQuote()`, `getHistorical()` | `yahoo-finance2` npm package | YES — fallback chain | P1 |
| `src/services/india/angelone/smartstream.ts` | SmartStreamClient | WS connect | Angel One SmartStream | YES — fallback | P1 |
| `src/app/api/in/feed/stream/route.ts` | /api/in/feed/stream | angel import | `angel` service | YES | P1 |
| `src/app/api/in/portfolio/route.ts` | /api/in/portfolio | angel import | `angel` service | YES | P1 |
| `src/app/api/in/health/route.ts` | /api/in/health | `isAngelConfigured()` | `angelone` service | Indirect | P2 |

**Note:** The India path has an internal fallback registry (`ScraplingProvider → AngelOneProvider → UpstoxProvider → YahooProvider`). When `DATA_SERVICE_URL` is set, `ScraplingProvider` hits DATA-SERVICE first. The direct provider calls remain as fallbacks, which is architecturally acceptable for gradual migration, but they bypass DATA-SERVICE persistence and provenance.

## Section D: Worker Jobs

| Worker Job | Data Consumed | Source | Bypass? |
|---|---|---|---|
| `scraping-tick-listener.ts` | Live ticks via Redis pub/sub | DATA-SERVICE publishes → worker subscribes | NO (correct) |
| Signal/alert jobs | Historical candles via broker | `getServerBroker()` → Delta/Binance | YES — crypto bypasses |
| `run-backtest.ts` | Kline candles | `getServerBroker()` → Delta/Binance | YES |

## Section E: AlphaForge Environment Variables for Market Data

| Variable | Value in .env.local | Status | Notes |
|---|---|---|---|
| `DATA_SERVICE_URL` | `http://localhost:8200` | CONFIGURED | Points to DATA-SERVICE |
| `NEXT_PUBLIC_BINANCE_WS` | `wss://stream.binance.com:9443/stream` | CONFIGURED | Direct bypass |
| `NEXT_PUBLIC_BINANCE_FUTURES_WS` | `wss://fstream.binance.com/stream` | CONFIGURED | Direct bypass |
| `NEXT_PUBLIC_DELTA_WS` | `wss://public-socket.india.delta.exchange` | CONFIGURED | Direct bypass |
| `ACTIVE_BROKER` | `delta` | CONFIGURED | Default broker = Delta Exchange |
| `DELTA_REST_BASE_URL` | `https://api.india.delta.exchange` | CONFIGURED | Direct bypass |
| `SMARTAPI_API_KEY` | _(empty)_ | NOT_CONFIGURED | |
| `SMARTAPI_CLIENT_CODE` | _(empty)_ | NOT_CONFIGURED | |
| `SMARTAPI_PIN` | _(empty)_ | NOT_CONFIGURED | |
| `SMARTAPI_TOTP_SECRET` | _(empty)_ | NOT_CONFIGURED | |
| `DERIBIT_CLIENT_ID` | _(empty)_ | NOT_CONFIGURED | |
| `DERIBIT_SECRET` | _(empty)_ | NOT_CONFIGURED | |
| `AUTH_SECRET` | populated | CONFIGURED | |
| `ENCRYPTION_KEY` | populated | CONFIGURED | |
