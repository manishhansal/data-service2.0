# INDIAN MARKET SCHEMA MIGRATION PLAN
**Version:** 2.0.0  
**Date:** 2026-09-15  
**From revision:** a1b2c3d4e5f6 (initial_schema)  
**To revision:** b1c2d3e4f5a6 (v2_production_schema)  
**Data source:** 5,425,725 rows in candle_bar  
**Data risk:** LOW — existing tables are NOT dropped; new tables are additive

---

## CHECKPOINT GATE PROTOCOL

Each checkpoint must PASS before the next phase begins.  
A FAIL stops the process — investigate, fix, then re-run from that checkpoint only.

```
CHECKPOINT 1 ✅  Pre-migration inventory verified (reports/INDIAN_MARKET_PRE_MIGRATION_INVENTORY.md)
CHECKPOINT 2     Backup created and verified
CHECKPOINT 3     New schema applied (Alembic migration b1c2d3e4f5a6)
CHECKPOINT 4     instrument_master populated
CHECKPOINT 5     instrument_provider_mapping populated
CHECKPOINT 6     equity_candle migration dry-run (no actual inserts)
CHECKPOINT 7     equity_candle migration completed and reconciled
CHECKPOINT 8     candle_bar deprecation tags applied
CHECKPOINT 9     API consumers switched to new tables
CHECKPOINT 10    Performance benchmarks passed
```

---

## PHASE 1 — BACKUP (CHECKPOINT 2)

### 1.1 Create Database Backup

```bash
# Take a pg_dump backup BEFORE any changes
docker exec data-service-postgres \
  pg_dump -U mds_user -d mds -F c -f /tmp/mds_pre_migration_2026-09-15.dump

# Verify backup integrity
docker exec data-service-postgres \
  pg_restore --list /tmp/mds_pre_migration_2026-09-15.dump | head -20

# Copy backup out of container
docker cp data-service-postgres:/tmp/mds_pre_migration_2026-09-15.dump \
  ./backups/mds_pre_migration_2026-09-15.dump
```

### 1.2 Record Current State

```bash
# Record exact row counts
docker exec data-service-postgres psql -U mds_user -d mds -c "
SELECT 'candle_bar' AS tbl, COUNT(*) FROM candle_bar
UNION ALL SELECT 'instrument_master', COUNT(*) FROM instrument_master;
" > backups/pre_migration_counts_2026-09-15.txt
```

### 1.3 Tag Docker Image

```bash
docker commit data-service-postgres \
  data-service-postgres:pre-migration-2026-09-15
```

**CHECKPOINT 2 PASS CRITERIA:**
- pg_dump completes without error
- pg_restore --list shows all 7 existing tables
- Row counts match pre-migration inventory exactly

---

## PHASE 2 — APPLY NEW SCHEMA (CHECKPOINT 3)

### 2.1 Run Alembic Migration

```bash
cd /Users/manishkumar/Desktop/data-service2.0
APP_ENV=local alembic upgrade head
```

This applies migration `b1c2d3e4f5a6` which:
1. Adds `instrument_class` and `name` columns to `instrument_master`
2. Creates `instrument_provider_mapping`
3. Creates `instrument_identity_history`
4. Creates `equity_candle`
5. Creates `futures_candle`
6. Creates `options_candle`
7. Creates `market_tick`
8. Creates `market_quote`
9. Creates `option_chain_snapshot`
10. Creates `option_chain_contract`
11. Creates `option_greeks_snapshot`
12. Creates `exchange_calendar`
13. Creates `market_session`
14. Creates `fno_universe_membership`
15. Creates `ingestion_job`
16. Creates `ingestion_checkpoint`
17. Creates `candle_bar_quarantine`
18. Promotes `equity_candle`, `futures_candle`, `options_candle`, `market_tick`, `market_quote`, `option_greeks_snapshot` to TimescaleDB hypertables

**CHECKPOINT 3 PASS CRITERIA:**
```sql
SELECT COUNT(*) FROM information_schema.tables 
WHERE table_schema = 'public' AND table_name IN (
  'equity_candle','futures_candle','options_candle',
  'market_tick','market_quote','option_chain_snapshot',
  'option_chain_contract','option_greeks_snapshot',
  'exchange_calendar','market_session','fno_universe_membership',
  'ingestion_job','ingestion_checkpoint','candle_bar_quarantine',
  'instrument_provider_mapping','instrument_identity_history'
);
-- Expected: 16
```

---

## PHASE 3 — POPULATE instrument_master (CHECKPOINT 4)

### 3.1 Insert known instruments from adapter constants

The `instrument_master` is currently empty. The adapter code contains
known tokens in `_ANGEL_ONE_KNOWN_TOKENS` and `_UPSTOX_INSTRUMENT_KEYS`.

Run the population script:
```bash
APP_ENV=local python3 scripts/populate_instrument_master.py
```

This script:
1. Reads all distinct `instrument_id` values from `candle_bar`
2. Classifies each as EQ / IDX / CRYPTO using the known symbol lists
3. Inserts into `instrument_master` with classification
4. Reads Angel One known tokens from adapter constants
5. Reads Upstox instrument keys from adapter constants
6. Inserts into `instrument_provider_mapping`

**CHECKPOINT 4 PASS CRITERIA:**
```sql
SELECT COUNT(*) FROM instrument_master;
-- Expected: >= 50 (all instruments in candle_bar)
```

---

## PHASE 4 — MIGRATE candle_bar → equity_candle (CHECKPOINTS 5–7)

### 4.1 Classification Rules Applied

| instrument_id | instrument_class | Destination Table |
|---|---|---|
| NSE:* WHERE symbol NOT IN (NIFTY, BANKNIFTY) | EQ | equity_candle |
| NSE:NIFTY, NSE:BANKNIFTY | IDX | equity_candle |
| BINANCE:* | CRYPTO | candle_bar (not migrated to equity_candle) |
| Any other pattern | UNKNOWN | candle_bar_quarantine |

### 4.2 Migration SQL (Idempotent)

```sql
-- DRY RUN: Count what will be migrated
SELECT 
  CASE 
    WHEN instrument_id IN ('NSE:NIFTY','NSE:BANKNIFTY') THEN 'INDEX'
    WHEN exchange = 'BINANCE' THEN 'CRYPTO'
    WHEN exchange = 'NSE' THEN 'EQUITY'
    ELSE 'UNKNOWN'
  END AS dest,
  COUNT(*) AS row_count
FROM candle_bar
GROUP BY 1;

-- ACTUAL MIGRATION: NSE equities + indices → equity_candle
INSERT INTO equity_candle (
  instrument_id, exchange, segment, interval_str, time, session_date,
  open, high, low, close, volume, vwap, turnover,
  data_origin, provider, source_type, source_timestamp, received_at,
  normalisation_version, dataset_version, quality_status,
  poor_quality, reconciliation_status, provenance_id, volume_unavailable,
  created_at
)
SELECT
  cb.instrument_id,
  cb.exchange,
  CASE 
    WHEN cb.instrument_id IN ('NSE:NIFTY','NSE:BANKNIFTY') THEN 'IDX'
    ELSE 'EQ'
  END AS segment,
  cb.interval_str,
  cb.time,
  cb.session_date,
  cb.open,
  cb.high,
  cb.low,
  cb.close,
  cb.volume,
  NULL AS vwap,
  NULL AS turnover,
  'PROVIDER' AS data_origin,
  cb.provider,
  cb.source_type,
  cb.source_timestamp,
  cb.received_at,
  cb.normalisation_version,
  cb.dataset_version,
  COALESCE(
    CASE WHEN cb.poor_quality THEN 'POOR_QUALITY' ELSE NULL END,
    'TRUSTED'
  ) AS quality_status,
  cb.poor_quality,
  cb.reconciliation_status,
  cb.data_observation_id AS provenance_id,
  cb.volume_unavailable,
  cb.received_at AS created_at
FROM candle_bar cb
WHERE cb.exchange = 'NSE'
ON CONFLICT (instrument_id, exchange, interval_str, time) 
DO NOTHING;
```

### 4.3 Update candle_bar to mark migrated rows

```sql
UPDATE candle_bar
SET reconciliation_status = 'MIGRATED_TO_EQUITY_CANDLE_V2'
WHERE exchange = 'NSE'
  AND reconciliation_status IS NULL;
```

### 4.4 Quarantine BINANCE rows (not migrated, but tagged)

```sql
-- BINANCE rows stay in candle_bar; they belong to a future crypto_candle table
UPDATE candle_bar
SET reconciliation_status = 'RETAINED_CRYPTO_PENDING_CRYPTO_TABLE'
WHERE exchange = 'BINANCE';
```

**CHECKPOINT 7 PASS CRITERIA:**

```sql
-- New table row count
SELECT COUNT(*) FROM equity_candle;
-- Expected: 5,425,719 (all NSE rows from candle_bar)

-- Verify all NSE rows migrated
SELECT 
  (SELECT COUNT(*) FROM candle_bar WHERE exchange = 'NSE') AS old_nse,
  (SELECT COUNT(*) FROM equity_candle) AS new_equity;
-- old_nse = new_equity (must match exactly)

-- Verify OHLC integrity preserved
SELECT COUNT(*) FROM equity_candle WHERE high < open OR high < close 
                                      OR low > open OR low > close;
-- Expected: 0

-- Verify no duplicates introduced
SELECT instrument_id, exchange, interval_str, time, COUNT(*)
FROM equity_candle
GROUP BY 1,2,3,4 HAVING COUNT(*) > 1 LIMIT 5;
-- Expected: 0 rows
```

---

## PHASE 5 — IDEMPOTENCY TEST (RUN MIGRATION TWICE)

```bash
# Run migration script again
APP_ENV=local python3 scripts/migrate_candle_bar.py

# Expected:
#   inserted_rows = 0 (all already present via ON CONFLICT DO NOTHING)
#   updated_rows = 0
#   duplicate_rows = 5,425,719
#   No corruption
```

---

## PHASE 6 — API SWITCH (CHECKPOINT 9)

Modify the following files to read from `equity_candle` instead of `candle_bar`:

1. `src/engines/historical_engine.py`:
   - `run_backfill()` → write to `equity_candle` (primary) and `candle_bar` (dual-write during transition)
   - Add `read_from_equity_candle()` method

2. `src/api/india.py`:
   - `get_historical_ohlcv()` → read from `equity_candle` WHERE instrument classification = EQ/IDX
   - Preserve backward compatibility via query router

3. `src/api/compat.py`:
   - Route to `equity_candle` after migration

Dual-write period: 7 days minimum before removing `candle_bar` writes.

---

## PHASE 7 — DEPRECATE candle_bar (CHECKPOINT 10)

After all consumers confirmed switched:

```sql
-- Add deprecation comment
COMMENT ON TABLE candle_bar IS 
  'DEPRECATED 2026-09-15: Data migrated to equity_candle. '
  'This table is read-only archive. Do not write new records. '
  'Scheduled for removal after 2026-10-15.';

-- Optional: add read-only trigger
CREATE OR REPLACE RULE candle_bar_no_insert AS
  ON INSERT TO candle_bar DO INSTEAD NOTHING;
```

---

## ROLLBACK PROCEDURE

If any checkpoint FAILS before CHECKPOINT 9 (API switch):

```bash
# Restore from pre-migration backup
docker exec data-service-postgres \
  pg_restore -U mds_user -d mds -c \
  /tmp/mds_pre_migration_2026-09-15.dump

# Roll back Alembic
APP_ENV=local alembic downgrade a1b2c3d4e5f6
```

The existing `candle_bar` is never touched destructively, so rollback is always safe.

---

## ESTIMATED TIMELINE

| Phase | Estimated Duration | Risk |
|---|---|---|
| Phase 1: Backup | 5 min | LOW |
| Phase 2: Apply schema | 2 min | LOW (additive only) |
| Phase 3: Populate instrument_master | 10 min | LOW |
| Phase 4: Migrate candle_bar | 15–30 min (5.4M rows) | MEDIUM (validate carefully) |
| Phase 5: Idempotency test | 5 min | LOW |
| Phase 6: API switch | 2–4 hours (code change + test) | MEDIUM |
| Phase 7: Deprecate candle_bar | 5 min | LOW |
| **Total** | **~4–6 hours** | **LOW–MEDIUM** |
