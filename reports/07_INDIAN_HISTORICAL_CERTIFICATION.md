# REPORT 07 — INDIAN HISTORICAL DATA CERTIFICATION
**Audit date:** 2026-09-13

---

## Status: ⛔ BLOCKED — Infrastructure Not Running

---

## Provider Historical Capability Matrix

| Provider | Adapter | 1m | 5m | 10m | 15m | 30m | 1h | 1d | 1w | 1M | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Angel One SmartAPI | `angel_one.py` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | No 1M mapping in INTERVAL_MAP — **gap** |
| Upstox V2/V3 | `upstox.py` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Full mapping |
| NSE/Scrapling | `scrapling_nse.py` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | Bhavcopy = daily only |
| Yahoo Finance | `yahoo_finance.py` | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | 10m not in Yahoo intervals |
| Jugaad-data | `jugaad_data.py` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | Daily bhavcopy only |
| OpenChart | `openchart.py` | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | No 10m or 1w documented |

**Angel One 1M gap**: `INTERVAL_MAP` in `angel_one.py` has no `"1M"` entry. A `1M` request will raise `ProviderUnsupportedError`. Upstox handles 1M correctly. This is not a blocking P0 but should be fixed — see DS2-RCA-015.

---

## Historical Data Flow (Code Verified)

```
Request → HistoricalEngine → CapabilityMatrix → Provider selection
→ fetch_historical_ohlcv() → raw response
→ Normaliser → 14-step ValidationPipeline
→ OHLCV invariant check
→ Deduplication (SHA-256 hash)
→ Persistence → candle_bar table (PostgreSQL + TimescaleDB)
→ API response ← Cache ← DB query
```

All steps implemented in code. No runtime execution verified.

---

## Backfill + Gap Recovery (Code Only)

| Feature | File | Status |
|---|---|---|
| Resumable backfill checkpoint | `src/engines/historical_engine.py` | 🟡 code exists |
| Redis checkpoint (no TTL) | Same | 🟡 |
| 4-state gap machine (PENDING/RECOVERING/RECOVERED/EXHAUSTED) | `src/core/validators/gap_detection.py` | 🟡 |
| Duplicate prevention (SHA-256) | `src/core/validators/dedup.py` | 🟡 |
| Chunk sizes configured | `.env.local` | 🟡 |

---

## Symbol/Timeframe Runtime Test Matrix

| Symbol | Timeframe | Rows Expected | Rows in DB | Provider | Status |
|---|---|---|---|---|---|
| RELIANCE | 1m | 375/day | 0 | N/A | ⛔ BLOCKED |
| NIFTY | 5m | 75/day | 0 | N/A | ⛔ BLOCKED |
| BANKNIFTY | 15m | 25/day | 0 | N/A | ⛔ BLOCKED |
| RELIANCE | 1d | 1/day | 0 | N/A | ⛔ BLOCKED |
| NIFTY | 1w | 1/week | 0 | N/A | ⛔ BLOCKED |

---

## Known Gap: Angel One 1M Interval

`src/providers/adapters/angel_one.py` `INTERVAL_MAP` does not include `"1M"`. Request for monthly bars will raise `ProviderUnsupportedError`. Upstox correctly maps `"1M" → "1month"`. This is a **P2** gap — monthly bars can be sourced from Upstox, but Angel One cannot serve them. Fix: add `"1M": "ONE_MONTH"` if SmartAPI supports it, otherwise document that Angel One does not support 1M and Upstox is the fallback.
