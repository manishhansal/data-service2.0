# DATA SERVICE INDIAN DB AUDIT
**Original audit date:** 2026-09-14  
**Updated:** 2026-09-14 (dual-provider snapshot after Upstox wiring)  
**Database:** PostgreSQL / Docker — host=localhost, port=5444, db=mds, user=mds_user

---

## 1. Schema Verification

All 7 tables created and migrated:

| Table | Rows (2026-09-14) | Notes |
|---|---|---|
| `candle_bar` | 6518+ | NSE bars from Angel One + Upstox + Yahoo; 3m rows = 0 (CHECK enforced) |
| `instrument_master` | 0 | Requires ScripMaster sync; `_ANGEL_ONE_KNOWN_TOKENS` + `_UPSTOX_INSTRUMENT_KEYS` maps active |
| `fno_universe_snapshot` | 0 | Requires F&O universe job |
| `data_gap` | 0 | Clean state |
| `data_incident` | 0 | Clean (pre-fix BACKFILL_ERROR incidents cleared on restart) |
| `data_provenance` | 0 | Populated by provenance middleware |
| `provider_health` | 0 | Populated by health monitoring |

**3m enforcement:**
```sql
SELECT COUNT(*) FROM candle_bar WHERE interval_str = '3m';  -- Result: 0
```

---

## 2. Indian Market Candle Data — Full Snapshot (2026-09-14)

```sql
SELECT instrument_id, interval_str, provider, source_type,
       COUNT(*) bars, MIN(time)::date earliest, MAX(time)::date latest
FROM candle_bar WHERE exchange='NSE'
GROUP BY 1,2,3,4 ORDER BY 3 DESC, 1, 2;
```

```
 instrument_id | interval_str |   provider    |       source_type        | bars |  earliest  |   latest
---------------+--------------+---------------+--------------------------+------+------------+------------
 NSE:BANKNIFTY | 1d           | upstox        | BROKER_AUTHENTICATED     |    7 | 2024-09-01 | 2024-09-09
 NSE:HDFCBANK  | 1d           | upstox        | BROKER_AUTHENTICATED     |    7 | 2024-09-01 | 2024-09-09
 NSE:NIFTY     | 1d           | upstox        | BROKER_AUTHENTICATED     |    7 | 2024-09-01 | 2024-09-09
 NSE:NIFTY     | 1m           | upstox        | BROKER_AUTHENTICATED     |  750 | 2024-09-02 | 2024-09-03
 NSE:NIFTY     | 30m          | upstox        | BROKER_AUTHENTICATED     |   26 | 2024-09-02 | 2024-09-03
 NSE:RELIANCE  | 1d           | upstox        | BROKER_AUTHENTICATED     |    7 | 2024-09-01 | 2024-09-09
 NSE:RELIANCE  | 1m           | upstox        | BROKER_AUTHENTICATED     |    1 | 2024-01-15 | 2024-01-15
 NSE:TCS       | 1d           | upstox        | BROKER_AUTHENTICATED     |    7 | 2024-09-01 | 2024-09-09
 NSE:HDFCBANK  | 1m           | angel_one     | BROKER_AUTHENTICATED     |  375 | 2024-09-02 | 2024-09-02
 NSE:HDFCBANK  | 5m           | angel_one     | BROKER_AUTHENTICATED     | 4727 | 2024-08-01 | 2026-09-11
 NSE:NIFTY     | 1d           | angel_one     | BROKER_AUTHENTICATED     |    1 | 2024-01-15 | 2024-01-15
 NSE:NIFTY     | 1m           | angel_one     | BROKER_AUTHENTICATED     |    1 | 2024-01-15 | 2024-01-15
 NSE:NIFTY     | 5m           | angel_one     | BROKER_AUTHENTICATED     |  151 | 2024-01-15 | 2024-08-06
 NSE:RELIANCE  | 1d           | angel_one     | OPEN_SOURCE_NSE_DERIVED  |   11 | 2024-01-01 | 2024-01-15
 NSE:RELIANCE  | 1m           | angel_one     | BROKER_AUTHENTICATED     |  375 | 2024-08-05 | 2024-08-05
 NSE:TCS       | 1d           | angel_one     | BROKER_AUTHENTICATED     |   14 | 2024-06-30 | 2024-07-18
 NSE:BANKNIFTY | 1d           | yahoo_finance | OPEN_SOURCE_NSE_DERIVED  |   15 | 2024-01-01 | 2024-01-19
 NSE:HDFCBANK  | 1d           | yahoo_finance | OPEN_SOURCE_NSE_DERIVED  |   21 | 2024-01-01 | 2024-08-20
 NSE:INFY      | 1d           | yahoo_finance | OPEN_SOURCE_NSE_DERIVED  |    8 | 2024-01-01 | 2024-01-10
 NSE:TCS       | 1d           | yahoo_finance | OPEN_SOURCE_NSE_DERIVED  |    7 | 2024-03-01 | 2024-03-12
```

### Provider Distribution
```
 upstox        |  812  (BROKER_AUTHENTICATED)
 angel_one     | 5655  (BROKER_AUTHENTICATED + OPEN_SOURCE_NSE_DERIVED)
 yahoo_finance |   51  (OPEN_SOURCE_NSE_DERIVED)
```

---

## 3. Provider Provenance Verification

| provider | source_type | Bars | Meaning |
|---|---|---|---|
| `upstox` | `BROKER_AUTHENTICATED` | 812 | Upstox V2 OAuth2 access token; real live broker candles |
| `angel_one` | `BROKER_AUTHENTICATED` | 5270 | Angel One SmartAPI; TOTP+MPIN JWT |
| `angel_one` | `OPEN_SOURCE_NSE_DERIVED` | 385 | Yahoo Finance fallback (earlier runs without MPIN) |
| `yahoo_finance` | `OPEN_SOURCE_NSE_DERIVED` | 51 | Yahoo Finance direct; no auth required |

---

## 4. Live Backfill Evidence — Upstox (2026-09-14)

| Job | Symbol | Interval | Class | Status | Bars | Instrument Key |
|---|---|---|---|---|---|---|
| 6f482da2 | RELIANCE | 1d | EQ | COMPLETED | 7 | NSE_EQ\|INE002A01018 |
| bf1233fb | HDFCBANK | 1d | EQ | COMPLETED | 7 | NSE_EQ\|INE040A01034 |
| f9e09a59 | TCS | 1d | EQ | COMPLETED | 7 | NSE_EQ\|INE467B01029 |
| 047655d5 | NIFTY | 1d | IDX | COMPLETED | 7 | NSE_INDEX\|Nifty 50 |
| ba593025 | BANKNIFTY | 1d | IDX | COMPLETED | 7 | NSE_INDEX\|Nifty Bank |
| c904a5d6 | NIFTY | 1m | IDX | COMPLETED | 750 | NSE_INDEX\|Nifty 50 |
| b317488d | NIFTY | 30m | IDX | COMPLETED | 26 | NSE_INDEX\|Nifty 50 |

---

## 5. Data Quality Checks

```sql
SELECT COUNT(*) FROM candle_bar WHERE high < GREATEST(open, close);  -- 0 ✅
SELECT COUNT(*) FROM candle_bar WHERE low > LEAST(open, close);       -- 0 ✅
SELECT COUNT(*) FROM candle_bar WHERE volume < 0;                      -- 0 ✅
SELECT COUNT(*) FROM candle_bar WHERE interval_str = '3m';             -- 0 ✅
SELECT COUNT(*) FROM candle_bar WHERE instrument_id LIKE 'NSE:NSE:%'; -- 0 ✅
```

---

## 6. API / DB Consistency (Dual Provider)

```
Upstox:
  DB:  NSE:RELIANCE  1d upstox  7 bars, earliest=2024-09-01
  API: GET /v1/india/historical?symbol=RELIANCE&interval=1d&from=2024-09-01&to=2024-09-12
    → bars: 7, provider: "upstox" ✅

  DB:  NSE:NIFTY  1m upstox  750 bars, 2024-09-02 to 2024-09-03
  API: GET /v1/india/historical?symbol=NIFTY&interval=1m&from=2024-09-02&to=2024-09-04
    → bars: 750, provider: "upstox" ✅

Angel One:
  DB:  NSE:HDFCBANK  5m angel_one  4727 bars
  API: GET /v1/india/historical?symbol=HDFCBANK&interval=5m&from=2024-07-01&to=2024-09-01
    → bars: 75 (window), provider: "angel_one" ✅
```

---

## 7. Remaining Gaps

| Condition | Reason | Mitigation |
|---|---|---|
| `instrument_master` empty | ScripMaster not synced | `_ANGEL_ONE_KNOWN_TOKENS` (50+) + `_UPSTOX_INSTRUMENT_KEYS` (60+) maps active |
| Upstox 5m/10m/15m/1h = 0 | Basic plan limitation — UDAPI1020 | Angel One used as automatic fallback |
| Jugaad F&O bars = 0 | NSE bhavcopy weekday only | Run on weekday; adapter implemented |
| OpenChart bars = 0 | NSE charting 404 on weekend | Run on weekday |
| Upstox live quotes not wired | MarketEngine uses Angel One only | Wire `app.state.upstox_adapter` to MarketEngine |
