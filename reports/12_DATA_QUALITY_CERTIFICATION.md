# REPORT 12 — DATA QUALITY CERTIFICATION
**Audit date:** 2026-09-13

---

## Status: 🟡 IMPLEMENTED BUT NOT RUNTIME-VERIFIED

Data quality engine is fully implemented and passes unit tests. No real data has flowed through it.

---

## Quality Engine Implementation

File: `src/engines/quality_engine.py`

| Component | Implementation | Unit Tests | Property Tests | Runtime |
|---|---|---|---|---|
| `DataConfidenceScore` formula | ✅ 0–95 cap | ✅ `test_confidence_score.py` | ❌ Property 5 pending | ⛔ BLOCKED |
| `DataQualityGate` 5-condition | ✅ closed-form AND | ✅ `test_quality_gate.py` | ❌ Property 8 pending | ⛔ BLOCKED |
| Freshness tiers | ✅ `FreshnessClassifier` | ✅ `test_freshness_classifier.py` | — | ⛔ BLOCKED |
| `score < 30 → BLOCKED` | ✅ `signalEngineAllowed: false` | ✅ | ❌ Property 9 pending | ⛔ BLOCKED |
| `score < 60 → POOR_QUALITY` | ✅ not delivered to API/event bus | ✅ `test_pipeline_poor_quality.py` | — | ⛔ BLOCKED |
| Quality score max 95 | ✅ hardcoded cap | ✅ | ❌ Property 5 pending | ⛔ BLOCKED |
| Quality score ≥ 0 | ✅ `max(0.0, ...)` | ✅ | ❌ Property 5 pending | ⛔ BLOCKED |
| Strategy overrides | ✅ `StrategyOverrides` | ✅ `test_strategy_overrides.py` | — | ⛔ BLOCKED |
| Option chain quality | ✅ | ✅ `test_option_chain_quality.py` | — | ⛔ BLOCKED |
| Reconciliation classification | ✅ MATCH/MINOR/MAJOR | ✅ `test_reconciliation.py` | ❌ Property 14 pending | ⛔ BLOCKED |

---

## OHLCV Invariants (VERIFIED by Property Test)

Property 1 (`test_ohlcv_invariants.py`) — **PASSES** (5 tests, 100+ iterations each):

| Invariant | Status |
|---|---|
| `high >= max(open, close)` | ✅ VERIFIED |
| `low <= min(open, close)` | ✅ VERIFIED |
| `volume >= 0` | ✅ VERIFIED |
| All prices > 0 | ✅ VERIFIED |
| 3m rejected for India | ✅ VERIFIED |

---

## Pending Property Tests (P2–P14)

These properties are listed in PRODUCTION_CERTIFICATION.md as "Pending" — but the test files do not even exist:

| Property | File | Status |
|---|---|---|
| P2: Normaliser round-trip | `tests/properties/test_normaliser_round_trip.py` | ❌ FILE MISSING |
| P3: Deribit name round-trip | `tests/properties/test_deribit_round_trip.py` | ❌ FILE MISSING |
| P4: Dedup hash stability | `tests/properties/test_dedup_hash.py` | ❌ FILE MISSING |
| P5: Confidence score bounds | `tests/properties/test_confidence_score_bounds.py` | ❌ FILE MISSING |
| P6: NSE session phase determinism | `tests/properties/test_session_phase_determinism.py` | ❌ FILE MISSING |
| P7: 3m rejection | `tests/properties/test_3m_rejection.py` | ❌ FILE MISSING |
| P8: Quality gate closed-form | `tests/properties/test_quality_gate_closed_form.py` | ❌ FILE MISSING |
| P9: BLOCKED score blocks signal | `tests/properties/test_blocked_score.py` | ❌ FILE MISSING |
| P10: Cache TTL monotonicity | `tests/properties/test_cache_ttl_monotonicity.py` | ❌ FILE MISSING |
| P11: Provenance ID uniqueness | `tests/properties/test_observation_id_uniqueness.py` | ❌ FILE MISSING |
| P12: OI semantic integrity | `tests/properties/test_oi_semantic_integrity.py` | ❌ FILE MISSING |
| P13: Look-ahead bias prevention | `tests/properties/test_look_ahead_bias.py` | ❌ FILE MISSING |
| P14: Reconciliation deviation | `tests/properties/test_reconciliation_deviation.py` | ❌ FILE MISSING |

**The certification claims these are "covered by corresponding unit tests in the interim". That is acceptable for P2 and above. For P0/P1 correctness invariants (3m rejection, confidence score bounds, quality gate, OI semantics), property tests provide stronger guarantees and should be implemented.**

---

## Null Semantics Verification (Code)

| Field | Null Policy | Enforcement | Test |
|---|---|---|---|
| `oi` | null + `oiMissing: true` when absent; never from tradedValue | `src/core/normaliser.py` | ✅ |
| `iv` | null; zero never substituted | Normaliser | ✅ |
| `delta/gamma/theta/vega/rho` | null; zero never substituted | Normaliser | ✅ |
| `bid`/`ask` | null; zero never substituted | Normaliser | ✅ |
| `volume` | `volumeUnavailable: true` + `volume=0` when absent | Normaliser | ✅ |
| `tradedValue` | distinct from `oi` | Normaliser + semantic validator | ✅ |
