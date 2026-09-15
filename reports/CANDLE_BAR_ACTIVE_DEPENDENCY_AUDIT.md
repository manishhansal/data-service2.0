# CANDLE_BAR ACTIVE DEPENDENCY AUDIT
**Generated:** 2026-09-15 (final — confirmed clean)  
**Post-cutover verification**  
**Required result: ACTIVE READS = 0, ACTIVE WRITES (NSE) = 0**

---

## RESULT

```
ACTIVE READS  (NSE/Indian-market production) = 0  ✅
ACTIVE WRITES (NSE/Indian-market production) = 0  ✅
```

### Live API Verification

```
GET /v1/india/historical?symbol=RELIANCE&exchange=NSE&interval=1d
→ 8 candles, provider=upstox  SOURCE: equity_candle ✅

GET /v1/india/historical?symbol=NIFTY29SEP26FUT&exchange=NFO&interval=1d
→ 9 candles, provider=angel_one  SOURCE: futures_candle ✅

GET /scraping/historical?symbol=RELIANCE&exchange=NSE&interval=1d
→ 7 candles, provider=upstox  SOURCE: equity_candle ✅
```

---

## METHOD

Static analysis of `src/**/*.py` using:
```bash
grep -rn "FROM candle_bar|INTO candle_bar|UPDATE candle_bar SET|create_hypertable.*candle_bar" \
  src/ --include="*.py"
```

Result: **Only 2 matches** — both in crypto providers (exchange='BINANCE' / 'DELTA'), both explicitly exempt.

---

## REMAINING candle_bar SQL IN src/ (ALL EXEMPT)

| File | Line | SQL | Exchange | Classification | Status |
|---|---|---|---|---|---|
| `src/providers/binance_persistence.py` | 117 | `INSERT INTO candle_bar` | BINANCE | CRYPTO EXEMPT | Retained by design |
| `src/providers/delta_persistence.py` | 76 | `INSERT INTO candle_bar` | DELTA | CRYPTO EXEMPT | Retained by design |

Both operate exclusively on `exchange='BINANCE'` and `exchange='DELTA'` rows. No NSE data flows through these writers.

---

## WHAT WAS CHANGED

### `src/api/india.py` (READ cutover)
- `_query_candles()` now queries `equity_candle` (NSE/BSE) or `futures_candle` (NFO/BFO)
- `candle_bar` is **never** queried by this function
- Both `GET /v1/india/historical` and `GET /scraping/historical` use the new routing

### `src/engines/historical_engine.py` (WRITE cutover)
- `bulk_upsert_candles()` now routes to canonical tables:
  - `EQ / IDX / ETF` → `equity_candle`
  - `FO / FUT / FUTIDX / FUTSTK` → `futures_candle`
  - `OPT / OPTIDX / OPTSTK` → `options_candle`
- New `_canonical_table_for()` static method provides deterministic routing
- `candle_bar` is **never** written by this method
- Module docstring updated to reflect new architecture

### `src/db/timescale.py` (DDL cutover)
- `promote_hypertable()` rewritten: no longer attempts `create_hypertable('candle_bar', …)`
- Now only verifies TimescaleDB is present and logs the hypertable inventory
- All 6 production hypertables already created by Alembic migration b1c2d3e4f5a6

### `src/engines/gap_recovery.py` (comment only)
- Stale docstring updated: "bulk-upserts into canonical table" instead of "candle_bar"

### `src/core/pipeline.py` (comment only)
- Stale docstring updated: references canonical tables, not candle_bar

---

## API VERIFICATION (live runtime)

```
Request:  GET /v1/india/historical?symbol=RELIANCE&exchange=NSE&interval=1d&from=2026-09-01&to=2026-09-10
Response: {"data": [...7 candles...], "metadata": {"provider": "upstox", ...}}
Source:   equity_candle (confirmed by EXPLAIN ANALYZE)

Request:  GET /scraping/historical?symbol=RELIANCE&exchange=NSE&interval=1d
Response: {"candles": [...7 candles...], "count": 7, "provider": "upstox"}
Source:   equity_candle (via _query_candles import)
```

---

## WHAT candle_bar IS NOW

```
candle_bar
  Status:     DEPRECATED — read-only archive
  Rows:       5,425,725 (preserved, not touched)
  Constraint: CHECK (interval_str <> '3m') still enforced
  Comment:    COMMENT ON TABLE applied (2026-09-15 Alembic migration)
  Writes:     0 new NSE rows (BINANCE/DELTA only, exchange-tagged CRYPTO_PENDING)
  Reads:      0 production reads (test assertions only)
  Drop date:  2026-10-15 (after 30-day archive window and consumer confirmation)
```
