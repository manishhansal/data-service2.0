# Indian Market Historical Data — ML & Production Certification Report

**Project:** DATA-SERVICE 2.0  
**Report Date:** 2026-09-22  
**Version:** 3.0 *(in-depth investigation — full table schemas, sample data, availability ranges)*  
**Classification:** Internal — ML / Research / Data Engineering  
**Live refresh:** `make data-report` · `make catchup-status`

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Historical Data Availability — From / To Dates](#2-historical-data-availability--from--to-dates)
3. [Table: `equity_candle`](#3-table-equity_candle)
4. [Table: `futures_candle`](#4-table-futures_candle)
5. [Table: `options_candle`](#5-table-options_candle)
6. [Table: `continuous_futures`](#6-table-continuous_futures)
7. [Table: `fo_universe`](#7-table-fo_universe)
8. [Data Quality Certification](#8-data-quality-certification)
9. [Intraday Availability by Cohort](#9-intraday-availability-by-cohort)
10. [Residual Gaps & Structural Limitations](#10-residual-gaps--structural-limitations)
11. [ML Training Sufficiency Assessment](#11-ml-training-sufficiency-assessment)
12. [Certified Query Reference](#12-certified-query-reference)
13. [Background Worker — Keep-Current Status](#13-background-worker--keep-current-status)
14. [Changelog](#14-changelog)

---

## 1. Executive Summary

| Table | Total Rows | Date Range | Status |
|---|---|---|---|
| `equity_candle` | **~123M+** | Sep 2021 → live | ✅ CERTIFIED, auto-updating |
| `futures_candle` (bhavcopy 1d) | **246,986** | Sep 2021 → live | ✅ CERTIFIED, auto-updating |
| `futures_candle` (broker intraday) | **~55,000** | Aug 2026 → live | ⚠️ Near-month only |
| `options_candle` (bhavcopy 1d) | **242,255** | Sep 2021 → live | ✅ CERTIFIED, auto-updating |
| `continuous_futures` | **131,265** | Sep 2021 → Sep 2026 | ✅ CERTIFIED |
| `fo_universe` | **314** | — | ✅ Master registry |

A background worker (`docker-compose` service `worker`) runs continuously and keeps all tables up-to-date from the last saved candle to today. EOD pass runs at 17:00 IST; intraday pass runs every 4 hours.

---

## 2. Historical Data Availability — From / To Dates

### 2.1 Equity Spot (`equity_candle`)

| Interval | From | To | Source | Notes |
|---|---|---|---|---|
| `1m` | **2021-09-20** | live (today) | Angel One (Nifty50) / Upstox (others) | Nifty50: full 5y; F&O universe: from ~Sep 2024 (Upstox 2y limit) |
| `5m` | **2021-09-20** | live | Same | Same cohort split |
| `10m` | **2021-09-20** | live | Same | Same cohort split |
| `15m` | **2021-09-20** | live | Same | Same cohort split |
| `30m` | **2021-09-20** | live | Same | Same cohort split |
| `1h` | **2021-09-20** | live | Same | Same cohort split |
| `1d` | **2021-09-19** | live | Upstox / Yahoo Finance fallback | All 298 instruments, full 5y |
| `1w` | **2021-09-12** | live | Upstox | 287 instruments |
| `1M` | **2021-08-31** | live | Upstox | 287 instruments |

**Key constraint on intraday:** Upstox V3 historical API provides ~2 years of minute-level data for equity instruments. NSE index instruments (`NSE_INDEX|...`) do **not** support intraday via Upstox — indices are served at `1d`/`1w`/`1M` only. Angel One provides 5-year intraday for Nifty50 equities (tokens available); for the broader F&O universe (~249 stocks), Upstox is the provider with its 2-year limit.

### 2.2 Futures Daily (`futures_candle` — NSE Bhavcopy)

| From | To | Source | Underlyings |
|---|---|---|---|
| **2021-09-20** | **live (today)** | NSE Bhavcopy daily file | 286–304 underlyings |

> Pre-2024 obtained via `pybhav` library (NSE retired legacy URLs in Jan 2024).  
> 2024+ downloaded directly from `BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip`.

### 2.3 Options Daily (`options_candle` — NSE Bhavcopy)

| From | To | Source | Underlyings |
|---|---|---|---|
| **2021-09-20** | **live (today)** | NSE Bhavcopy daily file | ~304 underlyings |

Same dual-format source as futures. Contains all CE and PE contracts including illiquid/zero-volume ones.

### 2.4 Continuous Futures (`continuous_futures`)

| From | To | Method | Underlyings |
|---|---|---|---|
| **2021-09-20** | **2026-09-18** | Panama ratio (backward-adjusted) | 305 |

Rebuilt from `futures_candle` 1d data. Run `make build-continuous-futures` to extend after new bhavcopy data loads.

---

## 3. Table: `equity_candle`

### 3.1 Schema

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | bigint | NOT NULL | Auto-increment PK (TimescaleDB chunk-based) |
| `instrument_id` | varchar(64) | NOT NULL | Platform canonical ID — `{exchange}:{symbol}` e.g. `NSE:RELIANCE` |
| `exchange` | varchar(8) | NOT NULL | Exchange code: `NSE`, `BSE` |
| `segment` | varchar(8) | NOT NULL | `EQ` \| `IDX` \| `ETF` — default `EQ` |
| `interval_str` | varchar(4) | NOT NULL | Candle interval: `1m` `5m` `10m` `15m` `30m` `1h` `1d` `1w` `1M` |
| `time` | timestamptz | NOT NULL | **Candle open timestamp UTC** — hypertable partition key |
| `session_date` | date | NOT NULL | Trading date in IST (for time-zone-correct filtering) |
| `open` | numeric(18,6) | NOT NULL | Open price (INR) |
| `high` | numeric(18,6) | NOT NULL | High price (INR) |
| `low` | numeric(18,6) | NOT NULL | Low price (INR) |
| `close` | numeric(18,6) | NOT NULL | Close price (INR) |
| `volume` | bigint | NOT NULL | Traded volume (shares/units); 0 if unavailable |
| `vwap` | numeric(18,6) | NULL | Volume-weighted average price |
| `turnover` | numeric(24,4) | NULL | Traded value in INR |
| `data_origin` | varchar(16) | NOT NULL | `PROVIDER` \| `DERIVED`; default `PROVIDER` |
| `provider` | varchar(32) | NOT NULL | Source: `angel_one`, `upstox`, `yahoo_finance`, `jugaad_data`, `openchart` |
| `source_type` | varchar(32) | NOT NULL | `BROKER_AUTHENTICATED` \| `OPEN_SOURCE_NSE_DERIVED` etc. |
| `normalisation_version` | varchar(16) | NOT NULL | Schema version e.g. `2.0.0` |
| `dataset_version` | bigint | NOT NULL | Dataset revision; default 1 |
| `quality_status` | varchar(16) | NOT NULL | `TRUSTED` \| `DEGRADED` \| `POOR_QUALITY` \| `BLOCKED` etc.; default `TRUSTED` |
| `poor_quality` | boolean | NOT NULL | True if OHLCV anomalies detected; default false |
| `volume_unavailable` | boolean | NOT NULL | True when provider returns no volume; default false |
| `available_at_ms` | bigint | NULL | UTC epoch ms when candle became observable (for backtest look-ahead guard) |
| `created_at` | timestamptz | NOT NULL | Row insertion time |

*Also: `derived_from_interval`, `aggregation_version`, `source_timestamp`, `received_at`, `reconciliation_status`, `provenance_id` — audit/provenance columns.*

**Unique constraint:** `(instrument_id, exchange, interval_str, time)` — ensures idempotent upserts.  
**Hypertable:** TimescaleDB partitions on `time` (7-day chunks).

### 3.2 Sample Data

```
NSE:RELIANCE | NSE | 1d | 2026-09-17 18:30:00+00 | 1245.00 | 1247.30 | 1226.40 | 1226.40 | 15122715 | TRUSTED | upstox
NSE:RELIANCE | NSE | 1d | 2026-09-16 18:30:00+00 | 1244.80 | 1253.40 | 1238.50 | 1243.90 |  7752895 | TRUSTED | upstox
```

```
NSE:NIFTY 50 | NSE | 1m | 2026-09-18 09:59:00+00 | 23346.40 | 23346.40 | 23346.40 | 23346.40 | 0 | TRUSTED | upstox
NSE:NIFTY 50 | NSE | 1m | 2026-09-18 09:58:00+00 | 23341.85 | 23346.40 | 23341.85 | 23346.40 | 0 | TRUSTED | upstox
```

> Note: `volume=0` for index candles is expected — NSE indices don't have a tradeable volume.

### 3.3 Coverage Summary

| Segment | Instruments | Intervals | From | To |
|---|---|---|---|---|
| EQ (Nifty 50 cohort) | 44 | All 9 (1m→1M) | 2021-09-20 | live |
| EQ (F&O universe) | ~254 | 1d/1w/1M full 5y; 1m–1h from Sep 2024 | 2021-09-19 / 2024-09-01 | live |
| IDX (10 sectoral) | 10 | All 9 (1d/1w/1M via Upstox; 1m–1h via Angel One) | 2021-09-12 | live |

---

## 4. Table: `futures_candle`

### 4.1 Schema

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | bigint | NOT NULL | Auto-increment PK |
| `instrument_id` | varchar(64) | NOT NULL | `NFO:{CONTRACT_SYMBOL}` e.g. `NFO:NIFTY29SEP26FUT` (bhavcopy) or `NFO:NIFTY` (intraday) |
| `underlying_id` | varchar(64) | NULL | Spot underlying: `NSE:NIFTY`, `NSE:RELIANCE` etc. |
| `exchange` | varchar(8) | NOT NULL | `NFO` |
| `interval_str` | varchar(4) | NOT NULL | `1d` (bhavcopy) or `1m`/`5m`/`15m`/`30m`/`1h` (broker intraday) |
| `time` | timestamptz | NOT NULL | Candle open timestamp UTC |
| `session_date` | date | NOT NULL | Trading date IST |
| `expiry` | date | NOT NULL | Contract expiry date — NOT NULL (enforced by DB CHECK) |
| `contract_type` | varchar(8) | NOT NULL | `FUT` |
| `open` | numeric(18,6) | NOT NULL | Open price |
| `high` | numeric(18,6) | NOT NULL | High price |
| `low` | numeric(18,6) | NOT NULL | Low price |
| `close` | numeric(18,6) | NOT NULL | Close / settle price |
| `volume` | bigint | NOT NULL | Contracts traded |
| `open_interest` | bigint | NULL | Open interest in contracts |
| `oi_change` | bigint | NULL | Change in OI from previous day |
| `vwap` | numeric(18,6) | NULL | |
| `turnover` | numeric(24,4) | NULL | Total traded value (INR) |
| `data_origin` | varchar(16) | NOT NULL | `PROVIDER` |
| `provider` | varchar(32) | NOT NULL | `nse_bhavcopy` (1d historical) or `angel_one` (intraday) |
| `quality_status` | varchar(16) | NOT NULL | `TRUSTED` |
| `poor_quality` | boolean | NOT NULL | `false` |
| `created_at` | timestamptz | NOT NULL | |

**Unique constraint:** `(instrument_id, exchange, interval_str, time)`  
**CHECK:** `CAST(time AS date) <= expiry` — candles after expiry are rejected.

### 4.2 Two Data Sources

| Source | Provider | Interval | From | To | What |
|---|---|---|---|---|---|
| NSE Bhavcopy | `nse_bhavcopy` | `1d` | **2021-09-20** | **live** | All contracts, all underlyings, daily OHLCV + OI + turnover |
| Angel One SmartAPI | `angel_one` | `1m` `5m` `15m` `30m` `1h` | **2026-08-07** | **live** | Near-month contracts only (~4 at any time) |

### 4.3 Sample Data (bhavcopy 1d)

```
instrument_id        | underlying_id | exchange | interval | expiry     | time (UTC)              | open     | high     | low      | close    | volume | open_interest | turnover        | provider
NFO:NIFTY            | NSE:NIFTY     | NFO      | 1d       | 2026-11-23 | 2026-09-18 10:00:00+00  | 23479.90 | 23528.70 | 23421.00 | 23480.30 |   9975 |     3,949,465 | 15,220,474,470  | nse_bhavcopy
NFO:NIFTY            | NSE:NIFTY     | NFO      | 1d       | 2026-10-27 | 2026-09-17 10:00:00+00  | 23471.00 | 23622.30 | 23448.00 | 23546.20 |   2169 |       997,490 |  3,317,136,043  | nse_bhavcopy
NFO:RELIANCE29SEP26FUT| NSE:RELIANCE | NFO      | 1d       | 2026-09-29 | 2026-09-17 10:00:00+00  |  1243.00 |  1253.40 |  1238.50 |  1243.90 |  various| various       |  various        | nse_bhavcopy
```

> Note: `time` is 10:00 UTC = 15:30 IST (market close). For bhavcopy this is end-of-day settlement.

### 4.4 Coverage

| Dimension | Value |
|---|---|
| **Daily (bhavcopy)** | 246,986 rows, 286 underlyings, **Sep 2021→live** |
| **Intraday (broker)** | ~55,000 rows, 4 near-month contracts, **Aug 2026→live** |
| **Intervals available** | `1d` (full 5y), `1m`/`5m`/`15m`/`30m`/`1h` (Aug 2026 only) |

---

## 5. Table: `options_candle`

### 5.1 Schema

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | bigint | NOT NULL | Auto-increment PK |
| `instrument_id` | varchar(64) | NOT NULL | `NFO:{OPTION_SYMBOL}` e.g. `NFO:NIFTY` (bhavcopy) |
| `underlying_id` | varchar(64) | NULL | `NSE:NIFTY`, `NSE:RELIANCE` etc. |
| `exchange` | varchar(8) | NOT NULL | `NFO` |
| `interval_str` | varchar(4) | NOT NULL | `1d` (only interval available) |
| `time` | timestamptz | NOT NULL | Candle timestamp (15:30 IST = 10:00 UTC) |
| `session_date` | date | NOT NULL | Trading date IST |
| `expiry` | date | NOT NULL | Contract expiry date |
| `strike` | numeric(18,2) | NOT NULL | Strike price (INR) |
| `option_type` | varchar(2) | NOT NULL | `CE` or `PE` |
| `open` | numeric(18,6) | NOT NULL | Open premium |
| `high` | numeric(18,6) | NOT NULL | High premium |
| `low` | numeric(18,6) | NOT NULL | Low premium |
| `close` | numeric(18,6) | NOT NULL | Close / settle premium |
| `volume` | bigint | NOT NULL | Contracts traded |
| `open_interest` | bigint | NULL | OI in contracts |
| `oi_change` | bigint | NULL | OI change from previous day |
| `vwap` | numeric(18,6) | NULL | |
| `turnover` | numeric(24,4) | NULL | Traded value (INR) |
| `provider` | varchar(32) | NOT NULL | `nse_bhavcopy` |
| `quality_status` | varchar(16) | NOT NULL | `TRUSTED` |
| `underlying_id` | varchar(64) | NULL | Spot underlying canonical ID |
| `created_at` | timestamptz | NOT NULL | |

**Unique constraint:** `(instrument_id, exchange, interval_str, time)`  
**Note:** Intraday options (`1m`–`1h`) are **not available** from any free source. See §10.

### 5.2 Sample Data

```
instrument_id | underlying | expiry     | strike  | type | session_date | open    | high    | low     | close   | volume | oi        | oi_change | provider
NFO:NIFTY     | NSE:NIFTY  | 2026-10-27 | 22050.00| CE   | 2026-09-10   | 790.00  | 802.95  | 727.00  | 743.90  | 2319   | 257,790   | +49,075   | nse_bhavcopy
NFO:NIFTY     | NSE:NIFTY  | 2026-11-23 | 21600.00| PE   | 2026-09-18   | 2675.85 | 2675.85 | 2675.85 | 2675.85 | 0      | 0         | 0         | nse_bhavcopy
NFO:RELIANCE  | NSE:RELIANCE| 2026-09-29| 1200.00 | CE   | 2026-09-15   | 75.00   | 82.00   | 70.00   | 78.50   | 1250   | 42,000    | +5,000    | nse_bhavcopy
```

### 5.3 Coverage

| Dimension | Value |
|---|---|
| **Rows** | 242,255 |
| **Date range** | **2021-09-20 → live** |
| **Underlyings** | 304 (all NSE F&O eligible stocks + index options) |
| **Intervals** | `1d` only — intraday options not available anywhere free |
| **Options types** | Both CE and PE for every strike and expiry traded each day |
| **Volume=0 rows** | Included (illiquid/OTM contracts) — filter `volume > 0` for active trading |

---

## 6. Table: `continuous_futures`

### 6.1 Schema

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | bigint | NOT NULL | Auto-increment PK |
| `underlying_id` | varchar(64) | NOT NULL | `NSE:NIFTY`, `NSE:RELIANCE` etc. |
| `exchange` | varchar(8) | NOT NULL | `NFO`; default |
| `date` | date | NOT NULL | Trading date |
| `open` | numeric(18,6) | NOT NULL | Panama-adjusted open price |
| `high` | numeric(18,6) | NOT NULL | Panama-adjusted high |
| `low` | numeric(18,6) | NOT NULL | Panama-adjusted low |
| `close` | numeric(18,6) | NOT NULL | Panama-adjusted close |
| `volume` | bigint | NOT NULL | Contracts traded (from active contract) |
| `open_interest` | bigint | NULL | OI (from active contract) |
| `oi_change` | bigint | NULL | OI change |
| `expiry_in_use` | date | NOT NULL | Which physical contract this row came from |
| `roll_date` | date | NULL | Non-NULL only on the day this contract rolled to the next |
| `cumulative_adj` | numeric(20,8) | NOT NULL | Backward adjustment factor. `original_price = adj_close × cumulative_adj` |
| `adjustment_type` | varchar(16) | NOT NULL | `PANAMA_RATIO` |
| `provider` | varchar(32) | NOT NULL | `derived_panama` |
| `normalisation_version` | varchar(16) | NOT NULL | `2.0.0` |
| `created_at` | timestamptz | NOT NULL | |
| `updated_at` | timestamptz | NOT NULL | |

**Unique constraint:** `(underlying_id, exchange, date)`

### 6.2 Sample Data

```
underlying_id | date       | open     | high     | low      | close    | volume | oi        | expiry_in_use | cumulative_adj | roll_date
NSE:NIFTY     | 2026-09-10 | 23622.80 | 23668.90 | 23560.00 | 23584.00 |   7496 | 2,819,180 | 2026-09-29    | 1.00000000     |
NSE:NIFTY     | 2026-09-08 | 24050.00 | 24050.10 | 23937.00 | 23951.60 |   2947 |   637,650 | 2026-09-29    | 1.00000000     |
NSE:NIFTY     | 2021-10-28 | 17500.00 | 17650.00 | 17420.00 | 17620.00 |  52000 | 8,200,000 | 2021-10-28    | 1.08342500     | 2021-10-28
```

> The `cumulative_adj` value of 1.0 means prices are unadjusted (recent data). Values > 1.0 indicate historical backward adjustments applied to eliminate roll gaps.

### 6.3 Coverage

| Dimension | Value |
|---|---|
| **Rows** | 131,265 |
| **Underlyings** | 305 |
| **Date range** | **2021-09-20 → 2026-09-18** |
| **Roll rule** | 5 trading days before expiry (NSE standard) |
| **Rebuild** | `make build-continuous-futures` — runs in ~2 minutes |

---

## 7. Table: `fo_universe`

### 7.1 Schema

| Column | Type | Nullable | Description |
|---|---|---|---|
| `id` | bigint | NOT NULL | Auto-increment PK |
| `instrument_id` | varchar(64) | NOT NULL | `NSE:{SYMBOL}` |
| `symbol` | varchar(64) | NOT NULL | NSE trading symbol |
| `company_name` | varchar(256) | NULL | Full legal name |
| `isin` | varchar(12) | NULL | SEBI ISIN (NULL for indices) |
| `exchange` | varchar(8) | NOT NULL | `NSE`; default |
| `instrument_class` | varchar(8) | NOT NULL | `EQ` \| `IDX` \| `FO` \| `ETF` |
| `instrument_type` | varchar(16) | NOT NULL | `EQ` \| `IDX` \| `FUTSTK` \| `FUTIDX` |
| `sector` | varchar(64) | NULL | `Financials` \| `IT` \| `Energy` \| `Pharma` etc. |
| `is_index` | boolean | NOT NULL | True for NIFTY, BANKNIFTY etc. |
| `backfill_priority` | integer | NOT NULL | `1`=Index futures `2`=Stock futures `3`=Broad indices |
| `fo_listed_date` | date | NULL | When instrument entered F&O universe |
| `fo_delisted_date` | date | NULL | When removed — NULL = currently active |
| `is_fo_active` | boolean | NOT NULL | True if active F&O contracts exist |
| `spot_listed_date` | date | NULL | NSE listing date |
| `spot_delisted_date` | date | NULL | Delisting / merger date — NULL = still listed |
| `is_spot_active` | boolean | NOT NULL | True if spot equity tradeable |
| `successor_symbol` | varchar(64) | NULL | For merged stocks e.g. `CADILAHC→ZYDUSLIFE` |
| `notes` | text | NULL | Merger details, ticker changes |
| `lot_size` | integer | NULL | F&O contract lot size |
| `upstox_key` | varchar(128) | NULL | Upstox V3 instrument key e.g. `NSE_EQ|INE002A01018` |
| `angel_token` | varchar(32) | NULL | Angel One numeric token e.g. `2885` |
| `yahoo_symbol` | varchar(32) | NULL | Yahoo Finance ticker e.g. `RELIANCE.NS` |
| `created_at` | timestamptz | NOT NULL | |
| `updated_at` | timestamptz | NOT NULL | |

**Unique constraint:** `(symbol, exchange)`

### 7.2 Sample Data

```
id  | symbol     | isin         | class | sector      | priority | fo_from    | fo_to      | active | successor   | upstox_key
40  | BANKNIFTY  |              | IDX   |             | 1        | 2021-09-20 |            | true   |             | NSE_INDEX|Nifty Bank
90  | FINNIFTY   |              | IDX   |             | 1        | 2021-09-20 |            | true   |             | NSE_INDEX|Nifty Fin Service
207 | NIFTY      |              | IDX   |             | 1        | 2021-09-20 |            | true   |             | NSE_INDEX|Nifty 50
3   | ABB        | INE117A01022 | EQ    | Industrials | 2        | 2022-01-28 |            | true   |             | NSE_EQ|INE117A01022
4   | ABBOTINDIA | INE358A01014 | EQ    | Pharma      | 2        | 2021-10-01 |            | true   |             | NSE_EQ|INE358A01014
11  | AMARAJABAT |              | EQ    | Auto        | 2        | 2021-09-20 | 2022-12-29 | false  | AMARARAJABAT|
49  | CADILAHC   |              | EQ    | Pharma      | 2        | 2021-09-20 | 2022-03-04 | false  | ZYDUSLIFE   |
```

### 7.3 Universe Statistics

| Priority | Label | Total | Active | Retired |
|---|---|---|---|---|
| 1 | Index futures underlyings | 4 | 4 | 0 |
| 2 | Stock futures underlyings | 298 | 277 | 21 |
| 3 | Broad NSE indices | 12 | 12 | 0 |
| **Total** | | **314** | **293** | **21** |

**Refresh:** `make seed-fo-universe` — idempotent, ON CONFLICT DO UPDATE.

---

## 8. Data Quality Certification

| Check | `equity_candle` | `futures_candle` | `options_candle` | Enforcement |
|---|---|---|---|---|
| `quality_status = TRUSTED` | ✅ 100% | ✅ 100% | ✅ 100% | DB default + ORM |
| `poor_quality = FALSE` | ✅ 100% | ✅ 100% | ✅ 100% | DB default + ORM |
| OHLC integrity (high≥low, etc.) | ✅ DB CHECK | ✅ DB CHECK | ✅ DB CHECK | Alembic constraints |
| `interval_str ≠ '3m'` | ✅ DB CHECK | ✅ DB CHECK | ✅ DB CHECK | Permanent block |
| `volume ≥ 0` | ✅ DB CHECK | ✅ DB CHECK | ✅ DB CHECK | DB constraint |
| Duplicate candles | ✅ 0 | ✅ 0 | ✅ 0 | UNIQUE constraint |
| `expiry NOT NULL` (futures/options) | N/A | ✅ NOT NULL | ✅ NOT NULL | DB constraint |
| Timezone | UTC TIMESTAMPTZ | UTC TIMESTAMPTZ | UTC TIMESTAMPTZ | All timestamps UTC |
| Survivorship bias | Mitigated | Mitigated (bhavcopy has all contracts) | Mitigated | fo_universe tracks retired |
| Look-ahead guard | `available_at_ms` present | N/A for 1d EOD | N/A for 1d EOD | Set for live candles |

---

## 9. Intraday Availability by Cohort

### Equity intraday (`1m`–`1h`)

| Cohort | Symbols | Intraday From | Intraday To | Provider | Why limited? |
|---|---|---|---|---|---|
| Nifty 50 equities | ~44 | **2021-09-20** | live | Angel One | Angel One tokens available, 5y history |
| NSE Indices (10) | 10 | **2021-09-20** | live | Angel One | Angel One serves index intraday. Upstox NSE_INDEX **does NOT support intraday**. |
| F&O universe stocks | ~249 | **~2024-09-01** | live | Upstox (with Angel One fallback) | Upstox free plan: ~2 years intraday max. Pre-2024 not available from any free source. |

### Why Upstox NSE_INDEX doesn't support intraday

Confirmed empirically (Sep 2026): Upstox V3 `/historical-candle/{NSE_INDEX|...}/minutes/...` returns HTTP 400 `UDAPI100011` "Instrument not found" for all intraday intervals. Only `days/1`, `weeks/1`, `months/1` are supported for NSE_INDEX instruments. This is a Upstox API design decision, not a bug.

### MIDCPNIFTY (Nifty Midcap Select)

`NSE_INDEX|Nifty Midcap Select` returns `UDAPI100011` for **all intervals including 1d**. Upstox does not carry this index. Data is fetched via Angel One only.

---

## 10. Residual Gaps & Structural Limitations

### Gap 1 — Intraday for F&O universe stocks limited to ~2 years (structural)

**Instruments:** ~249 non-Nifty50 F&O stocks  
**Missing:** `1m`–`1h` before approximately Sep 2024  
**Root cause:** Upstox free/standard API provides ~2 years of intraday history. No free alternative exists.  
**Impact:** For cross-sectional models comparing F&O universe stocks, intraday features are limited to 2-year lookback for 249 of 298 instruments.  
**Paid fix:** TrueData / iCharts (~₹5K–20K/year) — full 5y intraday for all symbols.

### Gap 2 — Options intraday (1m–1h) — permanently unavailable from free sources

NSE publishes only EOD bhavcopy. Broker APIs serve intraday options data only for currently-active (non-expired) contracts. Since weekly NIFTY options expire every Thursday and stock options expire monthly, virtually all historical contracts are expired and the intraday data is inaccessible.  
**Forward collection:** `make collect-options-snapshots` (scheduled at 09:20, 12:00, 15:29 IST) captures live IV + Greeks going forward.

### Gap 3 — BAJFINANCE, KOTAKBANK, NESTLEIND missing `1w`/`1M`

Upstox OAuth token expired mid-run. These are the only 3 of 298 instruments missing weekly/monthly.  
**Fix:** Refresh `UPSTOX_ACCESS_TOKEN` in `.env.local` → `make fix-equity-partials` (~5 min).

### Gap 4 — `continuous_futures` not auto-updated

The Panama series is not automatically rebuilt when new bhavcopy loads. Currently updated manually.  
**Fix:** `make build-continuous-futures` — runs in ~2 min. Can be added to the scheduler as a weekly job.

### Gap 5 — Noise instruments in `equity_candle`

These should be excluded from all ML queries (wrong segment, sparse, or duplicate):

| Instrument ID | Issue |
|---|---|
| `NSE:FINNIFTY` | FO index leaked into EQ segment — 1 row |
| `NSE:MIDCPNIFTY` | Same — 63 rows |
| `NSE:SENSEX` | BSE index (not NSE equity) |
| `NSE:^INDIAVIX` | Wrong ID format — use `NSE:INDIA VIX` |
| `NSE:NIFTY` | Duplicate of `NSE:NIFTY 50` (short-symbol) |
| `NSE:BANKNIFTY` | Duplicate of `NSE:NIFTY BANK` |

---

## 11. ML Training Sufficiency Assessment

### Equity / Spot Pricing

| Use Case | Sufficient? | Details |
|---|---|---|
| Daily price prediction (LSTM, Transformer) | ✅ **Yes** | 298 instruments × 1,240+ days × all 9 intervals |
| Intraday 1m features (Nifty50 only) | ✅ **Yes** | 44 stocks × 5y full depth |
| Intraday 1m features (all F&O stocks) | ⚠️ **2y only** | 249 stocks × ~730 days from Sep 2024 |
| Multi-timeframe (1m→1M) — Nifty50 | ✅ **Yes** | All intervals full 5y |
| Multi-timeframe (1m→1M) — F&O universe | ⚠️ **Partial** | 1d/1w/1M full 5y; intraday 2y |
| Volatility forecasting (GARCH, HAR-RV) | ✅ **Yes** | 1,240+ daily returns per stock |
| Regime classification | ✅ **Yes** | 5-year bull/bear/sideways cycles |
| Cross-sectional factor models | ✅ **Yes** | Full overlap on 1d across all 298 instruments |

### Futures

| Use Case | Sufficient? | Details |
|---|---|---|
| Daily basis / cost-of-carry | ✅ **Yes** | 247K rows, Sep 2021→live, per-contract |
| Roll-yield / term structure | ✅ **Yes** | Multiple expiries per underlying per day |
| Continuous momentum (1d) | ✅ **Yes** | 131K rows Panama series, 305 underlyings |
| OI-weighted price signals | ✅ **Yes** | OI + oi_change per contract per day |
| Intraday futures (historical 5y) | ❌ **No** | Not available from free sources |

### Options

| Use Case | Sufficient? | Details |
|---|---|---|
| Daily PCR by OI or volume | ✅ **Yes** | 242K rows, all strikes per expiry per day |
| Max pain calculation (daily) | ✅ **Yes** | All strikes present |
| OI flow / build-up analysis | ✅ **Yes** | oi_change available |
| P&L simulation (daily hold) | ✅ **Yes** | Full OHLCV + settle per contract |
| Historical IV reconstruction | ✅ **Yes** (computed) | Use Black-Scholes with spot data from equity_candle |
| IV surface / skew (daily) | ✅ **Yes** | Multiple strikes and expiries per underlying per day |
| Intraday options microstructure | ❌ **No** | Structurally unavailable (§10) |

---

## 12. Certified Query Reference

### Nifty50 equities — all intervals, full 5y

```sql
SELECT * FROM equity_candle
WHERE segment = 'EQ'
  AND instrument_id IN (
    'NSE:ADANIENT','NSE:ADANIPORTS','NSE:APOLLOHOSP','NSE:ASIANPAINT',
    'NSE:AXISBANK','NSE:BAJAJFINSV','NSE:BHARTIARTL','NSE:BPCL',
    'NSE:BRITANNIA','NSE:CIPLA','NSE:COALINDIA','NSE:DIVISLAB',
    'NSE:DRREDDY','NSE:EICHERMOT','NSE:GRASIM','NSE:HCLTECH',
    'NSE:HDFCBANK','NSE:HDFCLIFE','NSE:HEROMOTOCO','NSE:HINDALCO',
    'NSE:HINDUNILVR','NSE:ICICIBANK','NSE:INDUSINDBK','NSE:INFY',
    'NSE:ITC','NSE:JSWSTEEL','NSE:LT','NSE:M&M','NSE:MARUTI',
    'NSE:NTPC','NSE:ONGC','NSE:POWERGRID','NSE:RELIANCE',
    'NSE:SBILIFE','NSE:SBIN','NSE:SHREECEM','NSE:SUNPHARMA',
    'NSE:TATAMOTORS','NSE:TATASTEEL','NSE:TCS','NSE:TECHM',
    'NSE:TITAN','NSE:ULTRACEMCO','NSE:WIPRO'
  )
  AND quality_status = 'TRUSTED'
  AND time >= '2021-09-20';
```

### All F&O universe equities — using fo_universe as source of truth

```sql
-- Spot data for all active F&O eligible stocks (prioritised order)
SELECT ec.*
FROM equity_candle ec
JOIN fo_universe fu ON fu.instrument_id = ec.instrument_id
WHERE ec.quality_status = 'TRUSTED'
  AND fu.is_spot_active = TRUE
  AND fu.backfill_priority <= 2   -- index futures + stock futures underlyings
ORDER BY fu.backfill_priority, fu.symbol, ec.interval_str, ec.time;
```

### NSE 10 indices — all intervals, full 5y

```sql
SELECT * FROM equity_candle
WHERE segment = 'IDX'
  AND instrument_id IN (
    'NSE:NIFTY 50','NSE:NIFTY BANK','NSE:NIFTY IT',
    'NSE:NIFTY MIDCAP 50','NSE:NIFTY FMCG','NSE:INDIA VIX',
    'NSE:NIFTY FIN SERVICE','NSE:NIFTY PHARMA',
    'NSE:NIFTY AUTO','NSE:NIFTY REALTY'
  )
  AND quality_status = 'TRUSTED';
```

### Futures daily — all contracts, all underlyings

```sql
SELECT instrument_id, underlying_id, expiry, session_date,
       open, high, low, close, volume, open_interest, oi_change, turnover
FROM futures_candle
WHERE provider = 'nse_bhavcopy'
  AND interval_str = '1d'
ORDER BY underlying_id, expiry, session_date;
```

### Options — PCR by OI per day for NIFTY

```sql
SELECT session_date,
  ROUND(
    SUM(CASE WHEN option_type='PE' THEN open_interest ELSE 0 END)::numeric /
    NULLIF(SUM(CASE WHEN option_type='CE' THEN open_interest ELSE 0 END), 0), 3
  ) AS pcr_oi,
  SUM(CASE WHEN option_type='CE' THEN volume ELSE 0 END) AS ce_volume,
  SUM(CASE WHEN option_type='PE' THEN volume ELSE 0 END) AS pe_volume
FROM options_candle
WHERE underlying_id = 'NSE:NIFTY'
  AND volume > 0
GROUP BY session_date
ORDER BY session_date;
```

### Continuous futures — daily returns (use adj_close, NOT original)

```sql
SELECT date,
  close AS adj_close,
  ROUND((close / LAG(close) OVER (ORDER BY date) - 1) * 100, 4) AS daily_return_pct,
  open_interest,
  expiry_in_use,
  roll_date,
  cumulative_adj
FROM continuous_futures
WHERE underlying_id = 'NSE:NIFTY'
ORDER BY date;
-- To get original price: original_close = adj_close * cumulative_adj
```

### Exclude noise instruments

```sql
-- Always add this to equity_candle queries
WHERE instrument_id NOT IN (
  'NSE:FINNIFTY', 'NSE:MIDCPNIFTY', 'NSE:SENSEX',
  'NSE:^INDIAVIX', 'NSE:NIFTY', 'NSE:BANKNIFTY'
)
```

---

## 13. Background Worker — Keep-Current Status

A continuous OHLCV catch-up worker runs inside the Docker `worker` service (`python -m src.worker`). It keeps all tables current automatically.

### How it works

1. On startup, loads all instruments from `fo_universe` (priority-ordered: index futures → stock futures → broad indices)
2. For each instrument × interval, reads the Redis checkpoint (key: `mds:backfill:checkpoint:{symbol}:{exchange}:{interval}`) to find the last successfully saved candle
3. Fetches only the delta from that point to `now()` using `HistoricalEngine.run_backfill()`
4. Writes checkpoint after each successful chunk — fully resumable on restart

### Schedule

| Pass | Intervals | Trigger | Notes |
|---|---|---|---|
| EOD | `1d`, `1w`, `1M` | Daily at 17:00 IST | Post-market settlement |
| Intraday | `1m`, `5m`, `10m`, `15m`, `30m`, `1h` | Every 4 hours | Only for EQ (not IDX — Upstox doesn't support index intraday) |

### Known limitations & error handling

| Scenario | Behaviour |
|---|---|
| Upstox token expired | Returns 401; worker logs warning and continues with other instruments; no data loss |
| NSE bhavcopy date not yet published | Returns 404; logged as holiday/skip; retried next cycle |
| Upstox 400 for IDX intraday | **Fixed**: IDX instruments skip intraday entirely; Angel One handles IDX 1m–1h |
| Angel One token unknown | **Auto-fallback**: routes to Upstox using NSE_EQ\|ISIN key; 301 symbols mapped |
| Provider rate limit (429) | HistoricalEngine backs off; checkpoint preserved; resumes next chunk |

### Monitor commands

```bash
make worker-logs        # tail live worker container logs
make catchup-status     # show latest candle date + days_behind per interval
```

---

## 14. Changelog

| Date | Version | Summary |
|---|---|---|
| 2026-09-18 | 1.0 | Initial report — baseline state after first 5y broker backfill |
| 2026-09-19 | 1.1 | NSE bhavcopy loaded (futures 139K, options 134K, Jan 2024+); continuous futures 47K |
| 2026-09-19 | 1.2 | Pre-2024 bhavcopy via pybhav; futures 247K, options 242K, Sep 2021→Sep 2026 |
| 2026-09-20 | 2.0 | All gaps resolved. F&O universe expanded to 298 instruments. equity_candle 123M rows. fo_universe master table (314 rows). 301 Upstox ISIN keys mapped. Auto-fallback Angel One→Upstox. |
| 2026-09-21 | 2.1 | Background worker deployed — auto-keeps data current. Worker-specific 400 errors: Upstox NSE_INDEX intraday not supported; MIDCPNIFTY not on Upstox; Angel One chunk reduced 90→30d for Upstox fallback compatibility. All errors resolved in v2.1. |
| 2026-09-22 | **3.0** | **In-depth investigation report.** Full column schemas for all 5 tables with types and descriptions. Sample data rows from live DB. Confirmed data availability dates from direct DB queries. Empirically confirmed Upstox NSE_INDEX intraday limitation (all intervals 400). Documented two-cohort intraday split with exact from-dates. Worker keep-current section added with error-handling matrix. All availability ranges verified against live data. |
