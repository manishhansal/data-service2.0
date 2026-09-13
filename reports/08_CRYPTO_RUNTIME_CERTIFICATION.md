# REPORT 08 — CRYPTO RUNTIME CERTIFICATION
**Audit date:** 2026-09-13

---

## CRITICAL FINDING: Delta Exchange NOT Implemented

The original requirement specifies **Delta Exchange** as the required crypto provider alongside Binance.

**What exists in DATA-SERVICE 2.0:**
- Binance REST adapter: ✅ `src/providers/adapters/binance_rest.py`
- Binance WebSocket: ✅ `src/providers/streams/binance_stream.py`
- Deribit REST adapter: ✅ `src/providers/deribit_client.py` (crypto OPTIONS, not Delta Exchange)
- Delta Exchange REST: ❌ **NO FILE EXISTS**
- Delta Exchange WebSocket: ❌ **NO FILE EXISTS**
- Settings have Delta URLs/keys configured: 🟡 (dead config — no code uses them)

**This is DS2-RCA-001 — P0 severity.**

Deribit is a different exchange from Delta Exchange:
- Delta Exchange: Indian INR-settled perpetuals, headquartered in India, `api.india.delta.exchange`
- Deribit: European crypto derivatives exchange, `www.deribit.com`

They are NOT interchangeable. Delta Exchange is what AlphaForge actually uses as its primary broker (`ACTIVE_BROKER=delta`).

---

## Binance Certification

| Capability | Adapter Method | Test File | Runtime | DB | Notes |
|---|---|---|---|---|---|
| Spot klines | `get_klines()` | `test_binance_client.py` | ⛔ BLOCKED | ⛔ BLOCKED | Service not running |
| Ticker price | `get_ticker_price()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| 24hr stats | `get_24hr_stats()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| Exchange info | `get_exchange_info()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| Futures mark price | `get_futures_mark_price()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| Futures OI | `get_futures_open_interest()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| OI history | `get_futures_oi_history()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| Long/short ratio | `get_long_short_ratio()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| Funding rate history | `get_funding_rate_history()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| WebSocket kline stream | `binance_stream.py` | `test_binance_stream.py` | ⛔ BLOCKED | ⛔ BLOCKED | |
| 3m interval | Explicitly ALLOWED for crypto | `test_binance_client.py` | ⛔ BLOCKED | ⛔ BLOCKED | |

## Delta Exchange Certification

| Capability | Adapter Method | Test File | Runtime | DB | Notes |
|---|---|---|---|---|---|
| REST client | N/A | N/A | ❌ | ❌ | **NOT IMPLEMENTED** |
| WebSocket | N/A | N/A | ❌ | ❌ | **NOT IMPLEMENTED** |
| Tickers | N/A | N/A | ❌ | ❌ | **NOT IMPLEMENTED** |
| Candles (OHLCV) | N/A | N/A | ❌ | ❌ | **NOT IMPLEMENTED** |
| OI | N/A | N/A | ❌ | ❌ | **NOT IMPLEMENTED** |
| Funding rate | N/A | N/A | ❌ | ❌ | **NOT IMPLEMENTED** |
| Mark price | N/A | N/A | ❌ | ❌ | **NOT IMPLEMENTED** |
| Index price | N/A | N/A | ❌ | ❌ | **NOT IMPLEMENTED** |

## Deribit Certification (Implemented — Wrong Exchange)

| Capability | Adapter Method | Test File | Runtime | DB | Notes |
|---|---|---|---|---|---|
| Instruments list | `get_instruments()` | `test_deribit_client.py` | ⛔ BLOCKED | ⛔ BLOCKED | Correct for Deribit options |
| Order book | `get_order_book()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| OHLCV | `get_ohlcv()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| Index price | `get_index_price()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| Ticker | `get_ticker()` | Same | ⛔ BLOCKED | ⛔ BLOCKED | |
| WebSocket | ❌ NO STREAM | N/A | ❌ | ❌ | Deribit WS not implemented |

---

## Crypto Symbol Runtime Test Matrix

| Symbol | Exchange | Live | Historical | DB Rows | Latest TS | Provider | Quality | Status |
|---|---|---|---|---|---|---|---|---|
| BTCUSDT | Binance | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | N/A | N/A | BLOCKED |
| ETHUSDT | Binance | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | N/A | N/A | BLOCKED |
| SOLUSDT | Binance | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | N/A | N/A | BLOCKED |
| BTCUSD | Delta | ❌ NOT IMPL | ❌ NOT IMPL | 0 | N/A | N/A | N/A | NOT IMPLEMENTED |
| ETHUSD | Delta | ❌ NOT IMPL | ❌ NOT IMPL | 0 | N/A | N/A | N/A | NOT IMPLEMENTED |
| SOLUSD | Delta | ❌ NOT IMPL | ❌ NOT IMPL | 0 | N/A | N/A | N/A | NOT IMPLEMENTED |
| BTC options | Deribit | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | N/A | N/A | BLOCKED |

---

## 3m Interval — Crypto

The original specification says "NO 3m DATA". The implementation documents a **deliberate exception** for Binance crypto:

- `binance_rest.py`: "The 3m interval is NOT banned here. The ban applies only to Indian market data. Binance natively supports 3m for crypto."
- `CANONICAL_CRYPTO_TIMEFRAMES` in `src/core/schemas/provider.py` includes `"3m"`.
- `src/api/crypto.py`: explicitly documents 3m is valid for Binance.

**Assessment**: This is a documented, deliberate policy decision for crypto. The AlphaForge `.env.local` confirms the delta broker also supports `"3m"` in its resolution table. However, the original requirement document said no 3m anywhere. This discrepancy must be resolved by the product owner. See DS2-RCA-014.

---

## Persistence Pipeline (Binance, Code Only)

```
BinanceStream → BinanceNormaliser → BinancePersistence → candle_bar table
```

Files: `src/providers/binance_normaliser.py`, `src/providers/binance_persistence.py`  
Tests: `test_binance_normaliser.py`, `test_binance_persistence.py` — unit tests pass  
Runtime: ⛔ BLOCKED — no DB running
