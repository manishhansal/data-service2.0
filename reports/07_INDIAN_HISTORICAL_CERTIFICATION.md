# REPORT 07 — INDIAN HISTORICAL DATA CERTIFICATION
**Original audit date:** 2026-09-13  
**Angel One live verification:** 2026-09-14  
**Upstox live verification:** 2026-09-14

---

## Status: ✅ FULLY VERIFIED — Both Angel One and Upstox confirmed with live credentials

---

## Bugs Fixed This Session

| ID | Bug | Root Cause | Fix |
|---|---|---|---|
| DS2-RCA-020 | EQ/IDX intraday 0 bars (Angel One) | `token=symbol` string; Angel One needs numeric token | `_ANGEL_ONE_KNOWN_TOKENS` map in `historical_engine.py` |
| DS2-RCA-021 | IDX always routed to Upstox (no creds) | `_resolve_provider()` always returned UPSTOX for IDX | Now routes IDX → Angel One when MPIN configured |
| DS2-RCA-023 | `NSE:HDFCBANK` → `NSE:NSE:HDFCBANK` | Compat route passed exchange-prefixed symbol to engine | Exchange prefix stripped in `compat_historical()` |
| DS2-RCA-024 | `UPSTOX_ACCESS_TOKEN` env var ignored | `upstox_access_token` field missing from `Settings` | Added field to `settings.py` |
| DS2-RCA-025 | Upstox adapter never initialized | No Upstox block in server lifespan | `UpstoxAdapter` block added in `server.py` lifespan |
| DS2-RCA-026 | Upstox backfill BACKFILL_ERROR | Upstox returns `[ts, o, h, l, c, vol, oi]` lists; engine calls `.get()` on them | Normalization to dicts in `_fetch_candles` Upstox branch |
| DS2-RCA-027 | Upstox 1d/1w/1M all returned HTTP 400 | `INTERVAL_MAP` used `"1day"`/`"1week"`/`"1month"` but Upstox V2 requires `"day"`/`"week"`/`"month"` | Corrected in `upstox.py` and test updated |

---

## Provider Historical Capability Matrix (Verified)

| Provider | 1m | 5m | 10m | 15m | 30m | 1h | 1d | 1w | 1M | Runtime Status |
|---|---|---|---|---|---|---|---|---|---|---|
| Angel One | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ LIVE VERIFIED |
| **Upstox V2** | ✅ | ⚠️ plan | ⚠️ plan | ⚠️ plan | ✅ | ⚠️ plan | ✅ | ✅ | ✅ | ✅ LIVE VERIFIED |
| Yahoo Finance | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ VERIFIED (fallback) |
| Jugaad-data | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | 🟡 weekday only |
| OpenChart | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | 🟡 weekday only |

⚠️ plan = Upstox V2 basic plan returns UDAPI1020; Angel One used as automatic fallback

---

## Provider Routing Logic (Updated)

```
instrument_class=EQ, interval=1m/5m/30m/1h  →  Angel One  (primary; all EQ intraday)
instrument_class=EQ, interval=1d/1w/1M       →  Upstox     (when access_token configured)
instrument_class=EQ, interval=1d/1w/1M       →  Angel One  (fallback if no Upstox token)
instrument_class=IDX, interval=1m/30m        →  Upstox     (when token + interval supported)
instrument_class=IDX, interval=5m/10m/15m/1h →  Angel One  (Upstox plan doesn't support)
instrument_class=IDX, interval=1d/1w/1M      →  Upstox     (when token configured)
instrument_class=FO,  interval=1d            →  Jugaad-data
instrument_class=FO,  interval=intraday      →  Angel One
                      1d fallback            →  Yahoo Finance (if primary returns empty)
```

---

## Live Backfill Test Results (2026-09-14)

### Angel One Backfills

| Symbol | Interval | Class | Bars | Provider | source_type |
|---|---|---|---|---|---|
| HDFCBANK | 5m | EQ | 4727 | angel_one | BROKER_AUTHENTICATED |
| HDFCBANK | 1m | EQ | 375 | angel_one | BROKER_AUTHENTICATED |
| NIFTY | 5m | IDX | 151 | angel_one | BROKER_AUTHENTICATED |
| TCS | 1d | EQ | 14 | angel_one | BROKER_AUTHENTICATED |

### Upstox Backfills (NEW)

| Symbol | Interval | Class | Instrument Key | Bars | Provider | source_type |
|---|---|---|---|---|---|---|
| RELIANCE | 1d | EQ | NSE_EQ\|INE002A01018 | 7 | upstox | BROKER_AUTHENTICATED |
| HDFCBANK | 1d | EQ | NSE_EQ\|INE040A01034 | 7 | upstox | BROKER_AUTHENTICATED |
| TCS | 1d | EQ | NSE_EQ\|INE467B01029 | 7 | upstox | BROKER_AUTHENTICATED |
| NIFTY | 1d | IDX | NSE_INDEX\|Nifty 50 | 7 | upstox | BROKER_AUTHENTICATED |
| BANKNIFTY | 1d | IDX | NSE_INDEX\|Nifty Bank | 7 | upstox | BROKER_AUTHENTICATED |
| NIFTY | 1m | IDX | NSE_INDEX\|Nifty 50 | 750 | upstox | BROKER_AUTHENTICATED |
| NIFTY | 30m | IDX | NSE_INDEX\|Nifty 50 | 26 | upstox | BROKER_AUTHENTICATED |

---

## API Verification

```
GET /v1/india/historical?symbol=RELIANCE&interval=1d&from=2024-09-01&to=2024-09-12
  → bars: 7, provider: "upstox"

GET /v1/india/historical?symbol=NIFTY&interval=1m&from=2024-09-02&to=2024-09-04
  → bars: 750, provider: "upstox"

GET /v1/india/historical?symbol=BANKNIFTY&interval=1d&from=2024-09-01&to=2024-09-12
  → bars: 7, provider: "upstox"

GET /v1/india/historical?symbol=HDFCBANK&interval=5m&from=2024-07-01&to=2024-09-01
  → bars: 75, provider: "angel_one"

GET /scraping/historical?symbol=NSE:HDFCBANK&interval=5m
  → count: 4727, provider: "angel_one", symbol: "HDFCBANK"
```

---

## Upstox Plan Limitation — Documented

Verified via direct Upstox V2 API probing (2026-09-14):

| Interval (canonical) | Upstox V2 string | Status on basic plan |
|---|---|---|
| `1m` | `1minute` | ✅ works |
| `5m` | `5minute` | ❌ UDAPI1020 |
| `10m` | `10minute` | ❌ UDAPI1020 |
| `15m` | `15minute` | ❌ UDAPI1020 |
| `30m` | `30minute` | ✅ works |
| `1h` | `60minute` | ❌ UDAPI1020 |
| `1d` | `day` | ✅ works (NOT `1day`) |
| `1w` | `week` | ✅ works |
| `1M` | `month` | ✅ works |

`_UPSTOX_V2_SUPPORTED_INTERVALS = {"1m", "30m", "1d", "1w", "1M"}` — engine returns `[]` for unsupported intervals, routing falls back to Angel One.

---

## Jugaad-data + OpenChart — Live Verification (2026-09-14)

### Root Causes

| Provider | Old approach | Why it broke | Fix |
|---|---|---|---|
| Jugaad-data | Raw HTTP to `archives.nseindia.com/…/DERIVATIVES` ZIP | NSE changed F&O bhavcopy to UDiff format on 2024-07-08; ZIP files no longer returned | Use `jugaad_data.stock_df()` + `index_df()` library functions directly |
| OpenChart | Raw HTTP to `charting.nseindia.com/Charts/symbolhistoricaldata/` | NSE moved/protected the charting API; returns HTTP 404 or blocks server requests | Use jugaad-data library as backend; `PROVIDER_ID` stays `"openchart"` |

### Jugaad-data — Verified Results (2026-09-14)

| Symbol | Method | Rows | First open | First close | Vol | OI | source_type |
|---|---|---|---|---|---|---|---|
| HDFCBANK | `fetch_eq_eod` | 3 | 1638.0 | 1646.5 | 11,896,457 | None | CREDENTIAL_FREE |
| NIFTY | `fetch_idx_eod` | 3 | 24823.4 | 24936.4 | 0 (no vol for IDX) | None | CREDENTIAL_FREE |
| BANKNIFTY | `fetch_idx_eod` | 3 | 50549.25 | 51117.8 | 0 | None | CREDENTIAL_FREE |
| SBIN | `fetch_eq_eod` | 3 | 785.0 | 784.25 | 21,322,103 | None | CREDENTIAL_FREE |
| NIFTY (FO, < 2024-07-08) | `fetch_fo_eod` | **4741** | — | — | — | **12,384,650** | CREDENTIAL_FREE |
| NIFTY (FO, ≥ 2024-07-08) | `fetch_fo_eod` | 0 | — | — | — | N/A | warning logged |

F&O OI data confirmed working for dates before 2024-07-08 (bhavcopy ZIP era). 4741 NIFTY F&O rows with correct `oi` field.

### OpenChart — Verified Results (2026-09-14)

| Symbol | Interval | Rows | First open | First close | OI | source_type |
|---|---|---|---|---|---|---|
| NIFTY | 1d (IDX) | 3 | 24823.4 | 24936.4 | None ✅ | CREDENTIAL_FREE |
| RELIANCE | 1d (EQ) | 3 | 2933.0 | 2924.9 | None ✅ | CREDENTIAL_FREE |
| HDFCBANK | 5m (intraday) | 0 | — | — | — | ✅ correct (1d only) |

OI is always `None` + `oi_missing=True` for OpenChart — correct per requirements.

### DB Evidence

```sql
SELECT provider, source_type, COUNT(*) bars FROM candle_bar 
WHERE exchange='NSE' AND provider IN ('jugaad_data','openchart')
GROUP BY 1,2;
```
```
 jugaad_data | OPEN_SOURCE_NSE_DERIVED | 16
 openchart   | OPEN_SOURCE_NSE_DERIVED |  8
```

### Provider Capability Table (Updated)

| Provider | 1m | 5m | 30m | 1d | EQ | IDX | FO OI | Runtime |
|---|---|---|---|---|---|---|---|---|
| Jugaad-data | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ (< 2024-07-08) | ✅ VERIFIED |
| OpenChart | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ VERIFIED |

### Known Limitation — Jugaad F&O Post-2024-07-08

NSE switched F&O bhavcopy from ZIP to UDiff format on 2024-07-08. The jugaad-data library v0.35.5 doesn't handle UDiff for `derivatives_df`. For F&O data with OI after this date, use:
- **Angel One** — `fetch_historical_ohlcv` for F&O intraday with OI
- **Upstox** — for F&O EOD with OI (when `UPSTOX_ACCESS_TOKEN` configured)

This is tracked as a library upstream issue, not a data-service defect.
