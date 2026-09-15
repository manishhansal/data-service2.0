# HISTORICAL OI MIGRATION REPORT
**Generated:** 2026-09-15  
**Investigation period:** All candle_bar rows where oi IS NOT NULL  
**Verdict:** NO REAL OI DATA EXISTS IN THE HISTORICAL CANDLE DATASET

---

## 1. FINDING SUMMARY

| Metric | Value |
|---|---|
| candle_bar rows with oi IS NOT NULL | 13,480 |
| candle_bar rows with oi > 0 | **0** |
| candle_bar rows with oi = 0 | **13,480** (100%) |
| candle_bar rows with oi < 0 | 0 |
| Real OI data lost | **ZERO** |

---

## 2. FULL OI INVESTIGATION

### 2.1 Query Evidence

```sql
SELECT COUNT(*) FROM candle_bar WHERE oi IS NOT NULL AND exchange='NSE' AND oi=0;
-- Result: 13,480

SELECT COUNT(*) FROM candle_bar WHERE oi IS NOT NULL AND exchange='NSE' AND oi>0;
-- Result: 0

SELECT COUNT(*) FROM candle_bar WHERE oi IS NOT NULL AND exchange='NSE' AND oi<0;
-- Result: 0
```

### 2.2 Source Analysis

All 13,480 rows with `oi IS NOT NULL` come from:

| Provider | Source Type | Intervals | Instruments | oi values |
|---|---|---|---|---|
| upstox | BROKER_AUTHENTICATED | 1d, 1w, 1M | 41 NSE equities + NIFTY + BANKNIFTY | ALL = 0 |

### 2.3 Why Upstox Returns oi=0 for Equity Candles

Upstox V2 historical candle endpoint returns a 7-element array for each candle:
```
[timestamp, open, high, low, close, volume, oi]
```

For **NSE equity cash market instruments** (`NSE_EQ|ISIN`), the `oi` field in the Upstox response is populated with `0` (integer zero), not `NULL`. This is expected behavior from Upstox:

- NSE **equities** do not have open interest — they trade on the cash market
- Only NSE **F&O contracts** (futures and options) have open interest
- Upstox includes the `oi` position in the array for consistency but fills it with `0` for cash instruments
- A value of `0` here means "no OI / not applicable", not "zero open interest contracts"

### 2.4 NSE Index OI (NIFTY, BANKNIFTY)

| Instrument | Intervals | oi values |
|---|---|---|
| NSE:NIFTY | 1d, 1m, 30m | ALL = 0 |
| NSE:BANKNIFTY | 1d | ALL = 0 |

NSE indices (NIFTY, BANKNIFTY) themselves are not traded — they are reference benchmarks. Their `oi=0` from Upstox is similarly a placeholder for the cash index, not meaningful OI data. The actual OI for NIFTY/BANKNIFTY derivatives lives in NFO futures and options contracts.

---

## 3. OI SEMANTIC CLASSIFICATION

| Classification | Row Count | Disposition |
|---|---|---|
| EQUITY_OI | 0 | N/A — no equity OI data |
| INDEX_OI | 0 | N/A — no index OI data |
| FUTURES_OI | 0 | N/A — no futures data in DB |
| OPTIONS_OI | 0 | N/A — no options data in DB |
| PLACEHOLDER_ZERO (upstox equity/index) | **13,480** | Correctly discarded — oi=0 is semantically meaningless for equity cash |
| UNKNOWN | 0 | None |

---

## 4. MIGRATION DISPOSITION

### What happened during migration

The 13,480 rows with `oi=0` were included in the `candle_bar → equity_candle` migration. However:

1. `equity_candle` has **no OI column** (by design — equities don't have OI)
2. The `oi=0` values were therefore not stored in `equity_candle`
3. This is the **correct behavior**

### Was any real OI data lost?

**NO.** The 13,480 rows with `oi IS NOT NULL` all have `oi=0`. A value of zero from Upstox for equity OHLCV candles is a placeholder, not actual open interest data. Discarding `oi=0` from equity candles introduces zero information loss.

### Where real OI will live

Real OI data — from F&O contracts (futures and options) — will be stored in:

| Table | OI Column | Applies to |
|---|---|---|
| `futures_candle` | `open_interest` (BIGINT, nullable) | NFO futures contracts |
| `options_candle` | `open_interest` (BIGINT, nullable) | NFO options contracts |

Both columns enforce `open_interest IS NULL OR open_interest >= 0` at the DB level.

---

## 5. CORRECTION TO PREVIOUS REPORT (and Final Disposition)

The previous migration report statement was:

> "OI belongs in futures_candle/options_candle"

This statement was **correct in principle** but was applied without inspecting the actual OI values. The complete finding (confirmed by direct DB query) is:

- The 13,480 rows cited as "OI present" ALL have `oi=0` (exactly zero)
- `oi=0` from Upstox for equity cash candles is a **provider artifact** (API response structure includes the field but fills it with 0 for equity instruments that don't have OI)
- No meaningful OI data existed in `candle_bar`
- The migration to `equity_candle` (which has no OI column) correctly dropped these zero values
- **ZERO real OI observations were lost**

**For reference — true F&O OI data:**
- `futures_candle.open_interest` — populated from Angel One NFO candles (confirmed: 20 rows with OI data from Sep 2026)
- `options_candle.open_interest` — ready for F&O backfill
- Upstox option chain via analytics key returns live OI: NIFTY ATM CE OI=5,488,665, PE OI=2,079,535

**OI distinction:**
```
equity OHLCV          → equity_candle (no OI column — correct)
equity OI proxy (=0)  → discarded — semantically meaningless
futures OI            → futures_candle.open_interest (live)
options OI            → options_candle.open_interest (live via Upstox)
```

---

## 6. OPEN_INTEREST SNAPSHOT TABLE — DECISION

**Decision: Not required at this time.**

An `open_interest_snapshot` table would be appropriate if there were actual non-zero OI observations to store. Since all 13,480 historical OI values are zero, creating a dedicated table would add infrastructure without any data to populate it.

When the F&O backfill runs, true OI data will enter `futures_candle.open_interest` and `options_candle.open_interest`, which are the correct canonical locations.

If intraday OI tick data (OI updates at sub-daily frequency) is needed in the future, an `open_interest_snapshot` table should be created at that point.

---

## 7. FINAL STATUS

| Check | Status |
|---|---|
| Source rows (oi IS NOT NULL) | 13,480 |
| Rows preserved with actual OI value | **0** (all were zero) |
| Rows relocated to futures_candle | 0 (no futures data) |
| Rows relocated to options_candle | 0 (no options data) |
| Rows rejected (invalid OI) | 0 |
| Rows quarantined | 0 |
| Unexplained loss | **ZERO** |
| Real OI data loss | **ZERO** |

**OI DISPOSITION: FULLY EXPLAINED — ZERO REAL DATA LOSS ✅**
