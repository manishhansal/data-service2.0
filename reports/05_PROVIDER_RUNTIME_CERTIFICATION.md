# REPORT 05 — PROVIDER RUNTIME CERTIFICATION
**Audit date:** 2026-09-13

---

## Provider Implementation Matrix

| Provider | Adapter File | REST | WebSocket | Auth | Rate Limit | Circuit Breaker | 3m Blocked (India) |
|---|---|---|---|---|---|---|---|
| Angel One SmartAPI | `src/providers/adapters/angel_one.py` | ✅ | `src/providers/streams/angel_one_stream.py` ✅ | TOTP+JWT ✅ | 3 req/s ✅ | Via gateway ✅ | ✅ |
| Upstox V2/V3 | `src/providers/adapters/upstox.py` | ✅ | `src/providers/streams/upstox_stream.py` ✅ | OAuth ✅ | 10 req/s ✅ | Via gateway ✅ | ✅ |
| NSE/Scrapling | `src/providers/adapters/scrapling_nse.py` | ✅ | N/A | curl_cffi ✅ | Configured ✅ | Via gateway ✅ | ✅ |
| Yahoo Finance | `src/providers/adapters/yahoo_finance.py` | ✅ | N/A | None ✅ | Configured ✅ | Via gateway ✅ | ✅ |
| Jugaad-data | `src/providers/adapters/jugaad_data.py` | ✅ | N/A | None ✅ | Configured ✅ | Via gateway ✅ | ✅ |
| OpenChart | `src/providers/adapters/openchart.py` | ✅ | N/A | None ✅ | Configured ✅ | Via gateway ✅ | ✅ |
| Binance | `src/providers/adapters/binance_rest.py` | ✅ | `src/providers/streams/binance_stream.py` ✅ | Optional API key ✅ | Configured ✅ | Via gateway ✅ | N/A (crypto) |
| **Delta Exchange** | **MISSING** | ❌ | ❌ | ❌ | ❌ | ❌ | N/A |
| Deribit | `src/providers/deribit_client.py` | ✅ | ❌ (no stream) | Optional ✅ | N/A | N/A | N/A |

---

## Runtime Provider Test Matrix

```
                  LIVE   HIST   DB    API   WS    FAILOVER
Angel One         ⛔      ⛔      ⛔    ⛔     ⛔     ⛔ BLOCKED — credentials empty
Upstox            ⛔      ⛔      ⛔    ⛔     ⛔     ⛔ BLOCKED — credentials empty
NSE/Scrapling     ⛔      ⛔      ⛔    ⛔     N/A   ⛔ BLOCKED — service not running
Yahoo             ⛔      ⛔      ⛔    ⛔     N/A   ⛔ BLOCKED — service not running
Jugaad            N/A    ⛔      ⛔    ⛔     N/A   ⛔ BLOCKED — service not running
OpenChart         N/A    ⛔      ⛔    ⛔     N/A   ⛔ BLOCKED — service not running
Binance           ⛔      ⛔      ⛔    ⛔     ⛔     ⛔ BLOCKED — service not running
Delta Exchange    ❌      ❌      ❌    ❌     ❌     ❌ NOT IMPLEMENTED
Deribit           ⛔      ⛔      ⛔    ⛔     ❌     ⛔ BLOCKED — service not running
```

**Every cell blocked because Docker infrastructure is not running.**

---

## Credential Status

| Provider | Required Credentials | DATA-SERVICE .env.local Status | AlphaForge .env.local Status |
|---|---|---|---|
| Angel One | ANGEL_ONE_API_KEY, ANGEL_ONE_CLIENT_ID, ANGEL_ONE_TOTP_SECRET | NOT_CONFIGURED (empty) | SMARTAPI_* all empty |
| Upstox | UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI | NOT_CONFIGURED (empty) | Not present |
| NSE/Scrapling | None | N/A — curl_cffi fingerprint | N/A |
| Yahoo Finance | None | N/A | N/A |
| Jugaad-data | None | N/A | N/A |
| OpenChart | None | N/A | N/A |
| Binance | Optional API key for private endpoints | NOT_CONFIGURED (public endpoints available) | Not present |
| Delta Exchange | DELTA_API_KEY, DELTA_API_SECRET | NOT_CONFIGURED (empty) | Not present |
| Deribit | Optional | NOT_CONFIGURED (empty) | DERIBIT_CLIENT_ID empty |

---

## Provider Capability Verification (Code Only)

### Angel One SmartAPI
- Historical OHLCV: ✅ `fetch_historical_ohlcv()` with TOTP+JWT auth
- Live quote: ✅ `fetch_live_quote()` FULL mode
- PCR: ✅ `fetch_pcr()`
- OI buildup: ✅ `fetch_oi_buildup()`
- Gainers/losers: ✅ `fetch_gainers_losers()`
- SmartStream WebSocket: ✅ stream adapter
- 3m blocked: ✅ `ProviderUnsupportedError` raised
- Interval map: 1m, 5m, 10m, 15m, 30m, 1h, 1d, 1w ✅ (no 1M mapping — **gap**)

### Upstox V2/V3
- Historical OHLCV: ✅ `fetch_historical_ohlcv()` OAuth flow
- Live quote: ✅ `fetch_live_quote()`
- Batch quotes: ✅ `fetch_market_quote()`
- WebSocket: ✅ stream adapter
- 3m blocked: ✅ `ValueError` raised before any I/O
- Interval map: 1m, 5m, 10m, 15m, 30m, 1h, 1d, 1w, 1M ✅

### NSE/Scrapling
- Live market data: ✅ (curl_cffi TLS fingerprint)
- Option chain: ✅
- Bhavcopy: ✅

### Binance
- Spot klines: ✅ `get_klines()` — 3m explicitly allowed for crypto
- Ticker price: ✅
- 24hr stats: ✅
- Futures mark price: ✅
- Futures OI: ✅
- OI history: ✅
- Long/short ratio: ✅
- Funding rate history: ✅
- WebSocket: ✅

### Delta Exchange
- REST: ❌ NO ADAPTER
- WebSocket: ❌ NO ADAPTER
- Tickers: ❌
- Candles: ❌
- OI: ❌
- Funding: ❌

### Deribit
- REST: ✅ `deribit_client.py`
- get_instruments: ✅
- get_order_book: ✅
- get_ohlcv: ✅
- get_index_price: ✅
- get_ticker: ✅
- WebSocket: ❌ NO STREAM ADAPTER

---

## RCA References

- **RCA-001**: Delta Exchange not implemented
- **RCA-002**: AlphaForge bypasses Binance
- **RCA-003**: AlphaForge bypasses Delta
- **RCA-004**: AlphaForge bypasses Deribit
