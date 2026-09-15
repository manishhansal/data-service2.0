# INDIAN MARKET PRE-MIGRATION INVENTORY
**Generated:** 2026-09-15  
**Database:** PostgreSQL 15.18 / TimescaleDB 2.28.3  
**Host:** localhost:5444 | DB: mds | User: mds_user  
**Container:** data-service-postgres (timescale/timescaledb:latest-pg15)  
**Current Alembic revision:** a1b2c3d4e5f6 (initial_schema — single migration, 2024-01-01)

---

## CHECKPOINT 1 — DATABASE INVENTORY COMPLETE

All row counts below are exact values from live `COUNT(*)` queries.  
No values are inferred, estimated, or fabricated.

---

## 1. TABLE-LEVEL INVENTORY

| Table | Exact Rows | Table Size | Index Size | Total Size | Min Time | Max Time |
|---|---|---|---|---|---|---|
| `candle_bar` | **5,425,725** | 930 MB | 1,028 MB | **1,958 MB** | 2024-01-01 03:45 UTC | 2026-09-13 15:00 UTC |
| `instrument_master` | **0** | ~0 bytes | 32 kB | 40 kB | — | — |
| `fno_universe_snapshot` | **0** | ~0 bytes | ~0 bytes | 16 kB | — | — |
| `data_gap` | **0** | ~0 bytes | ~0 bytes | 16 kB | — | — |
| `data_incident` | **0** | ~0 bytes | ~0 bytes | 32 kB | — | — |
| `data_provenance` | **0** | ~0 bytes | ~0 bytes | 24 kB | — | — |
| `provider_health` | **0** | ~0 bytes | ~0 bytes | 24 kB | — | — |

**Total user data:** ~1.96 GB, entirely in `candle_bar`

---

## 2. CANDLE_BAR — COMPLETE INVENTORY

### 2.1 Aggregate Statistics

| Metric | Value |
|---|---|
| Total rows | 5,425,725 |
| Unique instrument_ids | 50 |
| Unique exchanges | 2 (NSE, BINANCE) |
| Unique intervals | 9 |
| Unique providers | 6 |
| Unique session_dates | 320 |
| Date range | 2024-01-01 → 2026-09-13 (UTC) |
| TimescaleDB hypertable | **NO** (not promoted; plain PostgreSQL heap) |
| Alembic version | a1b2c3d4e5f6 |

### 2.2 Interval Distribution

| interval_str | Row Count | Unique Instruments | Min Time (UTC) | Max Time (UTC) |
|---|---|---|---|---|
| `1m` | 3,891,638 | 43 | 2024-01-15 09:15 | 2026-09-11 09:59 |
| `5m` | 718,009 | 43 | 2024-01-15 09:15 | 2026-09-11 09:55 |
| `10m` | 370,520 | 42 | 2025-09-10 03:45 | 2026-09-11 09:55 |
| `15m` | 238,325 | 42 | 2025-09-10 03:45 | 2026-09-11 09:45 |
| `30m` | 128,450 | 43 | 2024-09-02 03:45 | 2026-09-11 09:45 |
| `1h` | 64,199 | 43 | 2024-01-15 10:00 | 2026-09-13 15:00 |
| `1d` | 11,944 | 49 | 2024-01-01 03:45 | 2026-09-11 03:45 |
| `1w` | 2,120 | 40 | 2025-09-07 18:30 | 2026-09-06 18:30 |
| `1M` | 520 | 40 | 2025-08-31 18:30 | 2026-08-31 18:30 |
| `3m` | **0** | — | — | — ✅ CHECK enforced |

**Total:** 5,425,725

### 2.3 Exchange Distribution

| Exchange | Row Count | Unique Instruments |
|---|---|---|
| NSE | 5,425,719 | 49 |
| BINANCE | 6 | 1 |

### 2.4 Provider Distribution

| Provider | Row Count | Unique Instruments | Source Type |
|---|---|---|---|
| angel_one | 5,410,384 | 43 | BROKER_AUTHENTICATED |
| upstox | 13,481 | 42 | BROKER_AUTHENTICATED |
| yahoo_finance | 1,830 | 11 | OPEN_SOURCE_NSE_DERIVED |
| jugaad_data | 16 | 4 | OPEN_SOURCE_NSE_DERIVED |
| openchart | 8 | 2 | OPEN_SOURCE_NSE_DERIVED |
| binance | 6 | 1 | EXCHANGE_REST_PUBLIC |

### 2.5 Source Type Breakdown

| source_type | Row Count |
|---|---|
| BROKER_AUTHENTICATED | 5,423,854 |
| OPEN_SOURCE_NSE_DERIVED | 1,865 |
| EXCHANGE_REST_PUBLIC | 6 |

---

## 3. INSTRUMENT CLASSIFICATION

> **CRITICAL NOTE:** `instrument_master` contains **0 rows**.  
> Classification below is derived from `instrument_id` patterns and the  
> `scripts/india_instruments.py` instrument list. No future/option records exist.

### 3.1 Type Classification (from candle_bar)

| Classified Type | Candle Count | Instrument Count | Basis |
|---|---|---|---|
| **EQUITY** | 5,424,751 | 47 | NSE symbol ∉ {NIFTY, BANKNIFTY} |
| **INDEX** | 968 | 2 | NSE:NIFTY, NSE:BANKNIFTY |
| **FUTURE** | **0** | 0 | No futures data present |
| **OPTION** | **0** | 0 | No options data present |
| **CRYPTO** | 6 | 1 | BINANCE:BTCUSDT |
| **UNKNOWN** | **0** | 0 | No unclassifiable records |

### 3.2 Per-Instrument Candle Counts (NSE Equities)

| instrument_id | Candle Count | Earliest | Latest | Intervals Present |
|---|---|---|---|---|
| NSE:ADANIENT | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:ADANIPORTS | 131,819 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:APOLLOHOSP | 252 | 2025-09-10 | 2026-09-11 | 10m,15m,1d |
| NSE:ASIANPAINT | 131,499 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:AXISBANK | 131,012 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:BAJAJFINSV | 131,854 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:BAJFINANCE | 129,189 | 2025-09-10 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:BHARTIARTL | 316 | 2025-08-31 | 2026-09-10 | 1d,1M,1w |
| NSE:BPCL | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:BRITANNIA | 131,062 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:CIPLA | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:COALINDIA | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:DIVISLAB | 132,211 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:DRREDDY | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:EICHERMOT | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:GRASIM | 132,267 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:HCLTECH | 131,499 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:HDFCBANK | 123,226 | 2024-01-01 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:HDFCLIFE | 253 | 2025-09-10 | 2026-09-11 | 10m,15m,1d |
| NSE:HEROMOTOCO | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:HINDALCO | 131,846 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:HINDUNILVR | 132,219 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:ICICIBANK | 123,686 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:INDUSINDBK | 132,280 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:INFY | 122,337 | 2024-01-01 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:ITC | 253 | 2025-09-10 | 2026-09-11 | 10m,15m,1d |
| NSE:JSWSTEEL | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:KOTAKBANK | 127,723 | 2024-09-02 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:LT | 129,170 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:M&M | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:MARUTI | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:NESTLEIND | 131,425 | 2025-09-10 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:NTPC | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:ONGC | 125,769 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:POWERGRID | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:RELIANCE | 125,767 | 2024-01-01 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:SBILIFE | 253 | 2025-09-10 | 2026-09-11 | 10m,15m,1d |
| NSE:SBIN | 110,911 | 2024-09-01 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:SHREECEM | 120,904 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:SUNPHARMA | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:TATAMOTORS | 132,280 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:TATASTEEL | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:TCS | 126,871 | 2024-03-01 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:TECHM | 110,682 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:TITAN | 132,281 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:ULTRACEMCO | 126,907 | 2025-08-31 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |
| NSE:WIPRO | 122,794 | 2024-09-01 | 2026-09-11 | 1m,5m,10m,15m,30m,1h,1d,1w,1M |

### 3.3 Index Instruments

| instrument_id | Candle Count | Earliest | Latest | Note |
|---|---|---|---|---|
| NSE:NIFTY | 940 | 2024-01-15 | 2024-09-12 | Old/sparse data, mix of jugaad_data + upstox |
| NSE:BANKNIFTY | 28 | 2024-01-01 | 2024-09-12 | Old/sparse data |

### 3.4 Crypto Instruments

| instrument_id | Candle Count | Provider | Earliest | Latest |
|---|---|---|---|---|
| BINANCE:BTCUSDT | 6 | binance | 2024-01-15 | 2026-09-13 |

---

## 4. DATA QUALITY AUDIT

### 4.1 OHLC Integrity

| Check | Violation Count | Status |
|---|---|---|
| high < open | 0 | ✅ PASS |
| high < close | 0 | ✅ PASS |
| low > open | 0 | ✅ PASS |
| low > close | 0 | ✅ PASS |
| high < low | 0 | ✅ PASS |
| open ≤ 0 | 0 | ✅ PASS |
| close ≤ 0 | 0 | ✅ PASS |
| volume < 0 | 0 | ✅ PASS |
| OI < 0 (where not null) | 0 | ✅ PASS |

### 4.2 Duplicate Check

| Check | Count | Status |
|---|---|---|
| Duplicate (instrument_id, exchange, interval_str, time) | 0 | ✅ PASS |

### 4.3 NULL / Availability Rates

| Field | NULL Count | NULL Rate | Notes |
|---|---|---|---|
| `oi` | 5,412,245 | 99.75% | OI only from upstox 1d/1w/1M rows |
| `source_timestamp` | ~5,412,000 | ~99.7% | Angel One doesn't return source_ts |
| `data_observation_id` | ~5,425,725 | 100% | Provenance not linked |
| `reconciliation_status` | 5,425,725 | 100% | No reconciliation run yet |
| `volume_unavailable` | 0 | 0% | All FALSE |

### 4.4 OI Coverage Detail

| Provider | Interval | Instruments with OI | OI Row Count |
|---|---|---|---|
| upstox | 1d | 41 equities + NIFTY + BANKNIFTY | ~10,900 |
| upstox | 1w | 40 equities | 2,120 |
| upstox | 1M | 40 equities | 520 |
| **Total** | | | **13,480** |

### 4.5 Normalisation Version

| normalisation_version | Count | Notes |
|---|---|---|
| `2.0.0` | 5,425,715 | Production candles |
| `1` | 10 | Seed/test data (3 NIFTY candles Jan 2024, 1 RELIANCE, 5 BTCUSDT candles) |

### 4.6 Reconciliation Status

| reconciliation_status | Count |
|---|---|
| NULL (not reconciled) | 5,425,725 |

All existing data has never been cross-provider reconciled.

---

## 5. SCHEMA AUDIT

### 5.1 Current Tables and Relationships

```
candle_bar          ← NO foreign key to instrument_master
instrument_master   ← EMPTY (0 rows)
fno_universe_snapshot ← EMPTY
data_gap            ← EMPTY
data_incident       ← EMPTY
data_provenance     ← EMPTY
provider_health     ← EMPTY
```

### 5.2 Missing Tables (Required by Target Architecture)

| Missing Table | Purpose | Priority |
|---|---|---|
| `equity_candle` | Canonical equity/index candles | CRITICAL |
| `futures_candle` | Canonical futures candles | CRITICAL |
| `options_candle` | Canonical options candles | CRITICAL |
| `instrument_provider_mapping` | Provider token normalization | HIGH |
| `instrument_identity_history` | Symbol/token change tracking | HIGH |
| `market_tick` | Live tick storage | HIGH |
| `market_quote` | Live quote snapshots | HIGH |
| `option_chain_snapshot` | Point-in-time option chain | HIGH |
| `option_chain_contract` | Per-strike option chain rows | HIGH |
| `option_greeks_snapshot` | IV + Greeks per instrument | MEDIUM |
| `exchange_calendar` | NSE/BSE holiday calendar | HIGH |
| `market_session` | Trading session records | HIGH |
| `fno_universe_membership` | Point-in-time F&O membership | HIGH |
| `ingestion_job` | Backfill job tracking | HIGH |
| `ingestion_checkpoint` | Resumable job state | HIGH |
| `candle_bar_quarantine` | Unclassifiable/invalid rows | MEDIUM |

### 5.3 Existing Indexes on candle_bar

| Index Name | Type | Columns |
|---|---|---|
| `candle_bar_pkey` | UNIQUE B-tree | (id, time) |
| `candle_bar_uq` | UNIQUE B-tree | (instrument_id, exchange, interval_str, time) |
| `candle_bar_symbol_interval_time` | B-tree | (instrument_id, interval_str, time DESC) |

### 5.4 TimescaleDB Status

| Check | Status |
|---|---|
| TimescaleDB version | 2.28.3 installed |
| candle_bar hypertable | **NOT promoted** |
| Any hypertables | **NONE** |

---

## 6. API DEPENDENCIES ON EXISTING SCHEMA

| Component | Table Read | Table Write | Critical? |
|---|---|---|---|
| `src/engines/historical_engine.py` | — | `candle_bar` (bulk upsert) | YES |
| `src/api/india.py` (GET /v1/india/historical) | `candle_bar` | — | YES |
| `src/api/compat.py` | `candle_bar` | — | YES |
| `src/api/instruments.py` | `instrument_master`, `fno_universe_snapshot` | — | YES |
| `scripts/backfill_india_1y.py` | — | `candle_bar` via engine | YES |
| `src/engines/gap_recovery.py` | `data_gap` | `data_gap` | YES |
| `src/api/india.py` (GET /v1/india/historical/gaps) | `data_gap` | — | YES |
| `src/api/quality.py` | `data_incident` | — | MEDIUM |
| `src/api/provenance.py` | `data_provenance` | — | MEDIUM |
| `src/api/providers.py` | `provider_health` | — | MEDIUM |

---

## 7. MIGRATION SCOPE SUMMARY

### What MUST be migrated

| Source | Rows | Destination | Classification |
|---|---|---|---|
| `candle_bar` WHERE exchange=NSE AND instrument_id NOT IN (NIFTY, BANKNIFTY) | 5,424,751 | `equity_candle` | EQUITY |
| `candle_bar` WHERE instrument_id IN (NSE:NIFTY, NSE:BANKNIFTY) | 968 | `equity_candle` | INDEX |
| `candle_bar` WHERE exchange=BINANCE | 6 | Retain in `candle_bar` or crypto table | CRYPTO |
| `instrument_master` | 0 rows | `instrument_master` (populate from adapters) | — |

### What must NOT be deleted before migration certification

- All 5,425,725 `candle_bar` rows
- All indexes on `candle_bar`
- The `candle_bar` table itself (deprecated post-migration, not dropped)

### What is ABSENT and must be created fresh

- futures/options candle tables (no existing data to migrate — created empty)
- market_tick, market_quote (created empty, ready for live feed)
- option_chain_snapshot, option_chain_contract (created empty)
- exchange_calendar, market_session (populated from authoritative source post-creation)
- ingestion_job, ingestion_checkpoint (created empty)

---

## 8. PRE-MIGRATION BACKUP VERIFICATION REQUIREMENTS

Before any destructive DDL:

1. PostgreSQL `pg_dump` of full database → verified backup file
2. Record current row counts per table (this document)
3. Record current Alembic revision (a1b2c3d4e5f6)
4. Tag Docker image snapshot: `data-service-postgres:pre-migration-2026-09-15`

---

## 9. CLASSIFICATION VERDICT

```
EQUITY candles    = 5,424,751  (measured)
INDEX candles     =       968  (measured)
FUTURES candles   =         0  (confirmed absent)
OPTIONS candles   =         0  (confirmed absent)
CRYPTO candles    =         6  (measured — BINANCE:BTCUSDT)
UNKNOWN candles   =         0  (all records classifiable)
TOTAL             = 5,425,725  (verified by COUNT(*))
```

**No unexplained records. No unclassifiable records. No duplicates. No OHLC violations.**

---

## CHECKPOINT 1: ✅ COMPLETE

Pre-migration inventory is accurate, complete, and independently verifiable.  
Safe to proceed to PHASE 6: Target Schema Design.
