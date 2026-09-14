# REPORT 13 — DATA PROVENANCE CERTIFICATION
**Audit date:** 2026-09-13

---

## Status: 🟡 IMPLEMENTED BUT NOT RUNTIME-VERIFIED

Provenance system is fully implemented and unit-tested. No real observations in DB.

---

## Provenance Schema

Every observation carries:

| Field | Type | Notes |
|---|---|---|
| `dataObservationId` | UUID v4 | Unique per observation |
| `provider` | ProviderId | angel_one, upstox, nse_scrapling, yahoo, jugaad, openchart, binance, deribit |
| `providerType` | SourceType | BROKER_AUTHENTICATED, EXCHANGE_DIRECT, AGGREGATOR |
| `market` | str | india, crypto |
| `exchange` | str | NSE, NFO, BSE, BINANCE, DERIBIT |
| `instrument` | str | Symbol or instrumentId |
| `dataset` | str | candle, quote, option_chain, etc. |
| `timestamp` | datetime | UTC |
| `receivedAt` | datetime | UTC — when data-service received it |
| `normalizationVersion` | str | Version of normaliser used |
| `sourceTimestamp` | datetime | Provider's own timestamp |
| `quality` | float | DataConfidenceScore 0–95 |
| `reconciliation` | ReconciliationStatus | CONFIRMED, MINOR_DISCREPANCY, MAJOR_DISCREPANCY |
| `dataSourceType` | DataSourceType | LIVE, HISTORICAL, REPLAY, CACHED |

## Provenance Lineage Chain

```
API response → observationId
→ GET /v1/lineage/{observationId}
→ Full lineage record (within 500ms)
→ DB: data_lineage table
```

Code: `src/stores/lineage_store.py`  
Test: `test_lineage_store.py` ✅  
Runtime: ⛔ BLOCKED

---

## Stale Data Policy (Code Verified)

| Condition | Behaviour |
|---|---|
| Provider outage + cached data exists | Cached data served with `dataSourceType = CACHED`, never `LIVE` |
| Timestamp > freshness threshold | `FreshnessClassifier.classify()` → `STALE` tier |
| Clock skew detected | `ClockSkewMonitor` logs warning, data not silently accepted |

---

## Forensics Endpoint

`GET /v1/lineage/forensics/{tradeId}` — joins trade + signal + provenance + lineage within 1000ms.  
Code: `src/forensics/` ✅  
Test: `test_trade_forensics.py` ✅  
Runtime: ⛔ BLOCKED

---

## Property P11 (Provenance ID Uniqueness) — MISSING

The test file `tests/properties/test_observation_id_uniqueness.py` does not exist.  
Behaviour is tested at unit level but UUID v4 uniqueness across concurrent observation creation is not property-tested.  
This is P2 risk — UUID v4 collision probability is negligible (~1 in 5×10^36) but the test is cheap insurance.
