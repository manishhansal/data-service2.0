# REPORT 09 — DATABASE DATA CERTIFICATION
**Audit date:** 2026-09-13

---

## Status: ⛔ BLOCKED — No Database Running

Docker Compose is not running. `docker compose ps` shows no active containers.  
No PostgreSQL is accessible. Zero database evidence can be gathered.

---

## Schema Verification (Code + Migration Only)

### Alembic Migration

File: `alembic/versions/20240101_000000_initial_schema.py`

| Table | Purpose | TimescaleDB Hypertable | CHECK constraints |
|---|---|---|---|
| `candle_bar` | OHLCV candles | 🟡 (promote_timescaledb.sql required) | `interval_str <> '3m'` ✅ |
| `data_observation` | Live quotes + provenance | 🟡 | — |
| `instrument` | Instrument master | — | — |
| `fno_universe_snapshot` | F&O universe | — | — |
| `data_incident` | Quality/provenance incidents | — | — |
| `data_lineage` | Provenance lineage | — | — |

### DB CHECK Constraint — 3m Ban

```sql
CHECK (interval_str <> '3m')
```

This is the 5th of 6 layers blocking 3m for Indian data. Independently verified in migration file.

---

## Expected Table State (After Proper Startup)

| Table | Expected Rows (after 1 day of data) | Actual Rows | Status |
|---|---|---|---|
| `candle_bar` | ~50,000+ | 0 | ⛔ BLOCKED |
| `data_observation` | ~10,000+ | 0 | ⛔ BLOCKED |
| `instrument` | ~60,000+ (ScripMaster) | 0 | ⛔ BLOCKED |
| `fno_universe_snapshot` | 1+ | 0 | ⛔ BLOCKED |
| `data_lineage` | ~10,000+ | 0 | ⛔ BLOCKED |
| `data_incident` | 0 (clean run) | 0 | ⛔ BLOCKED |

---

## TimescaleDB Promotion

The `candle_bar` table requires TimescaleDB promotion for optimal performance:

```sql
-- scripts/promote_timescaledb.sql
SELECT create_hypertable('candle_bar', 'timestamp', if_not_exists => TRUE);
```

This script exists but has not been run (no DB running). Without TimescaleDB extension, the table works as a standard PG table but with degraded time-series query performance.

---

## Persistence Verification Requirements (Blocked)

The following queries CANNOT be run until infrastructure is started:

```sql
-- Row counts
SELECT COUNT(*) FROM candle_bar;
SELECT COUNT(*) FROM data_observation;

-- Provider distribution
SELECT provider, COUNT(*) FROM candle_bar GROUP BY provider;

-- Quality distribution
SELECT quality_grade, COUNT(*) FROM data_observation GROUP BY quality_grade;

-- Provenance coverage
SELECT COUNT(*) FROM data_observation WHERE observation_id IS NOT NULL;

-- Duplicate check
SELECT instrument_id, interval_str, timestamp, COUNT(*) 
FROM candle_bar 
GROUP BY instrument_id, interval_str, timestamp 
HAVING COUNT(*) > 1;

-- Latest timestamp per provider
SELECT provider, MAX(timestamp) FROM candle_bar GROUP BY provider;
```

---

## Demo/Test Data Contamination

No database is running. No demo/test data contamination can be assessed.  
The codebase does not contain fixtures that auto-insert market data into production DB.  
Test suite uses in-memory mocks (`tests/mocks/provider_mocks.py`) — no real DB writes.

**Expected production result once running:** ZERO fabricated market observations.
