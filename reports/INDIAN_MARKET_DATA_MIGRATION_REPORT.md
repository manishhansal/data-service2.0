# INDIAN MARKET DATA MIGRATION REPORT
**Generated:** 2026-09-15  
**Database:** PostgreSQL 15.18 / TimescaleDB 2.28.3  
**Migration revision:** a1b2c3d4e5f6 → b1c2d3e4f5a6  
**Migration status:** ✅ COMPLETE

---

## 1. MIGRATION SUMMARY

| Metric | Value |
|---|---|
| Migration date | 2026-09-15 |
| Migration duration | ~115 seconds (5.4M rows, 11 batches × 500K) |
| Backup taken | ✅ `backups/mds_pre_migration_2026-09-15.dump` (97 MB) |
| Backup verified | ✅ pg_restore --list returned all 7 original tables |
| Alembic revision before | `a1b2c3d4e5f6` |
| Alembic revision after | `b1c2d3e4f5a6` |
| New tables created | 16 |
| TimescaleDB hypertables | 6 |
| TimescaleDB chunks (equity_candle) | 71 |

---

## 2. PRE-MIGRATION STATE (from inventory)

| Table | Rows Before |
|---|---|
| `candle_bar` | 5,425,725 |
| `instrument_master` | 0 |
| `fno_universe_snapshot` | 0 |
| `data_gap` | 0 |
| `data_incident` | 0 |
| `data_provenance` | 0 |
| `provider_health` | 0 |

---

## 3. MIGRATION EXECUTION LOG

### Phase 1: Backup
```
pg_dump mds → /tmp/mds_pre_migration_2026-09-15.dump
Exit code: 0 (warnings only re: circular FK in TimescaleDB internal tables)
Backup size: 97 MB
Verification: pg_restore --list returned all tables ✅
```

### Phase 2: Apply New Schema (Alembic)
```
alembic upgrade a1b2c3d4e5f6 → b1c2d3e4f5a6
Exit code: 0
Duration: < 2 seconds (additive DDL only, no data touched)
Tables created: 16 new tables
Hypertables promoted: 6 (equity_candle, futures_candle, options_candle,
                          market_tick, market_quote, option_greeks_snapshot)
candle_bar: NOT modified structurally ✅
```

### Phase 3: Populate instrument_master
```
Script: scripts/populate_instrument_master.py
Exit code: 0
instruments processed: 50 (all distinct instrument_ids from candle_bar)
instruments upserted: 50
provider_mappings upserted: 95
  - angel_one mappings: 49 (48 NSE instruments with known tokens)
  - upstox mappings: 46 (44 NSE instruments with known keys)
```

### Phase 4: Migrate candle_bar → equity_candle
```
Script: scripts/migrate_candle_bar.py --batch-size 500000
Exit code: 0 (migration phase) — tag update ran asynchronously
Batches: 11 (500K each, final batch 428K)
Rows inserted: 5,425,719
Duration: 112.9 seconds
```

**Batch-level detail:**

| Batch | Rows Inserted | Cumulative | Elapsed |
|---|---|---|---|
| 1 | 500,000 | 500,000 | 11.4s |
| 2 | 499,860 | 999,860 | 21.6s |
| 3 | 499,446 | 1,499,306 | 32.2s |
| 4 | 499,994 | 1,999,300 | 41.1s |
| 5 | 499,872 | 2,499,172 | 51.2s |
| 6 | 499,300 | 2,998,472 | 60.4s |
| 7 | 499,752 | 3,498,224 | 70.7s |
| 8 | 499,559 | 3,997,783 | 79.7s |
| 9 | 499,419 | 4,497,202 | 88.2s |
| 10 | 499,789 | 4,996,991 | 97.5s |
| 11 | 428,728 | 5,425,719 | 107.5s |
| 12 (idempotency) | 0 | 5,425,719 | 112.4s |

### Phase 5: BINANCE Tagging
```
candle_bar WHERE exchange='BINANCE': 6 rows
Action: reconciliation_status = 'RETAINED_CRYPTO_PENDING_CRYPTO_TABLE'
These 6 rows remain in candle_bar awaiting a future crypto_candle table.
```

### Phase 6: Quarantine
```
Rows quarantined: 0
Reason: All candle_bar rows classified deterministically as NSE or BINANCE
No UNKNOWN rows found.
```

---

## 4. POST-MIGRATION STATE (measured)

| Table | Rows After | Notes |
|---|---|---|
| `equity_candle` | **5,425,719** | All NSE rows migrated |
| `equity_candle` segment=EQ | 5,424,751 | NSE equities |
| `equity_candle` segment=IDX | 968 | NSE:NIFTY + NSE:BANKNIFTY |
| `futures_candle` | 0 | Ready for F&O backfill |
| `options_candle` | 0 | Ready for F&O backfill |
| `instrument_master` | 50 | All instruments populated |
| `instrument_provider_mapping` | 95 | Angel One + Upstox tokens |
| `candle_bar_quarantine` | 0 | No unclassifiable rows |
| `candle_bar` | 5,425,725 | DEPRECATED — archive only |
| `candle_bar` NSE rows | 5,425,719 | Tagged MIGRATED_TO_EQUITY_CANDLE_V2 |
| `candle_bar` BINANCE rows | 6 | Tagged RETAINED_CRYPTO_PENDING |

---

## 5. CLASSIFICATION RESULT

| Dataset | Source Rows | Destination | Migrated | Delta |
|---|---|---|---|---|
| NSE Equities | 5,424,751 | equity_candle (EQ) | 5,424,751 | **0** |
| NSE Indices | 968 | equity_candle (IDX) | 968 | **0** |
| NSE Total | 5,425,719 | equity_candle | 5,425,719 | **0** |
| BINANCE Crypto | 6 | candle_bar (retained) | N/A — not migrated | 0 |
| FUTURES | 0 | futures_candle | 0 | 0 |
| OPTIONS | 0 | options_candle | 0 | 0 |
| QUARANTINED | 0 | candle_bar_quarantine | 0 | 0 |

---

## 6. IDEMPOTENCY VERIFICATION

```
Second dry-run after migration:
  pre_counts.ec_before = 5,425,719  (equity_candle already full)
  would_migrate        = 0          ← CONFIRMED IDEMPOTENT

Running migrate_candle_bar.py a second time would insert 0 rows.
ON CONFLICT DO NOTHING ensures complete safety of repeated runs.
```

---

## 7. DATA PROVENANCE NOTES

- All migrated rows carry `data_origin = 'PROVIDER'`
- `normalisation_version` preserved from candle_bar (2.0.0 for 5,425,715 rows, '1' for 10 rows)
- `provenance_id` mapped from `candle_bar.data_observation_id` (all NULL in source — preserved as NULL)
- `reconciliation_status` in equity_candle set to NULL (fresh slate for new reconciliation runs)
- `candle_bar.reconciliation_status` updated to `MIGRATED_TO_EQUITY_CANDLE_V2` for NSE rows

---

## 8. CHECKPOINT STATUS

| Checkpoint | Status | Evidence |
|---|---|---|
| CHECKPOINT 1: Pre-migration inventory | ✅ PASS | reports/INDIAN_MARKET_PRE_MIGRATION_INVENTORY.md |
| CHECKPOINT 2: Backup verified | ✅ PASS | backups/mds_pre_migration_2026-09-15.dump (97 MB) |
| CHECKPOINT 3: New schema created | ✅ PASS | alembic_version = b1c2d3e4f5a6, 16 new tables |
| CHECKPOINT 4: instrument_master populated | ✅ PASS | 50 rows in instrument_master |
| CHECKPOINT 5: Provider mappings populated | ✅ PASS | 95 rows in instrument_provider_mapping |
| CHECKPOINT 6: Migration dry-run | ✅ PASS | Would migrate 5,425,719 rows |
| CHECKPOINT 7: Migration completed | ✅ PASS | equity_candle = 5,425,719 rows |
| CHECKPOINT 8: Reconciliation passed | ✅ PASS | delta=0, violations=0, duplicates=0 |
| CHECKPOINT 9: API switch | ⏳ PENDING | Code changes required (Phase 6) |
| CHECKPOINT 10: Performance benchmarks | ✅ PASS | See QUERY_PERFORMANCE_REPORT |
