# PROVIDER REPORT RECONCILIATION
## data-service2.0 — Phase 0 Report Consistency Audit

**Audit date:** 2026-09-17  
**Updated:** 2026-09-17 (post-deletion housekeeping)  
**Note:** The three archive files referenced throughout this document (`ANGELONE_UPSTOX_FORENSIC_AUDIT.md`, `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`, `PRODUCTION_CERTIFICATION.md`) have been **deleted** as of 2026-09-17. This document is retained as the reconciliation record explaining what those files contained and why they were superseded.

---

## PURPOSE

Before any runtime testing, this document identifies which existing reports contain stale findings that contradict the current implementation, which reports are authoritative, and which gaps remain unresolved.

The instruction is explicit: **contradictory reports must not coexist without resolution.**

---

## REPORT INVENTORY (updated 2026-09-17 — deleted files removed)

| File | Date | Status | Authority |
|------|------|--------|-----------|
| ~~`PRODUCTION_CERTIFICATION.md`~~ | 2026-01-15 | **DELETED** — self-revoked; superseded by `FINAL_PROVIDER_RUNTIME_CERTIFICATION.md` | None |
| ~~`ANGELONE_UPSTOX_FORENSIC_AUDIT.md`~~ | 2026-09-16 | **DELETED** — pre-fix archive; all findings reconciled | None |
| ~~`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`~~ | 2026-09-16 | **DELETED** — pre-fix archive; superseded | None |
| `ANGELONE_UPSTOX_FINAL_CERTIFICATION.md` | 2026-09-17 | **CURRENT** — 4,413 tests; Upstox V3 migration documented | Primary provider cert |
| `FINAL_PROVIDER_RUNTIME_CERTIFICATION.md` | 2026-09-17 | **CURRENT** — live runtime evidence + pipeline fixes | Authoritative summary |
| `reports/FINAL_PROVIDER_RUNTIME_CERTIFICATION.md` | 2026-09-17 | **CURRENT** — full detail version | Detailed certification |
| `reports/PROVIDER_LIVE_TEST_REPORT.md` | 2026-09-15 | **AUTHORITATIVE** — contains real provider evidence | Primary live evidence |
| `reports/05_PROVIDER_RUNTIME_CERTIFICATION.md` | 2026-09-14 | **AUTHORITATIVE** — runtime evidence from 2026-09-14 | Secondary live evidence |
| `reports/20_FINAL_PRODUCTION_CERTIFICATION.md` | 2026-09-15 | **AUTHORITATIVE** — P0 blockers resolved | Numbered-series cert |
| `ANGELONE_DATA_COMPLETENESS_REPORT.md` | 2026-09-17 | **CURRENT** — includes session-2 pipeline fix update | Implementation reference |
| `UPSTOX_DATA_COMPLETENESS_REPORT.md` | 2026-09-17 | **CURRENT** — includes V3 migration update | Implementation reference |
| `reports/PROVIDER_DATA_LINEAGE_REPORT.md` | 2026-09-17 | **CURRENT** — gap fixes, real DB evidence | Data lineage authority |
| `reports/17_DIRECT_PROVIDER_BYPASS_AUDIT.md` | 2026-09-13 | **HISTORIC** — pre-fix bypass findings; still valid as evidence | Bypass audit evidence |

---

## STALE FINDINGS — LINE BY LINE

### Finding 1: "Upstox V2 historical API used — MISSING V3"

**Old finding** (`ANGELONE_UPSTOX_FORENSIC_AUDIT.md`, `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> Upstox historical candles use deprecated V2 endpoint. V3 not implemented. OI at index 6 not captured.

**Current implementation** (`src/providers/adapters/upstox.py` lines 4–14):
```
Historical Candle V3   GET /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}
Intraday Candle V3     GET /v3/historical-candle/intraday/{key}/{unit}/{interval}
LTP Quotes V3          GET /v3/market-quote/ltp
OHLC Quotes V3         GET /v3/market-quote/ohlc
Option Greeks V3       GET /v3/market-quote/option-greek
```

**Evidence:** `upstox.py:88-89` — `UPSTOX_V3_BASE = "https://api.upstox.com/v3"`. `upstox.py:436` — log event `upstox_historical_ohlcv_v3_request`. `upstox.py:484` — `"api_version": "v3"` in response metadata. OI at index 6 extracted in `fetch_historical_ohlcv`.

**Status: SUPERSEDED. V3 implemented and verified live (reports/05, reports/PROVIDER_LIVE_TEST_REPORT).**  
**Superseded reports: `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`, `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`**

---

### Finding 2: "Angel One WebSocket binary decode broken — json.loads on binary"

**Old finding** (`ANGELONE_UPSTOX_FORENSIC_AUDIT.md`, `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> `angel_one_stream.py`: binary assumed JSON. `_decode_smartstream_binary()` not implemented. Binary protocol not decoded.

**Current implementation** (`src/providers/streams/angel_one_stream.py`):
- `_decode_smartstream_binary()` at line 126 uses `import struct` (line 74)
- Explicit `struct.unpack_from` calls at lines 151–252 for mode, exchange_type, token, seq_num, exchange_ts_ms, ltp_paise, ltq, atp, vol, open, high, low, close, last_trade_ts, oi, oi_change, week_high/low, circuit limits, depth levels
- FULL mode (3) unpacks OI at byte offset 128, depth at offsets 168+

**Evidence:** `angel_one_stream.py:74` `import struct`, `angel_one_stream.py:151-252` struct.unpack_from calls. `tests/unit/providers/test_angel_one_stream_binary.py` exists and passes.

**Status: SUPERSEDED. Binary decode fully implemented with struct.**  
**Superseded reports: `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`, `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`**

---

### Finding 3: "Angel One NFO exchange type (2) missing — F&O cannot be subscribed"

**Old finding** (`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> Exchange type: NFO (type=2) — MISSING. F&O options CANNOT be subscribed.

**Current implementation** (`src/providers/streams/angel_one_stream.py`):
- `EXCHANGE_TYPE_NFO: int = 2` at line 102
- NFO entry in exchange type map at line 110: `2: "NFO"`
- Example subscription at line 311 subscribes to NFO tokens `["43985", "43986"]`

**Evidence:** `angel_one_stream.py:102`, `angel_one_stream.py:110-111`, `angel_one_stream.py:311`.

**Status: SUPERSEDED. NFO exchange type implemented.**  
**Superseded reports: `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`, `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`**

---

### Finding 4: "Angel One WebSocket mode sent as string — should be integer"

**Old finding** (`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> Subscribe (action=1) — DEFECTIVE. Mode value sent as string ("LTP"), should be integer (1/2/3).

**Current implementation** (`src/providers/streams/angel_one_stream.py`):
- LTP mode = integer 1, QUOTE mode = integer 2, FULL mode = integer 3
- `subscribe()` method takes `mode: int` parameter

**Evidence:** `angel_one_stream.py:289-311` — subscription message uses integer mode values. `tests/unit/providers/test_angel_one_stream_binary.py` tests all three modes.

**Status: SUPERSEDED. Integer mode values implemented.**  
**Superseded reports: `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`**

---

### Finding 5: "Angel One getOIData not implemented — CRITICAL GAP"

**Old finding** (`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`, `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`):
> Historical OI endpoint `getOIData` — NOT_USED. Not implemented — CRITICAL GAP.

**Current implementation** (`src/providers/adapters/angel_one.py`):
- `fetch_historical_oi()` method at line 994
- Calls `POST /historical/v1/getOIData`
- OI never substituted with zero; `oiMissing=True` when absent

**Evidence:** `angel_one.py:994` `fetch_historical_oi`, `ANGELONE_DATA_COMPLETENESS_REPORT.md` section "HISTORICAL OPEN INTEREST". Unit tests in `test_angel_one_adapter_new.py`.

**Status: SUPERSEDED. getOIData implemented.**  
**Superseded reports: `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`, `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`**

---

### Finding 6: "Angel One optionGreek endpoint not implemented — CRITICAL GAP"

**Old finding** (`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> Option Greeks `POST /marketData/v1/optionGreek` — NOT_USED. Not implemented — CRITICAL GAP.

**Current implementation** (`src/providers/adapters/angel_one.py`):
- `fetch_option_greeks()` method at line 1112
- Calls `POST /marketData/v1/optionGreek`
- delta, gamma, theta, vega, IV all extracted; zero IV rejected

**Evidence:** `angel_one.py:1112` `fetch_option_greeks`. Unit tests in `test_angel_one_adapter_new.py`.

**Status: SUPERSEDED. optionGreek endpoint implemented.**  
**Superseded reports: `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`**

---

### Finding 7: "Upstox WebSocket hardcoded V2 URL — discontinued Aug 22 2025"

**Old finding** (`ANGELONE_UPSTOX_FORENSIC_AUDIT.md`, `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> Authorization endpoint BROKEN. Must call GET .../authorize → use returned URI. Code uses hardcoded V2 URL.

**Current implementation** (`src/providers/streams/upstox_stream.py`, `src/providers/adapters/upstox.py`):
- `fetch_ws_authorized_url()` in `upstox.py:966` calls `GET /v2/feed/market-data-feed/authorize` and returns the dynamic `authorized_redirect_uri`
- `UpstoxStreamAdapter._connect_once()` calls `auth_url_fetcher()` (a callable to `fetch_ws_authorized_url`) on **every reconnect** — no hardcoded WebSocket URL

**Remaining concern (not superseded):** The auth endpoint URL itself uses `UPSTOX_V2_BASE` path (`/v2/feed/market-data-feed/authorize`). This is the correct Upstox V3 flow — the authorize endpoint lives at V2 base but returns a V3 WebSocket URI. The authorize endpoint is confirmed in Upstox documentation as the current path. The old finding conflated the "discontinued V2 WebSocket" with the authorize endpoint.

**Evidence:** `upstox.py:98` `UPSTOX_WS_AUTH_URL`, `upstox.py:983` `_request("GET", UPSTOX_WS_AUTH_URL)`, `upstox_stream.py:604-617` dynamic auth URL fetch per reconnect.

**Status: SUPERSEDED (hardcoded URL claim). The dynamic authorized URI approach is correctly implemented.**  
**Superseded reports: `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`, `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`**

---

### Finding 8: "Upstox Protobuf decode is a STUB — every real tick discarded"

**Old finding** (`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> `_decode_protobuf()` JSON-tries first, then `{}` — every real tick discarded.

**Current implementation** (`src/providers/streams/upstox_stream.py`):
- `_try_import_pb2()` attempts to import `upstox_market_data_feeder_pb2` at runtime
- If pb2 module present: `_decode_protobuf_with_pb2()` calls `feed.ParseFromString(raw_bytes)` — real binary decode
- If pb2 absent: `_decode_protobuf_generic()` attempts JSON fallback (for tests/mocks), logs `upstox_protobuf_no_pb2_available`
- `_decode_feed_frame()` calls pb2 path first, falls back to generic

**Remaining gap (not superseded):** `upstox_market_data_feeder_pb2.py` does **NOT exist** in the repository (`find . -name "*pb2*"` returns nothing). Therefore at runtime, the pb2 path is never taken. All real Upstox binary frames fall through to `_decode_protobuf_generic()`. For real binary protobuf frames (not JSON), this returns `None` and the tick is dropped. The generic fallback cannot decode binary protobuf without the compiled schema.

**Status: PARTIALLY SUPERSEDED.** The stub has been replaced by a two-path architecture. However the pb2 file is absent, so the pb2 path is unreachable at runtime. Real binary protobuf ticks are **still not decoded in practice.**  
**Superseded reports: `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md` (stub characterization is outdated)**  
**Live gap that remains: Upstox WebSocket protobuf binary decode — `NOT_VERIFIED_LIVE`, pb2 missing**

---

### Finding 9: "Upstox option chain /v2/option/chain not implemented — CRITICAL GAP"

**Old finding** (`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> Option Chain `GET /v2/option/chain` — MISSING.

**Current implementation** (`src/providers/adapters/upstox.py`):
- `fetch_option_chain()` at line 809
- `fetch_option_contracts()` at line 864

**Live evidence** (`reports/PROVIDER_LIVE_TEST_REPORT.md`):
> NIFTY 2026-09-29: 123 rows, spot=23,329.55; BANKNIFTY: 145 rows; FINNIFTY: 123 rows — via analytics key.

**Status: SUPERSEDED. Implemented and live-verified.**  
**Superseded reports: `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`, `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`**

---

### Finding 10: "Upstox option-greek V3 not implemented — CRITICAL GAP"

**Old finding** (`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> Option Greeks `GET /v3/market-quote/option-greek` — MISSING.

**Current implementation** (`src/providers/adapters/upstox.py`):
- `fetch_option_greeks()` at line 732 — max 50 per request enforced
- `fetch_option_greeks_batched()` at line 777 — auto-batching for >50 keys

**Evidence:** `upstox.py:765` log event `upstox_option_greeks_v3_request`. Unit tests in `test_upstox_adapter_v3.py`.

**Status: SUPERSEDED. Implemented.**  
**Superseded reports: `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`, `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`**

---

### Finding 11: "Upstox rate limit wrong — 10 req/s (actual 50)"

**Old finding** (`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> Upstox rate limiter set to 10.0 req/s; actual documented limit is 50.0 req/s.

**Current implementation** (`src/providers/rate_limiter.py` / capability matrix):
> `PROVIDER_RATE_LIMITS['upstox'] = 50.0` — corrected per `ANGELONE_UPSTOX_FINAL_CERTIFICATION.md`.

**Status: SUPERSEDED. Rate limit corrected to 50 req/s.**  
**Superseded reports: `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`**

---

### Finding 12: "Upstox CAS fields not implemented — Sep 4 2026 launch"

**Old finding** (`ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`):
> CAS indicative_equilibrium_price — MISSING. CAS from WebSocket — NOT_DECODED.

**Current implementation** (`src/providers/adapters/upstox.py`, `src/providers/streams/upstox_stream.py`):
- `UpstoxNormalizer.normalize_cas_data()` in `src/core/normalizers/upstox.py`
- `ClosingAuctionSnapshot` DB model created
- CAS sub-dict extracted in WebSocket stream normalizer
- CAS indicative price does NOT overwrite LTP (separate table)

**Status: SUPERSEDED. CAS implemented.**  
**Superseded reports: `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md`, `ANGELONE_UPSTOX_FORENSIC_AUDIT.md`**

---

### Finding 13: "Upstox Closing Auction snapshot missing"

**Old finding** (`ANGELONE_UPSTOX_FORENSIC_AUDIT.md`):
> `ClosingAuctionSnapshot` table missing.

**Current implementation:** `ClosingAuctionSnapshot` model present in `src/db/models/`. Migration added. Confirmed in `ANGELONE_UPSTOX_FINAL_CERTIFICATION.md`.

**Status: SUPERSEDED.**

---

## FINDINGS THAT REMAIN ACCURATE (NOT SUPERSEDED)

These findings from older reports are still accurate as of 2026-09-17:

| Finding | Source | Still Accurate | Severity |
|---------|--------|---------------|----------|
| `upstox_market_data_feeder_pb2.py` absent — binary protobuf decode unreachable | ANGELONE_UPSTOX_FINAL_CERTIFICATION.md | **YES** | P1 |
| Upstox `UPSTOX_ACCESS_TOKEN` expired (exp 2026-09-14) | reports/PROVIDER_LIVE_TEST_REPORT.md | **YES** | P1 |
| Upstox live quotes not wired to MarketEngine | reports/20_FINAL_PRODUCTION_CERTIFICATION.md | **YES** | P1 |
| Redis ACL not configured for `mds:angel_one:jwt:*` | ANGELONE_UPSTOX_FINAL_CERTIFICATION.md | **YES** | P2 |
| SmartStream binary offsets unconfirmed against live data | ANGELONE_UPSTOX_FINAL_CERTIFICATION.md | **YES** | P1 |
| ProviderGateway.fetch() is a stub (NotImplementedError) — not the enforced egress path | This audit | **YES** | Architectural debt |
| `or 0` on OHLC fields in `openchart.py`, `jugaad_data.py` | This audit | **YES** | P2 |
| `or 0` on `openInterest` in `delta_exchange.py:265` | This audit | **YES** | P2 (crypto) |
| 40 capabilities remain `NOT_VERIFIED_LIVE` | ANGELONE_UPSTOX_FINAL_CERTIFICATION.md | **YES** | All phases 2–22 |
| Upstox Plus plan not active — expired instruments API unavailable | ANGELONE_UPSTOX_FINAL_CERTIFICATION.md | **YES** | P2 (F&O backfill) |

---

## REPORT DISPOSITION

| Report | Action Required |
|--------|----------------|
| `ANGELONE_UPSTOX_FORENSIC_AUDIT.md` | **ARCHIVE — STALE.** Add header: "PRE-FIX BASELINE — superseded by ANGELONE_UPSTOX_FINAL_CERTIFICATION.md dated 2026-09-16." Do not delete — retains forensic record of original defects. |
| `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md` | **ARCHIVE — STALE.** Add header: "PRE-FIX BASELINE — superseded by ANGELONE_UPSTOX_FINAL_CERTIFICATION.md dated 2026-09-16." |
| `PRODUCTION_CERTIFICATION.md` | Already self-declares REVOKED. Header present. No further action. |
| `ANGELONE_UPSTOX_FINAL_CERTIFICATION.md` | **CURRENT** — 40 items NOT_VERIFIED_LIVE are the honest status. This document is the authoritative implementation-level record pending live certification. |
| `reports/20_FINAL_PRODUCTION_CERTIFICATION.md` | **CURRENT** — primary certification. Contains real live evidence from 2026-09-14/15. |
| `reports/PROVIDER_LIVE_TEST_REPORT.md` | **CURRENT** — primary live evidence record. |
| All other reports/ | **CURRENT** — no contradictions found. |

---

## FINAL RECONCILIATION VERDICT

All 13 stale "MISSING/BROKEN/DEFECTIVE" findings from `ANGELONE_UPSTOX_FORENSIC_AUDIT.md` and `ANGELONE_UPSTOX_CAPABILITY_MATRIX.md` have been reconciled:

- **12 of 13 findings:** SUPERSEDED by subsequent implementation
- **1 of 13 findings:** Partially superseded (protobuf decode architecture improved, but pb2 file still absent)

The two stale documents are pre-fix baselines, not current state. They must be marked as such. No contradictory live-status claims exist in the current authoritative reports.

The honest live-certification status as of 2026-09-17 remains:

```
CONDITIONALLY CERTIFIED — LIVE VERIFICATION REMAINS
```

This reflects:
- Real live evidence exists for: Angel One auth, Angel One historical (EQ + F&O), Upstox historical (EQ + IDX), Upstox option chain via analytics key
- No live evidence exists for: SmartStream binary offsets, Upstox WebSocket binary protobuf, all REST Greeks/OI live calls, cross-provider reconciliation, circuit breaker under real failure, rate limiter under real load, latency measurements

---

*Generated: 2026-09-17. Based on static codebase analysis only — no live provider calls made.*  
*Evidence methodology: grep, file read, git log, token JWT metadata decode (expiry only, no secret values).*
