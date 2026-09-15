# F&O DATABASE CONSTRAINT AUDIT
**Generated:** 2026-09-15  
**Tables audited:** futures_candle, options_candle, equity_candle, exchange_calendar  
**Verdict:** ✅ ALL REQUIRED CONSTRAINTS ACTIVE

---

## 1. futures_candle CONSTRAINTS

| Constraint Name | Expression | Status |
|---|---|---|
| `fc_no_3m_interval` | `interval_str <> '3m'` | ✅ Active |
| `fc_high_gte_open` | `high >= open` | ✅ Active |
| `fc_high_gte_close` | `high >= close` | ✅ Active |
| `fc_low_lte_open` | `low <= open` | ✅ Active |
| `fc_low_lte_close` | `low <= close` | ✅ Active |
| `fc_high_gte_low` | `high >= low` | ✅ Active |
| `fc_volume_non_negative` | `volume >= 0` | ✅ Active |
| `fc_oi_non_negative` | `open_interest IS NULL OR open_interest >= 0` | ✅ Active |
| `fc_contract_type_fut` | `contract_type = 'FUT'` | ✅ Active |
| `fc_data_origin_valid` | `data_origin IN ('PROVIDER','DERIVED')` | ✅ Active |
| `fc_quality_status_valid` | `quality_status IN (7 valid values)` | ✅ Active |

**Functional test:** INSERT with `interval_str='3m'` → REJECTED ✅

---

## 2. options_candle CONSTRAINTS

| Constraint Name | Expression | Status |
|---|---|---|
| `oc_no_3m_interval` | `interval_str <> '3m'` | ✅ Active |
| `oc_option_type_valid` | `option_type IN ('CE','PE')` | ✅ Active |
| `oc_strike_positive` | `strike > 0` | ✅ Active |
| `oc_high_gte_open` | `high >= open` | ✅ Active |
| `oc_high_gte_close` | `high >= close` | ✅ Active |
| `oc_low_lte_open` | `low <= open` | ✅ Active |
| `oc_low_lte_close` | `low <= close` | ✅ Active |
| `oc_high_gte_low` | `high >= low` | ✅ Active |
| `oc_volume_non_negative` | `volume >= 0` | ✅ Active |
| `oc_oi_non_negative` | `open_interest IS NULL OR open_interest >= 0` | ✅ Active |
| `oc_data_origin_valid` | `data_origin IN ('PROVIDER','DERIVED')` | ✅ Active |
| `oc_quality_status_valid` | `quality_status IN (7 valid values)` | ✅ Active |

**Functional test:** INSERT with `option_type='XX'` → REJECTED ✅
**Functional test:** INSERT with `strike=0` → REJECTED ✅
**Functional test:** INSERT with negative OI → REJECTED ✅

---

## 3. equity_candle CONSTRAINTS

| Constraint Name | Expression | Status |
|---|---|---|
| `ec_no_3m_interval` | `interval_str <> '3m'` | ✅ Active |
| `ec_high_gte_open` | `high >= open` | ✅ Active |
| `ec_high_gte_close` | `high >= close` | ✅ Active |
| `ec_low_lte_open` | `low <= open` | ✅ Active |
| `ec_low_lte_close` | `low <= close` | ✅ Active |
| `ec_high_gte_low` | `high >= low` | ✅ Active |
| `ec_volume_non_negative` | `volume >= 0` | ✅ Active |
| `ec_data_origin_valid` | `data_origin IN ('PROVIDER','DERIVED')` | ✅ Active |
| `ec_quality_status_valid` | `quality_status IN (7 valid values)` | ✅ Active |

---

## 4. candle_bar ARCHIVE CONSTRAINT

| Constraint | Status | Effect |
|---|---|---|
| `no_3m_interval` | ✅ Still active on archive | Prevents any 3m data if archive is accidentally written |

---

## 5. UNIQUE CONSTRAINTS (BUSINESS KEYS)

| Table | Unique Constraint | Columns |
|---|---|---|
| equity_candle | `equity_candle_uq` | `(instrument_id, exchange, interval_str, time)` |
| futures_candle | `futures_candle_uq` | `(instrument_id, exchange, interval_str, time)` |
| options_candle | `options_candle_uq` | `(instrument_id, exchange, interval_str, time)` |
| exchange_calendar | `ec_exchange_segment_date_uq` | `(exchange, segment, calendar_date)` |

All use `ON CONFLICT ... DO NOTHING` or `DO UPDATE` in upsert operations — enforcing idempotency.

---

## 6. TIMESCALEDB HYPERTABLES

| Table | Chunks | Partition Key | Status |
|---|---|---|---|
| equity_candle | 71 | time | ✅ Active |
| futures_candle | 0 (empty) | time | ✅ Active |
| options_candle | 0 (empty) | time | ✅ Active |
| market_tick | 0 (empty) | timestamp | ✅ Active |
| market_quote | 0 (empty) | timestamp | ✅ Active |
| option_greeks_snapshot | 0 (empty) | timestamp | ✅ Active |

---

## 7. COMPLETE CONSTRAINT SUMMARY

| Requirement | Implementation | Status |
|---|---|---|
| 3m permanently banned (equity) | `ec_no_3m_interval` | ✅ |
| 3m permanently banned (futures) | `fc_no_3m_interval` | ✅ |
| 3m permanently banned (options) | `oc_no_3m_interval` | ✅ |
| 3m permanently banned (archive) | `no_3m_interval` on candle_bar | ✅ |
| CE/PE only for options | `oc_option_type_valid`, `occ_option_type_valid` | ✅ |
| Strike > 0 | `oc_strike_positive`, `occ_strike_positive` | ✅ |
| contract_type = FUT | `fc_contract_type_fut` | ✅ |
| Negative OI impossible (futures) | `fc_oi_non_negative` | ✅ |
| Negative OI impossible (options) | `oc_oi_non_negative` | ✅ |
| Negative volume impossible | All 3 candle tables | ✅ |
| OHLC integrity | 5 CHECKs per candle table | ✅ |
| Quality status controlled | All candle + live tables | ✅ |
| Data origin controlled | All candle tables | ✅ |

**ALL 32 CHECK CONSTRAINTS ACTIVE AND VERIFIED ✅**
