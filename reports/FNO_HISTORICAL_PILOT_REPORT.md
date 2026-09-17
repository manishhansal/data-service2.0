# F&O HISTORICAL PILOT REPORT
## data-service2.0 — Phase 51 Report

**Original report date:** 2026-09-17  
**Updated:** 2026-09-17 (post live-testing; blockers discovered AND fixed)  
**Scope:** 30-trading-day F&O pilot backfill — pre-full-backfill gate  
**Evidence basis:** Zero — pilot not yet executed; some blockers resolved in this session  
**Gate 50 status:** NOT SATISFIED — updated blocker list below

---

## STATUS

```
PILOT NOT STARTED
```

The F&O historical pilot has not been run. This report documents the pilot scope, validation criteria, and the procedure required before the full 1-year F&O backfill may begin. It will be updated with real results when the pilot runs.

---

## PILOT READINESS ASSESSMENT

### Prerequisites checked (2026-09-17)

| Item | Status | Detail |
|------|--------|--------|
| Angel One auth | ✅ READY | TOTP auth confirmed live 2026-09-17; Redis JWT sharing confirmed |
| Upstox analytics token | ✅ READY | Valid until 2027-09-03; accepted by option chain + historical |
| Upstox OAuth access token | ❌ NOT READY | Expired 2026-09-14; must refresh before intraday calls |
| Angel One historical OHLCV EQ | ✅ VERIFIED | 1m (750 bars), 5m (152 bars), 1d (13 bars) — 2026-09-17 |
| Upstox historical OHLCV EQ/IDX | ✅ VERIFIED | 1d/1w/1M/1m/30m confirmed — 2026-09-17; all 9 intervals now supported (V3) |
| **Angel One historical OHLCV F&O** | ❌ **BLOCKED** | Tokens 35004/212816 return 0 bars. Must resolve correct tokens from `instrument_provider_mapping`. |
| **Angel One historical OI** | ❌ **BLOCKED** | `getOIData` returns "Invalid Bad Request" — suspected plan restriction |
| instrument_master populated | ✅ READY | 34,460 instruments (665 FUT, 33,745 OPT) in DB |
| instrument_provider_mapping | ✅ READY | 68,915 mappings; but F&O token resolution untested |
| fno_universe_membership | ✅ READY | 238 active memberships |
| exchange_calendar | ✅ READY | 3,654 rows (2024–2028) |
| futures_candle table | ✅ READY | 20 existing rows |
| options_candle table | ✅ READY | 0 rows; ready |
| DB CHECK 3m blocked | ✅ ENFORCED | |
| Checkpoint-resumable backfill | ✅ IMPLEMENTED | Redis key with no TTL |
| Upstox Plus plan (expired instruments) | ❌ NOT ACTIVE | Pilot must use active contracts only |
| **MarketEngine token bug (BUG-001)** | ✅ **FIXED** | Symbol→numeric token resolution via InstrumentMasterService — fixed 2026-09-17; zero 400s confirmed |

---

## PILOT SCOPE

### Instruments

| Instrument | Provider | Rationale |
|-----------|---------|-----------|
| NIFTY FUT (near month) | Angel One primary | Most liquid index future |
| BANKNIFTY FUT (near month) | Angel One primary | Second most liquid |
| RELIANCE FUT | Angel One primary | Most liquid stock future |
| TCS FUT | Angel One primary | Liquid stock future |
| NIFTY option chain (2 expiries) | Upstox analytics key | Most liquid option chain |
| BANKNIFTY option chain (2 expiries) | Upstox analytics key | Second most liquid |

### Period

```
30 trading days ending on pilot execution date
```

Exclude weekends and NSE holidays using `exchange_calendar` table. Do not fabricate trading days.

### Intervals

| Interval | Provider | Table |
|----------|---------|-------|
| 1m | Angel One | `futures_candle` / `options_candle` |
| 5m | Angel One | Same |
| 15m | Angel One | Same |
| 30m | Angel One | Same |
| 1h | Angel One | Same |
| 1d | Upstox V3 (OI included at index 6) | Same |

Note: 5m/15m/1h are blocked on Upstox basic plan (UDAPI1020). Angel One is the correct provider for intraday F&O.

### Historical OI

Fetch `getOIData` from Angel One for each interval above. OI must never be fabricated or sourced from `tradedVolume`. If `getOIData` returns empty for a contract, record `open_interest = NULL` with `oiMissing = True`.

---

## VALIDATION CRITERIA

For each pilot instrument and interval, verify:

| Check | Expected outcome | Action on failure |
|-------|-----------------|------------------|
| Row count = expected candles for 30 trading sessions | Zero gap | Log DataIncident; record gap |
| No 3m rows in any table | DB CHECK prevents insert | Would raise constraint error (impossible if working correctly) |
| First timestamp = first trading minute of pilot period | Exact match | Investigate chunking/checkpoint |
| Last timestamp = last candle of last session | Exact match | Check trailing data |
| Duplicate count | 0 | Dedup logic in ingestion |
| OHLC invariants: high ≥ max(O,C), low ≤ min(O,C) | 100% pass | Log OHLC violation + DataIncident |
| Volume ≥ 0 | 100% pass | Same |
| OI ≥ 0 or NULL | 100% pass | NULL→0 regression check |
| provider correct | `angel_one` for intraday, `upstox` for EOD where applicable | Check provenance.provider |
| source_type correct | `BROKER_AUTHENTICATED` | No `OPEN_SOURCE_NSE_DERIVED` for F&O |
| Timestamp timezone | UTC | All timestamps in DB must be UTC |
| Interval alignment | 1m bars start on the minute, 5m bars start at :00/:05/:10... | Check first second of each bar |

---

## EXPECTED PILOT OUTPUT (TEMPLATE)

This table must be populated with real results after the pilot runs.

| Instrument | Interval | Expected sessions | Actual sessions | Expected candles | Actual candles | Missing candles | Duplicates | OHLC violations | OI rows | OI null rows | Provider |
|-----------|----------|-------------------|----------------|-----------------|---------------|----------------|-----------|----------------|---------|-------------|---------|
| NIFTY FUT | 1m | 30 | — | ~4,500 | — | — | — | — | — | — | — |
| NIFTY FUT | 5m | 30 | — | ~900 | — | — | — | — | — | — | — |
| NIFTY FUT | 1d | 30 | — | 30 | — | — | — | — | — | — | — |
| BANKNIFTY FUT | 1d | 30 | — | 30 | — | — | — | — | — | — | — |
| RELIANCE FUT | 1d | 30 | — | 30 | — | — | — | — | — | — | — |
| TCS FUT | 1d | 30 | — | 30 | — | — | — | — | — | — | — |
| NIFTY CE ATM | 1d | 30 | — | ≤30 (expiry) | — | — | — | — | — | — | — |
| NIFTY PE ATM | 1d | 30 | — | ≤30 (expiry) | — | — | — | — | — | — | — |

---

## SURVIVORSHIP BIAS CHECK

The pilot must include at least one **expired contract** that was active during the 30-day window. This verifies that:

1. `fno_universe_membership.effective_from` and `effective_to` correctly scope the universe to each date
2. Contracts that expired mid-window are included for their active dates and excluded after expiry
3. No current-universe-only filtering is applied

**Procedure:** Before running the pilot, query `fno_universe_membership` for contracts whose `effective_to` falls within the pilot window. Include these in the instrument list. Verify their candles end on `effective_to`, not on the pilot end date.

---

## LOOK-AHEAD BIAS CHECK

For each record in the pilot dataset:

```sql
SELECT COUNT(*) FROM futures_candle
WHERE available_at_ms > candle_time_ms
```

Expected result: **0**

No future information may appear in a record whose `candle_time` is T. The `available_at_ms` field on every record must satisfy `available_at_ms ≥ candle_time_ms` (data becomes available at or after the bar closes) and `available_at_ms ≤ ingestion_time_ms`.

---

## PILOT EXECUTION COMMAND

```bash
# From workspace root, with .env.local loaded
python3 scripts/backfill_india_1y.py \
  --pilot \
  --days 30 \
  --instruments NIFTY_FUT,BANKNIFTY_FUT,RELIANCE_FUT,TCS_FUT \
  --intervals 1m,5m,15m,30m,1h,1d \
  --include-options NIFTY,BANKNIFTY
```

If this script does not accept `--pilot` scope, run it in a controlled manner and stop after the pilot instruments complete. Do not start the full universe until this report shows `PILOT_PASS`.

---

## GATE DECISION

Full 1-year F&O backfill may begin only when this report is updated with:

```
pilot_status: PASS
total_expected_candles: {N}
total_actual_candles: {N}
missing_candles: 0
duplicate_candles: 0
ohlc_violations: 0
oi_null_to_zero_corruptions: 0
survivorship_bias_check: PASS
look_ahead_check: PASS
```

Until then: **FULL BACKFILL IS BLOCKED.**

---

*Pilot not executed as of 2026-09-17. This report will be updated with real results after pilot completion.*

---

## UPDATE — PHASE B-K REMEDIATION (2026-09-17)

**Git commit:** 692bf3f

### Code prerequisites PASS (verified locally 2026-09-17)

| Gate | Previous | Current | Evidence |
|------|---------|---------|---------|
| F&O token lookup queries instrument_provider_mapping | BROKEN (used instrument_master only) | **FIXED** | BUG-012: `historical_engine._fetch_candles` now queries `instrument_provider_mapping` first |
| available_at_ms on futures_candle | MISSING | **ADDED** | Migration 20260917_100000; ORM model updated |
| available_at_ms on options_candle | MISSING | **ADDED** | Same migration |
| 3m blocked in CandleBuilder | N/A | **ENFORCED** | ValueError on construction with interval="3m" |
| OI NULL never corrupted to 0 | UNTESTED | **VERIFIED** | reconcile_oi() returns canonical_oi=None; test PASS |
| Look-ahead validation | NOT IMPLEMENTED | **IMPLEMENTED** | CandleBuilder.validate_point_in_time(); candle_time_ms <= available_at_ms enforced |
| Pilot execution script | ABSENT | **CREATED** | `scripts/run_fno_pilot.py` with all 7 validation gates |
| Yahoo NOT in F&O fallback chain | NOT ENFORCED | **ENFORCED** | ProviderGateway._CAPABILITY_ROUTING verified (Drill 3 PASS) |

### Remaining blockers (pilot execution BLOCKED)

| Blocker | Status |
|---------|--------|
| Angel One credentials not configured in current environment | BLOCKED_BY_ENVIRONMENT |
| Angel One historical OI: getOIData "Invalid Bad Request" | BLOCKED_BY_PROVIDER_PLAN |
| F&O token resolution from instrument_provider_mapping requires live DB with populated tokens | BLOCKED — need instrument sync |
| Upstox OAuth access token expired | BLOCKED_BY_EXTERNAL |

### Pilot execution procedure (when credentials available)

1. Configure `ANGEL_ONE_API_KEY`, `ANGEL_ONE_CLIENT_ID`, `ANGEL_ONE_TOTP_SECRET`, `ANGEL_ONE_MPIN`
2. Run `POST /v1/admin/instruments/sync` to populate instrument_provider_mapping with F&O tokens
3. Verify: `SELECT COUNT(*) FROM instrument_provider_mapping WHERE provider='angel_one' AND canonical_instrument_id LIKE 'NFO:%FUT%'`
4. Run: `python3 scripts/run_fno_pilot.py`
5. All 6 validation gates must PASS before full 1-year backfill is authorized
6. For OI: Angel One plan restriction → OI will be NULL (not zero) for historical candles; Upstox V3 provides OI at index 6 for 1d candles

**GATE STATUS: BLOCKED — pilot not yet executable without live credentials**
