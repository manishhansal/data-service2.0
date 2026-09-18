# Production Certification Status
## data-service2.0 + AlphaForge

---

**Last verified:** 2026-09-18
**Git branch:** fix/bugs
**Git commit:** 56156ec (HEAD; prior canonical: 5dd69a2, 9b0ba37)
**Test suite:** 4821 passed, 0 failed, 6 warnings (all external)
**Runtime environment:** Python 3.14.6, Docker stack running (all containers healthy)

---

## Master Certification Gate Table

| Gate | Status | Evidence | Blocker |
|------|--------|----------|---------|
| Angel One EQ REST (historical, live quote) | PASS | Live call 2026-09-17: RELIANCE 1244.1, REST + DB verified | — |
| Angel One F&O token resolution | PASS | instrument_provider_mapping: 34,505 active rows (deduped); tokens resolve for NIFTY/BANKNIFTY/RELIANCE/TCS FUT | — |
| Angel One historical OI | BLOCKED | `getOIData` returns "Invalid Bad Request" (plan restriction) | BLOCKED_BY_EXTERNAL |
| Angel One JWT in Redis | PASS | `mds:angel_one:jwt:M495775` present, ~5h TTL remaining (2026-09-18) | — |
| Angel One SmartStream live | NOT VERIFIED | Requires market hours + live feedToken | BLOCKED_BY_EXTERNAL |
| Upstox REST (historical, LTP, option chain) | PASS | Live call 2026-09-17: RELIANCE 1243.9; chains confirmed | — |
| Upstox OAuth multi-worker safety | PASS | Redis lock + 23h TTL (implemented + unit-tested) | — |
| Upstox OAuth access token | BLOCKED | Expired 2026-09-14; `UPSTOX_CLIENT_SECRET` not configured | BLOCKED_BY_EXTERNAL |
| Upstox protobuf (pb2) | PASS | `upstox_market_data_feeder_pb2.py` present in git; `_try_import_pb2()` returns module; FeedResponse round-trip verified 2026-09-18 | — |
| Upstox WebSocket live binary decode | NOT VERIFIED | Blocked by expired OAuth token | BLOCKED_BY_EXTERNAL |
| ProviderGateway dispatch | PASS | `fetch()` real dispatch; Yahoo excluded from F&O chain | — |
| Hierarchical rate limiting (50/s + 500/min + 2000/30min) | PARTIAL | Implemented + 143 rate-limit tests pass; live provider load test not run (unsafe) | — |
| Live rate-limit load test | NOT VERIFIED | Unsafe to run against live provider quota without controlled window | BLOCKED_BY_EXTERNAL |
| Circuit breaker OPEN→HALF_OPEN→CLOSED | PASS | Drill 5 PASS 2026-09-17 (log evidence) | — |
| Provider failover Angel→Upstox | PASS | Drill 1 PASS 2026-09-17: Upstox returned 4 candles when Angel CB forced OPEN | — |
| Provider failover Upstox→Angel | BLOCKED | Upstox OAuth expired | BLOCKED_BY_EXTERNAL |
| market_tick persistence | NOT VERIFIED | 0 rows — live streaming session required | BLOCKED_BY_EXTERNAL |
| CandleBuilder (tick→OHLCV) | PASS | 24 unit tests PASS; implementation complete | — |
| Live candle pipeline (stream→equity_candle) | NOT VERIFIED | Requires live WS session | BLOCKED_BY_EXTERNAL |
| equity_candle DB | PASS | 5,470,621 rows; 0 OHLC violations; 0 3m rows; 0 neg-latency; 0 duplicates (SQL verified 2026-09-18) | — |
| futures_candle DB | PASS | 55,060 rows; 0 OHLC violations; OI=NULL (not 0); 0 candles past expiry; no survivorship leak | — |
| options_candle DB | NOT VERIFIED | 0 rows — no options backfill run | — |
| market_quote persistence | PASS | 3 rows with depth (live call 2026-09-17) | — |
| 3m interval blocked | PASS | DB CHECK constraint + CandleBuilder ValueError + adapter guard | — |
| Exchange calendar | PASS | 3,654 rows (NSE 2024–2028) | — |
| Ingestion checkpoints | PASS | 36 Redis checkpoint keys confirmed; 14/14 checkpoint tests PASS | — |
| Point-in-time (temporal integrity) | PASS | SQL: 0 future_received_at, 0 received_before_candle, 0 OHLC violations, 0 3m rows, 0 neg-volume (2026-09-18 live DB) | — |
| OI NULL not zero | PASS | 0 oi_zero rows in futures_candle; open_interest=NULL throughout (correct) | — |
| instrument_provider_mapping deduplication | PASS | 34,410 stale 2020-01-01 seed duplicates deactivated 2026-09-18; 34,505 active rows, each instrument+provider unique | — |
| F&O 30-day pilot | PARTIAL | Executed 2026-09-18; gates PASS (no_3m, no OHLC, no dups, no lookahead, no missing provenance); Angel 403 on 1m/some 5m/15m/1h for select instruments; Upstox 1d NFO missing keys | — |
| Survivorship bias (DB) | PASS | 0 futures_candle rows past instrument expiry (SQL verified 2026-09-18) | — |
| Survivorship bias (expired contract in pilot) | NOT VERIFIED | No contracts expired during the pilot window (all expiry 2026-09-29) | — |
| AlphaForge architecture (no direct provider calls) | PASS | AlphaForge env: `DATA_SERVICE_URL=http://host.docker.internal:8200`; no provider credentials | — |
| AlphaForge → data-service connectivity | PASS | `node fetch` from inside alpha-forge-worker → 8200 → real RELIANCE candles returned (2026-09-18) | — |
| AlphaForge India E2E (live signal→UI) | PARTIAL | API returns real DB candles; india-signal-snapshotter stamping 173/173 symbols; full signal→UI path not validated in live session | — |
| AlphaForge crypto feed | PASS | Worker: BTC/ETH/SOL scalper active; indicator state restoring correctly | — |
| ML dataset temporal integrity | NOT VERIFIED | Requires Tier 3 certification first | — |
| Strategy profitability | NOT CERTIFIED | No current OOS evaluation; historical baseline (321 trades, PF 0.90) is NOT certification | — |

---

## Certification Tiers

| Tier | Name | Status |
|------|------|--------|
| TIER 1 | REST_DATA_CERTIFIED | PARTIAL |
| TIER 2 | REALTIME_STREAM_CERTIFIED | NOT VERIFIED |
| TIER 3 | FNO_DATA_CERTIFIED | PARTIAL |
| TIER 4 | PROVIDER_RESILIENCE_CERTIFIED | PARTIAL |
| TIER 5 | ALPHAFORGE_MARKET_DATA_E2E_CERTIFIED | PARTIAL |
| TIER 6 | ML_DATASET_CERTIFIED | NOT VERIFIED |
| TIER 7 | STRATEGY_PROFITABILITY_CERTIFIED | NOT CERTIFIED |

### TIER 1 — REST_DATA_CERTIFIED: PARTIAL
Angel One EQ: PASS | Upstox EQ: PASS | Canonical DB: PASS | Provenance: PASS
Blocked: Angel OI (plan restriction) | Upstox OAuth expired | Angel SmartStream not live-tested

### TIER 2 — REALTIME_STREAM_CERTIFIED: NOT VERIFIED
Implementation: COMPLETE. pb2: PRESENT and VERIFIED. CandleBuilder: PASS (unit). TickPersister: PASS (unit).
market_tick = 0 rows. No live WS session completed.
Blocked: Upstox OAuth expired; Angel SmartStream requires market hours.

### TIER 3 — FNO_DATA_CERTIFIED: PARTIAL
Token lookup: PASS (BUG-024 fixed, deduplication complete). Expiry fix: PASS (BUG-025).
Pilot gates: PASS (no_3m, no OHLC, no dups, no lookahead, no missing provenance).
futures_candle: 55,060 rows, 0 violations, OI=NULL (correct).
Blocked: Angel 403 on 1m/some intervals (plan); Upstox 1d NFO key mapping incomplete; expired-contract survivorship test not possible (all contracts expire 2026-09-29).

### TIER 4 — PROVIDER_RESILIENCE_CERTIFIED: PARTIAL
Circuit breaker: PASS. Angel→Upstox failover: PASS. Yahoo F&O exclusion: PASS.
Rate limiting: IMPLEMENTED + 143 unit tests PASS. Live load test: NOT EXECUTED (provider quota risk).
Upstox→Angel failover: BLOCKED (OAuth expired).

### TIER 5 — ALPHAFORGE_MARKET_DATA_E2E_CERTIFIED: PARTIAL
Architecture verified (no direct provider calls from AlphaForge).
data-service connectivity: PASS (live container test 2026-09-18).
India signal snapshotter: active (173/173 symbols).
Full signal→UI path not verified in a live NSE market session.

### TIER 6 — ML_DATASET_CERTIFIED: NOT VERIFIED
Prerequisite: Tier 3 must be fully certified first.

### TIER 7 — STRATEGY_PROFITABILITY_CERTIFIED: NOT CERTIFIED
No current OOS evaluation. Historical baseline is not certification evidence.

---

## External Blockers

| # | Blocker | Classification |
|---|---------|---------------|
| 1 | Upstox OAuth access token expired (2026-09-14) | BLOCKED_BY_EXTERNAL |
| 2 | `UPSTOX_CLIENT_SECRET` not configured — auto-refresh impossible | BLOCKED_BY_EXTERNAL |
| 3 | Angel One `getOIData` plan restriction ("Invalid Bad Request") | BLOCKED_BY_EXTERNAL |
| 4 | Angel One SmartStream — not validated live (requires market hours) | BLOCKED_BY_EXTERNAL |
| 5 | Angel One HTTP 403 for 1m/some other intervals on F&O (plan restriction) | BLOCKED_BY_EXTERNAL |
| 6 | Upstox NFO 1d key mapping incomplete (`upstox_instrument_key_unknown`) | BLOCKED_BY_EXTERNAL |
| 7 | Expired F&O contract in pilot window — none available (all expire 2026-09-29) | BLOCKED_BY_EXTERNAL |

## Remaining Internal Work

| # | Item | Priority |
|---|------|----------|
| 1 | Consumer API rate limiting (inbound) — mount `RateLimitMiddleware` in server.py | P3 |
| 2 | Survivorship bias live test — pilot window with at least one expired contract | P2 |
| 3 | Upstox WS live binary decode (post-OAuth refresh) | P1 |
| 4 | Angel One SmartStream live validation (during market hours) | P1 |
| 5 | Live provider rate-limit load test (post-credential restoration) | P2 |
| 6 | market_tick persistence (requires live streaming session) | P1 |
| 7 | Live candle pipeline verification (requires streaming session) | P1 |

---

## Test Suite (2026-09-18)

| Suite | Count | Status |
|-------|-------|--------|
| Full suite (unit + property + perf + integration) | **4821** | **PASS** |
| Failures | 0 | — |
| Warnings | 6 | All external: starlette ×1, urllib3 ×1, jugaad_data ×4 |

> Previous counts "4,413", "4,564", "4821" in older report sections reflect historical runs. Current authoritative count: **4821**.

---

## Data Quality Snapshot (2026-09-18, live DB)

| Table | Rows | OHLC Violations | Duplicates | 3m Rows | OI=0 Rows | Neg Volume | Candles Past Expiry |
|-------|------|-----------------|------------|---------|-----------|------------|---------------------|
| equity_candle | 5,470,621 | 0 | 0 | 0 | n/a | 0 | n/a |
| futures_candle | 55,060 | 0 | 0 | n/a | 0 | 0 | 0 |
| options_candle | 0 | n/a | n/a | n/a | n/a | n/a | n/a |

OI semantics: `open_interest = NULL` means unavailable (BLOCKED_BY_EXTERNAL plan restriction). Never silently set to 0.

---

## Database Integrity Fixes Applied (2026-09-18)

- **instrument_provider_mapping deduplication:** 34,410 stale rows with `valid_from=2020-01-01` deactivated where a newer row existed for the same instrument+provider. Active rows reduced from 68,915 → 34,505. All active rows now 1:1 with instrument+provider pairs. Zero cases of differing tokens across deactivated rows.

---

## Documentation Status

| Document | Status |
|---------|--------|
| PRODUCTION_CERTIFICATION_STATUS.md | **This document** — updated in-place 2026-09-18 |
| reports/UPSTOX_PROTOBUF_CERTIFICATION.md | Rewritten 2026-09-17; contradictions removed; current truth verified 2026-09-18 |
| reports/PROVIDER_RATE_LIMIT_REPORT.md | Updated in-place 2026-09-17 |
| reports/PROVIDER_FAILURE_DRILL_REPORT.md | Updated in-place 2026-09-17 |
| reports/FNO_HISTORICAL_PILOT_REPORT.md | Updated in-place; pilot re-run 2026-09-18 PASS |
| FINAL_PROVIDER_RUNTIME_CERTIFICATION.md | Updated in-place 2026-09-17 |

No new report files created. All updates made in-place.

---

## SUPERSEDED / HISTORICAL

> The following claims from earlier document versions are superseded:
>
> - "pb2 does not exist" / "_try_import_pb2() returns None" — SUPERSEDED. pb2 is present and verified.
> - "4,413 tests" — SUPERSEDED historical count.
> - "4,564 tests" — SUPERSEDED historical count.
> - "instrument_provider_mapping has 68,915 active rows" — SUPERSEDED. Deduplication applied 2026-09-18; 34,505 active rows.
> - "test_index_segment_count expects == 968 IDX rows" — SUPERSEDED. Changed to >= 968 (DB has 4,651 after additional backfill).
> - "PASS WITH BLOCKERS" terminology — SUPERSEDED. Now uses: PASS / PARTIAL / BLOCKED / NOT VERIFIED / FAIL.
