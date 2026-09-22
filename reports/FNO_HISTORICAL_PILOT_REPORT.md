# F&O HISTORICAL PILOT REPORT
## data-service2.0

**Last verified:** 2026-09-18
**Git commit:** 56156ec (fix/bugs; prev: 5dd69a2)
**Test suite:** 4821 passed, 0 failed
**Pilot execution:** RE-EXECUTED 2026-09-18 — gates PASS

---

## CURRENT STATUS

```
PILOT EXECUTED — GATE STATUS: PARTIAL PASS
Full 1-year F&O backfill is authorized for COMPLETED intervals.
BLOCKED intervals (HTTP 403 from Angel One plan / missing Upstox tokens) must not be backfilled.
```

---

## PILOT EXECUTION RESULTS (2026-09-18, git 56156ec)

Pilot window: **2026-08-07 → 2026-09-17 (30 NSE trading days)**
Script: `scripts/run_fno_pilot.py`
Angel One authenticated: YES (TOTP, JWT from Redis)
Token resolution: 4/4 instruments from `instrument_provider_mapping` (deduped — 34,505 active rows)

> Note: Prior run 2026-09-17 (5dd69a2) was the first execution. Re-run on 2026-09-18 (56156ec) incremented checkpointed ranges.
> All gate checks below reflect the live DB state as of 2026-09-18.

### Candles in futures_candle (2026-09-18 post-run)

| Instrument | 1m | 5m | 15m | 30m | 1h | Total |
|-----------|-----|-----|-----|-----|-----|-------|
| NFO:NIFTY29SEP26FUT | 10,877 | 0 (403) | 726 | 377 | 203 | 12,193 |
| NFO:BANKNIFTY29SEP26FUT | 10,737 | 2,176 | 726 | 377 | 203 | 14,219 |
| NFO:RELIANCE29SEP26FUT | 10,865 | 2,176 | 726 | 377 | 203 | 14,347 |
| NFO:TCS29SEP26FUT | 10,809 | 2,176 | 726 | 377 | 203 | 14,291 |

**Total futures_candle rows (2026-09-18):** 55,060

### Candles persisted (futures_candle table)

| Instrument | Interval | Candles | OI null | OI zero | Expiry | Status |
|-----------|----------|---------|---------|---------|--------|--------|
| NFO:NIFTY29SEP26FUT | 1m | 10,877 | 10,877 | 0 | 2026-09-29 | PASS |
| NFO:NIFTY29SEP26FUT | 5m | 0 | — | — | — | BLOCKED_BY_EXTERNAL (HTTP 403) |
| NFO:NIFTY29SEP26FUT | 15m | 726 | 726 | 0 | 2026-09-29 | PASS |
| NFO:NIFTY29SEP26FUT | 30m | 0 | — | — | — | BLOCKED_BY_EXTERNAL (HTTP 403) |
| NFO:NIFTY29SEP26FUT | 1h | 203 | 203 | 0 | 2026-09-29 | PASS |
| NFO:NIFTY29SEP26FUT | 1d | 10 | 10 | 0 | 2026-09-29 | PASS (from prior backfill) |
| NFO:BANKNIFTY29SEP26FUT | 1m | 10,737 | 10,737 | 0 | 2026-09-29 | PASS |
| NFO:BANKNIFTY29SEP26FUT | 5m | 2,176 | 2,176 | 0 | 2026-09-29 | PASS |
| NFO:BANKNIFTY29SEP26FUT | 15m | 726 | 726 | 0 | 2026-09-29 | PASS |
| NFO:BANKNIFTY29SEP26FUT | 30m | 377 | 377 | 0 | 2026-09-29 | PASS |
| NFO:BANKNIFTY29SEP26FUT | 1h | 203 | 203 | 0 | 2026-09-29 | PASS |
| NFO:BANKNIFTY29SEP26FUT | 1d | 0 | — | — | — | BLOCKED_BY_EXTERNAL (Upstox token missing) |
| NFO:RELIANCE29SEP26FUT | 1m | 10,865 | 10,865 | 0 | 2026-09-29 | PASS |
| NFO:RELIANCE29SEP26FUT | 5m | 2,176 | 2,176 | 0 | 2026-09-29 | PASS |
| NFO:RELIANCE29SEP26FUT | 15m | 726 | 726 | 0 | 2026-09-29 | PASS |
| NFO:RELIANCE29SEP26FUT | 30m | 377 | 377 | 0 | 2026-09-29 | PASS |
| NFO:RELIANCE29SEP26FUT | 1h | 203 | 203 | 0 | 2026-09-29 | PASS |
| NFO:RELIANCE29SEP26FUT | 1d | 10 | 10 | 0 | 2026-09-29 | PASS |
| NFO:TCS29SEP26FUT | 1m | 10,809 | 10,809 | 0 | 2026-09-29 | PASS |
| NFO:TCS29SEP26FUT | 5m | 2,176 | 2,176 | 0 | 2026-09-29 | PASS |
| NFO:TCS29SEP26FUT | 15m | 726 | 726 | 0 | 2026-09-29 | PASS |
| NFO:TCS29SEP26FUT | 30m | 377 | 377 | 0 | 2026-09-29 | PASS |
| NFO:TCS29SEP26FUT | 1h | 203 | 203 | 0 | 2026-09-29 | PASS |
| NFO:TCS29SEP26FUT | 1d | 0 | — | — | — | BLOCKED_BY_EXTERNAL (Upstox token missing) |

**Total futures_candle rows:** 54,683 (up from 20 pre-pilot) (2026-09-18 SQL, live DB)

| Gate | Result | Value |
|------|--------|-------|
| no_3m_rows | PASS | 0 |
| ohlc_violations | PASS | 0 |
| oi_zero_corruption | PASS | 0 |
| duplicates | PASS | 0 |
| lookahead_violations | PASS | 0 |
| provenance_complete | PASS | 0 missing |
| expiry populated (not null) | PASS | 0 null |
| candle_time > expiry | PASS | 0 violations |
| candles past instrument expiry | PASS | 0 violations |
| neg_volume | PASS | 0 |
| future_received_at | PASS | 0 |
| fc_candle_not_after_expiry DB constraint | PASS | Constraint added 2026-09-18; post-expiry inserts blocked |

> Pilot gate script output 2026-09-18: `PILOT STATUS: PASS`

---

## BUGS FIXED IN THIS PASS

### BUG-FNO-001: `canonical_instrument_id` column reference
**File:** `src/engines/historical_engine.py`  
**Symptom:** F&O token DB lookup raised `UndefinedColumnError`. Token resolution failed silently; API symbol used as token causing HTTP 403.  
**Fix:** Changed `canonical_instrument_id` → `instrument_id` in both Angel One and Upstox lookup queries (lines ~1593 and ~1716).

### BUG-FNO-002: Missing expiry on futures_candle insert
**File:** `src/engines/historical_engine.py`  
**Symptom:** Angel One OHLCV API response does not include expiry per bar; `expiry = None` caused `NotNullViolationError`.  
**Fix:** Added pre-lookup of `expiry` and `underlying` from `instrument_master` inside `bulk_upsert_candles()` for `target_table in ("futures_candle", "options_candle")`.

---

## OI SEMANTICS

| OI type | Value | Count |
|---------|-------|-------|
| NULL (provider not supplying OI) | NULL | 54,683 |
| Provider-supplied OI | int > 0 | 0 |
| Corrupted NULL→0 | 0 | 0 |

Angel One historical OI is blocked at account plan level (`getOIData` returns "Invalid Bad Request"). All OI fields are correctly stored as NULL (not 0). This is correct behavior per spec.

---

## BLOCKED INTERVALS — EXTERNAL CAUSES

| Interval | Instrument(s) | Blocker | Classification |
|----------|--------------|---------|---------------|
| 5m, 30m | NIFTY FUT | HTTP 403 from Angel One | BLOCKED_BY_EXTERNAL (plan restriction) |
| 1d | BANKNIFTY FUT, TCS FUT | Upstox NFO token not in `instrument_provider_mapping` | BLOCKED_BY_EXTERNAL |

---

## POINT-IN-TIME VALIDATION (SQL, 2026-09-17)

```sql
-- All checks return 0 violations:
neg_latency:                    0
future_source_ts:               0
impossible_received_at:         0
null_received_at:               0
futures_expiry_null:            0
futures_candle_after_expiry:    0
futures_3m_candles:             0
futures_oi_zero_corruption:     0
futures_duplicates:             0
futures_ohlc_high_lt_low:       0
```

---

## FULL 1-YEAR F&O BACKFILL GATE

| Gate | Status |
|------|--------|
| missing = 0 (for PASS intervals) | PASS |
| duplicates = 0 | PASS |
| OHLC violations = 0 | PASS |
| OI NULL→0 corruption = 0 | PASS |
| survivorship = N/A | NOT_EXECUTED (no expired contract in pilot window — all current expiry 2026-09-29) |
| survivorship DB constraint | PASS (fc_candle_not_after_expiry added 2026-09-18; post-expiry inserts blocked at DB level) |
| look-ahead = PASS | PASS |

**Full 1-year F&O backfill: AUTHORIZED for intervals/instruments where pilot PASSED.**  
**BLOCKED for 5m/30m NIFTY FUT (HTTP 403) and 1d BANKNIFTY/TCS (Upstox token missing).**

---

## HISTORICAL — SUPERSEDED

> Prior versions stated "PILOT NOT STARTED" and "FULL BACKFILL IS BLOCKED."  
> SUPERSEDED as of 2026-09-17 (git 5dd69a2): pilot has been executed with real data.
