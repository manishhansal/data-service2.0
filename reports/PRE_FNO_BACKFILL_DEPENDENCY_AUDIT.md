# PRE-F&O BACKFILL DEPENDENCY AUDIT
**Generated:** 2026-09-15  
**Scope:** All `candle_bar` references in src/, scripts/, tests/  
**Purpose:** Establish baseline before cutover; verify zero active production dependency after cutover

---

## SUMMARY

| Category | Before Cutover | After Cutover |
|---|---|---|
| Active NSE/Indian-market READs from candle_bar | 2 | **0** |
| Active NSE/Indian-market WRITEs to candle_bar | 1 | **0** |
| Crypto WRITEs (BINANCE/DELTA — exempt) | 2 | 2 (unchanged, by design) |
| Startup DDL on candle_bar | 1 | **0** |
| Migration references | 3 | 3 (unchanged, run-once scripts) |
| Test references | 4 | 4 (unchanged, test-only) |
| Documentation/comment references | ~15 | ~15 |

---

## PART 1 — ACTIVE PRODUCTION REFERENCES (pre-cutover)

### READ-1: `src/api/india.py` — `_query_candles()`

```
File:    src/api/india.py
Lines:   1315–1430 (before cutover)
Type:    ACTIVE READ — production SELECT
SQL:     SELECT … FROM candle_bar WHERE instrument_id=… AND exchange=… AND interval_str=…
Called by:
  - GET /v1/india/historical (every historical candle request)
  - GET /scraping/historical (via compat.py import)
```

**STATUS AFTER CUTOVER: RESOLVED** — `_query_candles()` now selects from `equity_candle` (NSE) or `futures_candle` (NFO). No `candle_bar` SQL present.

---

### READ-2 (indirect): `src/api/compat.py` — `compat_historical()`

```
File:    src/api/compat.py
Lines:   170, 185, 197
Type:    INDIRECT ACTIVE READ — via _query_candles import
SQL:     None direct; calls _query_candles() and hist_engine.run_backfill()
Called by:
  - GET /scraping/historical (AlphaForge ScraplingProvider compatibility route)
```

**STATUS AFTER CUTOVER: RESOLVED** — `_query_candles` now reads `equity_candle`. `run_backfill()` now writes to canonical tables.

---

### WRITE-1: `src/engines/historical_engine.py` — `bulk_upsert_candles()`

```
File:    src/engines/historical_engine.py
Lines:   1163 (before cutover)
Type:    ACTIVE WRITE — production INSERT (every NSE backfill job)
SQL:     INSERT INTO candle_bar (…) ON CONFLICT … DO UPDATE …
Called by:
  - run_backfill() for ALL NSE/Indian-market instrument classes
  - Gap recovery (via HistoricalEngine)
  - compat_historical() on cache miss
```

**STATUS AFTER CUTOVER: RESOLVED** — `bulk_upsert_candles()` now routes to:
- `EQ / IDX / ETF` → `equity_candle`
- `FO / FUT` → `futures_candle`
- `OPT / OPTIDX / OPTSTK` → `options_candle`
- New `_canonical_table_for()` static method handles routing.

---

### DDL-1: `src/db/timescale.py` — `promote_hypertable()`

```
File:    src/db/timescale.py
Lines:   54 (before cutover)
Type:    STARTUP DDL — create_hypertable('candle_bar', 'time', …)
Called by:
  - Application startup lifespan
```

**STATUS AFTER CUTOVER: RESOLVED** — `promote_hypertable()` rewritten to only verify TimescaleDB presence and log hypertable inventory. No `create_hypertable('candle_bar', …)` call. All production hypertables (equity_candle, futures_candle, etc.) are managed by Alembic migration b1c2d3e4f5a6.

---

## PART 2 — INTENTIONALLY RETAINED WRITES (CRYPTO/DELTA — EXEMPT)

These two writers continue to write to `candle_bar` **by design** until a dedicated `crypto_candle` table is created. They operate on `exchange='BINANCE'` and `exchange='DELTA'` rows exclusively and do not affect any Indian-market data path.

### WRITE-EXEMPT-1: `src/providers/binance_persistence.py`

```
File:    src/providers/binance_persistence.py
Lines:   117
Type:    ACTIVE WRITE (CRYPTO — EXEMPT)
SQL:     INSERT INTO candle_bar (…) exchange='BINANCE' …
Rationale: BINANCE rows tagged RETAINED_CRYPTO_PENDING_CRYPTO_TABLE.
           No impact on NSE/Indian-market canonical tables.
```

**STATUS: INTENTIONALLY RETAINED** — Crypto writes to `candle_bar` pending `crypto_candle` table.

---

### WRITE-EXEMPT-2: `src/providers/delta_persistence.py`

```
File:    src/providers/delta_persistence.py
Lines:   76
Type:    ACTIVE WRITE (DELTA EXCHANGE — EXEMPT)
SQL:     INSERT INTO candle_bar (…) exchange='DELTA' …
Rationale: Delta Exchange (INR-settled crypto derivatives) uses candle_bar
           until a crypto/delta canonical table exists.
```

**STATUS: INTENTIONALLY RETAINED** — Delta writes to `candle_bar` pending `crypto_candle` / `delta_candle` table.

---

## PART 3 — NON-ACTIVE REFERENCES (MIGRATION / TEST / DOCUMENTATION)

### MIGRATION scripts (run-once, not production runtime)

| File | Reference | Classification |
|---|---|---|
| `scripts/migrate_candle_bar.py` | SELECT FROM, UPDATE SET reconciliation_status | MIGRATION |
| `scripts/populate_instrument_master.py` | SELECT DISTINCT instrument_id FROM candle_bar | ADMIN READ |
| `scripts/backfill_india_1y.py` | docstring: "persist to candle_bar" | STALE DOC |
| `alembic/versions/20240101_000000_initial_schema.py` | CREATE TABLE candle_bar | MIGRATION DDL |
| `alembic/versions/20260915_000000_v2_production_schema.py` | COMMENT ON TABLE (deprecation) | MIGRATION DDL |
| `scripts/promote_timescaledb.sql` | create_hypertable('candle_bar', ...) | MIGRATION DDL |

### TEST files

| File | Reference | Classification |
|---|---|---|
| `tests/test_v2_schema_migration.py` | SELECT COUNT(*) FROM candle_bar, schema assertions | TEST |
| `tests/unit/db/test_timescale.py` | assert "candle_bar" in sql_text | TEST |
| `scripts/test_binance_persistence.py` | INSERT + SELECT on candle_bar | TEST SCRIPT |

### DOCUMENTATION / STALE COMMENTS (no SQL execution)

| File | Line(s) | Classification |
|---|---|---|
| `src/engines/historical_engine.py` | 38, 1092 | DOCUMENTATION (explains exemption) |
| `src/api/india.py` | 811 | DOCUMENTATION ("NOT read by this endpoint") |
| `src/db/timescale.py` | 74, 79 | DOCUMENTATION (explains why not promoted) |
| `src/engines/gap_recovery.py` | 600 | DOCUMENTATION (updated) |
| `src/core/pipeline.py` | 615 | DOCUMENTATION (updated) |
| `src/db/models/operations.py` | 216 | COLUMN COMMENT on candle_bar_quarantine |
| `reports/*.md` | various | DOCUMENTATION |

---

## PART 4 — VERIFICATION EVIDENCE

### API test (live)

```
GET /v1/india/historical?symbol=RELIANCE&exchange=NSE&interval=1d&from=2026-09-01&to=2026-09-10
→ candles=7  provider=upstox  (source: equity_candle confirmed)

GET /scraping/historical?symbol=RELIANCE&exchange=NSE&interval=1d&from=2026-09-01&to=2026-09-10
→ candles=7  provider=upstox  (source: equity_candle confirmed)
```

### Database query confirmation

```sql
-- equity_candle contains expected data:
SELECT COUNT(*) FROM equity_candle;  -- 5,425,719 ✅

-- candle_bar NOT being queried by production code:
-- Zero active SELECT ... FROM candle_bar in src/ (excluding crypto providers)
```

### Static analysis (grep result)

```
grep "FROM candle_bar\|INTO candle_bar\|UPDATE candle_bar SET" src/**/*.py

Result:
  binance_persistence.py:117: INSERT INTO candle_bar (exchange='BINANCE')  ← EXEMPT
  delta_persistence.py:76:    INSERT INTO candle_bar (exchange='DELTA')    ← EXEMPT

No other active SQL references found.
```

---

## FINAL VERDICT

| Check | Result |
|---|---|
| Active NSE/Indian reads from candle_bar | **0** ✅ |
| Active NSE/Indian writes to candle_bar | **0** ✅ |
| API reads equity_candle | **YES** ✅ |
| Historical engine writes canonical tables | **YES** ✅ |
| timescale.py promotes candle_bar | **NO** ✅ |
| Crypto/Delta writes remain (exempt) | 2 (by design) ✅ |

**ACTIVE READS = 0. ACTIVE WRITES (NSE) = 0. CUTOVER COMPLETE.**
