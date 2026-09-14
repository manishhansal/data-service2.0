# REPORT 04 — ALPHAFORGE DATA API PARITY
**Audit date:** 2026-09-13

---

## Indian Market API Parity

| AlphaForge Consumer | Expected Data | DATA-SERVICE Endpoint | Implemented | Runtime Tested | Fields Matched | Status |
|---|---|---|---|---|---|---|
| `DataServiceClient.market.quote()` | `MDQuote{symbol,token,ltp,change,changePct,prevClose,open,high,low,volume,oi,bid,ask}` | `GET /v1/india/quotes/{symbol}` | ✅ | ⛔ BLOCKED | 🟡 code-only | BLOCKED |
| `DataServiceClient.market.quotes()` | Array of MDQuote | `GET /v1/india/quotes?symbols=...` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |
| `DataServiceClient.market.candles()` | `OHLCVCandle[]{time,open,high,low,close,volume}` | `GET /v1/india/historical?symbol=&interval=&from=&to=` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |
| `DataServiceClient.market.options()` | `OptionChain{underlying,spot,expiry,rows[{strike,ce,pe}],analytics{pcrOi,pcrVolume,...}}` | `GET /v1/india/option-chain?underlying=&expiry=` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |
| `DataServiceClient.market.instruments()` | `Instrument[]{token,tradingSymbol,exchange,instrumentType,expiry,strike,optionType}` | `GET /v1/instruments?exchange=&instrumentType=` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |
| `DataServiceClient.universe.fno()` | `Instrument[]` for NSE F&O equities | `GET /v1/instruments/fno-universe` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |
| AF `/api/in/option-chain` | Full option chain with IV, Greeks, OI, OI change, bid/ask | `GET /v1/india/option-chain` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |
| AF `/api/in/historical` | OHLCV candles 1m–1M except 3m | `GET /v1/india/historical` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |
| AF `/api/in/quote` | Live quote with LTP, OI | `GET /v1/india/quotes/{symbol}` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |
| AF broker analytics | PCR, OI buildup, gainers/losers | `GET /v1/india/broker-analytics/*` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |
| Market status | Session phase, next change, holiday | `GET /v1/india/market/status` | ✅ | ⛔ BLOCKED | 🟡 | BLOCKED |

## Crypto API Parity

| AlphaForge Consumer | Expected Data | DATA-SERVICE Endpoint | Implemented | Runtime Tested | AlphaForge Uses DATA-SERVICE? | Status |
|---|---|---|---|---|---|---|
| `src/services/binance/rest.ts::fetch24hrTickers()` | 24h ticker: price, change, high, low, vol | ~~`GET /v1/crypto/klines/{symbol}`~~ | 🟡 partial | ⛔ BLOCKED | ❌ NO — calls Binance directly | **P0 BYPASS** |
| `src/services/binance/futures.ts::fetchPremiumIndex()` | markPrice, indexPrice, fundingRate | `GET /v1/crypto/futures/overview` | ✅ | ⛔ BLOCKED | ❌ NO — calls Binance directly | **P0 BYPASS** |
| `src/services/binance/futures.ts::fetchOpenInterest()` | openInterest | `GET /v1/crypto/futures/oi-history/{symbol}` | ✅ | ⛔ BLOCKED | ❌ NO — calls Binance directly | **P0 BYPASS** |
| `src/services/binance/futures.ts::fetchLongShortRatio()` | longShortRatio, longAccount, shortAccount | `GET /v1/crypto/futures/long-short/{symbol}` | ✅ | ⛔ BLOCKED | ❌ NO — calls Binance directly | **P0 BYPASS** |
| `src/services/binance/ws.ts::BinanceWsClient` | Mini ticker stream | `WS /v1/stream/ticks` | 🟡 | ⛔ BLOCKED | ❌ NO — connects Binance WS directly | **P0 BYPASS** |
| `src/services/brokers/delta/rest.ts` (ALL methods) | Delta tickers, candles, OI, product info | ❌ NO DATA-SERVICE ENDPOINT | ❌ NOT IMPL | ❌ | ❌ NO — calls Delta directly | **P0 BYPASS + P0 MISSING** |
| `src/services/deribit/rest.ts::fetchOptionsBookSummary()` | Options book: markIv, OI, bid, ask, underlyingPrice | `GET /v1/crypto/options/{currency}/book` | ✅ | ⛔ BLOCKED | ❌ NO — calls Deribit directly | **P0 BYPASS** |
| `src/services/deribit/rest.ts::fetchIndexPrice()` | Index price for BTC/ETH/SOL | `GET /v1/crypto/options/{currency}/overview` | ✅ | ⛔ BLOCKED | ❌ NO — calls Deribit directly | **P0 BYPASS** |

## Key Field Parity Issues

| Field | Expected | DATA-SERVICE Provides | Issue |
|---|---|---|---|
| `oi` (open interest) | Actual OI in contracts | `oi` with `oiMissing: true` when null | ✅ correct |
| `iv` (implied volatility) | Null when absent, never zero | null with no zero substitution | ✅ correct |
| `delta/gamma/theta/vega/rho` | Null when absent | null | ✅ correct |
| `bid`/`ask` | Null when absent, never zero | null | ✅ correct |
| `tradedValue` | Distinct from OI | distinct field | ✅ correct |
| `3m interval` (India) | HTTP 400 | HTTP 400 at 6 layers | ✅ correct |
| Delta Exchange tickers | markPrice, spot, OI, fundingRate | ❌ NOT PROVIDED | 🔴 missing |
| Delta candles | time,open,high,low,close,volume | ❌ NOT PROVIDED | 🔴 missing |

## Summary

- **India API parity: BLOCKED** — all providers credential-blocked; code parity appears correct from inspection.
- **Binance crypto parity: BLOCKED + BYPASS** — DATA-SERVICE has correct endpoints but AlphaForge does not use them.
- **Delta Exchange parity: MISSING + BYPASS** — DATA-SERVICE has no Delta endpoints; AlphaForge calls Delta directly.
- **Deribit parity: BYPASS** — DATA-SERVICE has Deribit endpoints but AlphaForge calls Deribit directly.
