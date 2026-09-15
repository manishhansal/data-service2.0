# HISTORICAL PROVENANCE AUDIT
**Generated:** 2026-09-15  
**Scope:** All 5,425,719 rows in equity_candle (migrated from candle_bar)  
**Status:** PROVENANCE LIMITATIONS DOCUMENTED — NO FABRICATION

---

## 1. PROVENANCE AVAILABILITY SUMMARY

| Field | Available | Coverage |
|---|---|---|
| `provider` | ✅ YES | 100% — all rows have provider name |
| `source_type` | ✅ YES | 100% — BROKER_AUTHENTICATED or OPEN_SOURCE_NSE_DERIVED |
| `normalisation_version` | ✅ YES | 99.999% (5,425,715 = '2.0.0', 10 = '1') |
| `dataset_version` | ✅ YES | 100% — all rows have dataset_version = 1 |
| `received_at` | ✅ YES | 100% — all rows have ingestion wall-clock time |
| `data_origin` | ✅ YES | 100% — all rows = 'PROVIDER' |
| `quality_status` | ✅ YES | 100% — all rows = 'TRUSTED' |
| `source_timestamp` | ❌ NO | 0% — provider did not return per-candle source timestamp |
| `provenance_id` | ❌ NO | 0% — pre-provenance-era data; data_provenance table was empty when ingested |

---

## 2. WHAT IS KNOWN FOR EACH ROW

Every row in `equity_candle` has these confirmed provenance fields:

### Provider Identity
```
provider        — who provided the data
  angel_one     — 5,410,381 rows (Angel One SmartAPI, TOTP+JWT authenticated)
  upstox        — 13,480 rows   (Upstox V2 OAuth2 authenticated)
  yahoo_finance — 1,830 rows    (Yahoo Finance, public endpoint)
  jugaad_data   — 16 rows       (NSE bhavcopy, open source)
  openchart     — 8 rows        (NSE charting, open source)

source_type     — authentication classification
  BROKER_AUTHENTICATED    — 5,423,861 rows (angel_one + upstox)
  OPEN_SOURCE_NSE_DERIVED —     1,858 rows (yahoo_finance + jugaad + openchart)
```

### Ingestion Identity
```
received_at           — wall-clock UTC time when our system ingested the candle
  All rows: 2026-09-13 to 2026-09-15 (the backfill date range)

normalisation_version — which normaliser version processed the data
  '2.0.0': 5,425,715 rows  (production normaliser)
  '1':          10 rows     (seed/test data from 2024-01-15)

data_origin = 'PROVIDER'  — data came directly from provider, not derived
```

### Candle Identity
```
instrument_id — canonical NSE instrument ID (e.g. 'NSE:RELIANCE')
exchange      — 'NSE' for all Indian equity rows
interval_str  — confirmed candle interval (1m, 5m, 10m, 15m, 30m, 1h, 1d, 1w, 1M)
time          — UTC candle open timestamp
session_date  — IST trading date
```

---

## 3. WHAT IS NOT AVAILABLE (AND WHY)

### 3.1 source_timestamp (NULL for all rows)

**Reason:** Angel One SmartAPI's `getCandleData` endpoint does not return a per-candle source timestamp in its response. The response contains only OHLCV values and the candle open time. There is no "exchange timestamp" returned by the provider.

**Disposition:** `NULL` preserved as-is. This is accurate — no timestamp was fabricated.

### 3.2 provenance_id (NULL for all rows)

**Reason:** The `data_provenance` table was empty at the time these candles were ingested. The provenance middleware (designed to record a `data_provenance` row per fetch operation) was not active during the backfill period (2026-09-13 to 2026-09-15).

**Disposition:** `provenance_id = NULL` preserved. This is accurate — no fake provenance IDs were created.

### 3.3 Request-level provenance (not reconstructable)

The backfill jobs that fetched these candles did not log enough metadata to reconstruct individual fetch operations. Specifically, the following are NOT available:
- Original HTTP request ID (Angel One / Upstox)
- Response hash (SHA-256 of raw API response)  
- Exact fetch timestamp per chunk
- Provider-side "as of" timestamp

**Disposition:** These are marked `HISTORICAL_UNAVAILABLE`. Forward-looking new ingestion will have full provenance via `data_provenance` records.

---

## 4. PROVENANCE STATUS BY PROVIDER

| Provider | Rows | source_timestamp | provenance_id | received_at | data_origin | quality_status |
|---|---|---|---|---|---|---|
| angel_one | 5,410,381 | NULL | NULL | ✅ | PROVIDER | TRUSTED |
| upstox | 13,480 | NULL | NULL | ✅ | PROVIDER | TRUSTED |
| yahoo_finance | 1,830 | NULL | NULL | ✅ | PROVIDER | TRUSTED |
| jugaad_data | 16 | NULL | NULL | ✅ | PROVIDER | TRUSTED |
| openchart | 8 | NULL | NULL | ✅ | PROVIDER | TRUSTED |

---

## 5. MINIMUM VIABLE PROVENANCE ASSESSMENT

Despite the limitations, the available provenance is sufficient for:

| Use Case | Sufficient? | Evidence |
|---|---|---|
| Identifying which provider sourced each candle | ✅ YES | `provider` field |
| Determining if data is broker-authenticated | ✅ YES | `source_type` field |
| Knowing when data was ingested | ✅ YES | `received_at` field |
| Knowing data is not derived | ✅ YES | `data_origin = 'PROVIDER'` |
| ML dataset reproducibility | ⚠️ PARTIAL | Provider + date + interval known; exact fetch not reconstructable |
| Forensic replay of individual fetches | ❌ NO | No request IDs or response hashes |
| Cross-provider reconciliation dating | ⚠️ PARTIAL | Time + provider known; original request metadata not available |

---

## 6. PROVENANCE FOR FUTURE DATA

Starting from the next ingestion run, all new data will have:

1. **Full `data_provenance` record** per fetch operation (observation_id, response_hash, fetch_at, data_as_of)
2. **`provenance_id` FK** linking each candle to its provenance record
3. **`ingestion_job` record** tracking which job inserted it
4. **`ingestion_checkpoint` record** tracking resume state

---

## 7. PROVENANCE CLASSIFICATION

```
provenance_status = HISTORICAL_UNAVAILABLE

Meaning: The historical candles have partial provenance (provider, source_type,
         received_at, normalisation_version, data_origin) but lack request-level
         provenance (provenance_id, source_timestamp, response_hash).

This is accurately documented. No provenance has been fabricated.
```

---

## 8. VERDICT

| Check | Status |
|---|---|
| All rows have provider identity | ✅ |
| All rows have source_type | ✅ |
| All rows have received_at | ✅ |
| All rows have data_origin = PROVIDER | ✅ |
| source_timestamp present | ❌ (provider doesn't return it) |
| provenance_id present | ❌ (pre-provenance-era data) |
| Fake timestamps created | ❌ NONE |
| Fake provenance IDs created | ❌ NONE |
| Limitations documented | ✅ |

**PROVENANCE AUDIT: LIMITATIONS DOCUMENTED, NO FABRICATION ✅**
