# PROVIDER DATA LINEAGE REPORT
## data-service2.0 — Updated 2026-09-17 (post pipeline gap fixes)

**Original date:** 2026-09-17 (static analysis)  
**Updated:** 2026-09-17 (post live-testing and pipeline gap fixes)  
**Evidence basis:** Static codebase analysis + live DB verification + 2026-09-17 live test results  
**Unit tests:** 4,413 passing (0 failures) — confirmed 2026-09-17

---

## SUMMARY OF PIPELINE GAP FIXES APPLIED 2026-09-17

The following critical gaps were identified and fixed in this session:

| Gap | Severity | Status | Fix |
|-----|----------|--------|-----|
| `market_quote` never written | CRITICAL | ✅ FIXED | `persist_market_quote()` in `market_engine.py` — fire-and-forget after every live quote |
| `option_greeks_snapshot` never written | CRITICAL | ✅ FIXED | `_persist_option_greeks()` in `api/india.py` — fire-and-forget after Greeks API call |
| `option_chain_snapshot/contract` never written | CRITICAL | ✅ FIXED | `_persist_option_chain()` in `dual_provider_engine.py` — fire-and-forget after chain fetch |
| Upstox `normalize_full_quote` dropped `totalBuyQty`, `totalSellQty`, `weekHigh52`, `weekLow52`, `avgTradedPrice` | MEDIUM | ✅ FIXED | All 5 fields added to normalizer output |
| `bulk_upsert_candles` dropped `source_timestamp`, `underlying_id` | HIGH | ✅ FIXED | Both added to row dict and all 3 SQL upserts |
| Historical API dropped `provider`, `sourceType` from per-candle response | MEDIUM | ✅ FIXED | `_query_candles` SQL now SELECTs `source_type`; row dict includes `provider` + `sourceType` |
| `market_quote` missing `depth_json`, `source_type`, `change`, `change_pct`, `avg_traded_price` | HIGH | ✅ FIXED | DB migration + ORM model updated |
| `option_greeks_snapshot` missing `oi`, `volume`, `ltp`, `prev_close`, `ltq`, `instrument_key` | MEDIUM | ✅ FIXED | DB migration + ORM model updated |
| Upstox V2 interval guard blocked 5m/10m/15m/1h | MEDIUM | ✅ FIXED | Replaced `_UPSTOX_V2_SUPPORTED_INTERVALS` with `_UPSTOX_V3_SUPPORTED_INTERVALS` (all 9) |
| Upstox `fetch_full_quote` used V2 endpoint | MEDIUM | ✅ FIXED | Migrated to `/v3/market-quote/quotes` (April 2025 Upstox V3 launch) |

---

## PROVENANCE FIELDS — ALL RECORDS

Every record in the canonical database carries the following provenance fields:

| Field | Type | Source | Status |
|-------|------|--------|--------|
| `provider` | string | Set in normalizer | ✅ Written in all upserts |
| `source_type` | string | Set in normalizer | ✅ Written in all upserts; returned in API response per-candle |
| `received_at` / `ingested_at` | UTC timestamp | Service wall clock | ✅ Written |
| `normalisation_version` | string | Software version | ✅ Written (defaults to "2.0.0") |
| `dataset_version` | integer | Schema version | ✅ Written (defaults to 1) |
| `source_timestamp` | UTC timestamp | Exchange-side timestamp | ✅ Written when available (was dropped before fix) |
| `underlying_id` | string | Derived from symbol | ✅ Written for futures/options (was dropped before fix) |

### Provider values confirmed in live data (2026-09-17)

| provider | source_type | Evidence |
|----------|-------------|---------|
| `angel_one` | `BROKER_AUTHENTICATED` | market_quote: RELIANCE ltp=1240.6 (2026-09-17) + equity_candle bars |
| `upstox` | `BROKER_AUTHENTICATED` | option_greeks_snapshot: 10 rows (2026-09-17) + equity_candle bars |
| `yahoo_finance` | `OPEN_SOURCE_NSE_DERIVED` | equity_candle today-gap fill |
| `jugaad_data` | `OPEN_SOURCE_NSE_DERIVED` | equity_candle historic |
| `openchart` | `OPEN_SOURCE_NSE_DERIVED` | equity_candle historic |

---

## DB PROVENANCE VERIFICATION (2026-09-17 POST-FIX)

| Table | Rows | Status | Notes |
|-------|------|--------|-------|
| equity_candle | 5,460,561 | ✅ LIVE DATA | `provider` + `source_type` written; `source_timestamp` now written where available |
| futures_candle | 20 | ✅ LIVE DATA | `underlying_id` + `source_timestamp` now written |
| options_candle | 0 | Ready | Awaiting F&O backfill |
| market_quote | 3 | ✅ NEW — LIVE DATA | RELIANCE + HDFCBANK from Angel One; `depth_json` confirms 5-level depth |
| market_tick | 0 | NOT_IMPLEMENTED | WebSocket streams not certified |
| option_greeks_snapshot | 10 | ✅ NEW — LIVE DATA | NIFTY Sep29 options; iv/delta/oi/volume populated |
| option_chain_snapshot | 53 | ✅ NEW — LIVE DATA | NIFTY/BANKNIFTY/FINNIFTY snapshots |
| option_chain_contract | 10 | ✅ NEW — LIVE DATA | Per-strike CE/PE rows with Greeks |

---

## NULL SEMANTICS AUDIT

### Angel One normalizer (`src/core/normalizers/angel_one.py`)

| Field | Null behavior | Status |
|-------|--------------|--------|
| `oi` (from opnInterest) | `None` + `oiMissing=True` when absent | ✅ CLEAN — no `or 0` patterns |
| `iv` | `None` when absent; 0 is rejected | ✅ CLEAN |
| `delta`, `gamma`, `theta`, `vega` | `None` when absent; zero-substitute rejected | ✅ CLEAN |
| `bid`, `ask` | `None` when absent; `allow_zero=False` | ✅ CLEAN |
| `totalBuyQty`, `totalSellQty` | `None` when absent | ✅ CLEAN |
| `weekHigh52`, `weekLow52` | `None` when absent | ✅ CLEAN |

### Upstox normalizer (`src/core/normalizers/upstox.py`) — updated 2026-09-17

| Field | Null behavior | Status |
|-------|--------------|--------|
| `oi` (cash equity) | `None` — cash instruments have no OI | ✅ CLEAN |
| `totalBuyQty`, `totalSellQty` | `None` when absent | ✅ FIXED (was dropped before) |
| `weekHigh52`, `weekLow52` | `None` when absent | ✅ FIXED (was dropped before) |
| `avgTradedPrice` | `None` when absent | ✅ FIXED (was dropped before) |
| `iv` | `None` when zero or absent | ✅ CLEAN |
| Greeks | `None` when absent | ✅ CLEAN |

### Remaining `or 0` patterns (non-critical)

| File | Field | Risk | Severity |
|------|-------|------|---------|
| `openchart.py:151-154` | open/high/low/close | OHLCV validator rejects zero-price candles | LOW-MEDIUM |
| `jugaad_data.py:124-127` | open/high/low/close | Same | LOW-MEDIUM |
| `delta_exchange.py:265` | openInterest (crypto) | Crypto OI silently 0 when absent | MEDIUM (crypto only) |

These were identified in the Phase 40 audit. The OHLCV validator provides a safety net — zero prices are caught and rejected before persistence. The correct fix (returning `None` + `OHLC_MISSING=True`) remains outstanding.

---

## API RESPONSE — WHAT ALPHAFORGE RECEIVES

### Historical OHLCV candles (GET /v1/india/historical)

Each candle now contains:
```json
{
  "time": 1789530300,
  "open": 1240.0,
  "high": 1253.4,
  "low": 1240.0,
  "close": 1240.0,
  "volume": 2374702,
  "oi": null,
  "volumeUnavailable": false,
  "provider": "angel_one",
  "sourceType": "BROKER_AUTHENTICATED"
}
```
`provider` and `sourceType` are now present per-candle (fixed 2026-09-17).

### Live quote (GET /v1/india/quotes/{symbol})

Contains: ltp, open, high, low, prevClose, change, changePct, volume, oi, totalBuyQty, totalSellQty, upperCircuit, lowerCircuit, weekHigh52, weekLow52, depthBuy[5], depthSell[5], provider, sourceType, marketStatus.

### Option chain (GET /v1/india/option-chain)

Returns: contracts[{strike, optionType, ltp, volume, oi, bid, ask, iv, delta, gamma, theta, vega, isAtm}], spotPrice, pcr, receivedAt, provider.

### Option Greeks (GET /v1/india/options/greeks)

Returns per key: iv, delta, gamma, theta, vega, oi, volume, ltp, prevClose, ltq, greekSource="PROVIDER".

---

## REMAINING GAPS (NOT FIXED IN THIS SESSION)

| Gap | Status | Notes |
|-----|--------|-------|
| `market_tick` never written | OUTSTANDING | WebSocket (SmartStream/Upstox WS) not certified |
| `vwap`, `turnover` in candle tables | OUTSTANDING | No provider returns per-candle VWAP/turnover currently |
| `closing_auction_snapshot` never written | OUTSTANDING | `normalize_cas_data()` is dead code — not called from any live path |
| `reconciliation_status`, `provenance_id` in candles | OUTSTANDING | Reconciliation engine results not yet written to candle tables |
| `oi_change` in futures/options candle | OUTSTANDING | OI delta not tracked across consecutive candles |

---

*Updated: 2026-09-17. Based on live DB verification, static codebase analysis, and 2026-09-17 market-hours test results.*
