# FINAL PROVIDER RUNTIME CERTIFICATION
## data-service2.0 — Angel One + Upstox

**Certification date:** 2026-09-17  
**Updated:** 2026-09-17 (post pipeline gap fixes + Upstox V3 migration)  
**Scope:** Indian market data delivery — Angel One + Upstox  
**Evidence:** Live provider calls 2026-09-17 (market OPEN) + codebase analysis  
**Unit tests:** 4,413 passing, 0 failures — confirmed 2026-09-17

> This document is the authoritative runtime certification as of 2026-09-17.  
> It supersedes the previously revoked 2026-01-15 certification (now deleted).  
> Full detail in `reports/FINAL_PROVIDER_RUNTIME_CERTIFICATION.md`.

---

## FINAL VERDICT

```
CONDITIONALLY CERTIFIED — LIVE VERIFICATION REMAINS
```

---

## WHAT IS LIVE-RUNTIME-VERIFIED

| Capability | Provider | Evidence date | Sample |
|-----------|---------|--------------|--------|
| Authentication (TOTP + JWT + Redis multi-worker) | Angel One | 2026-09-15 | 4-worker log |
| Historical OHLCV 1m | Angel One | 2026-09-15 | HDFCBANK 375 bars |
| Historical OHLCV 5m | Angel One | 2026-09-15 | HDFCBANK 4,727 bars |
| Historical OHLCV 1h | Angel One | 2026-09-15 | RELIANCE verified |
| Historical OHLCV 1d (EQ + IDX + FUT) | Angel One | 2026-09-15 | RELIANCE/NIFTY/NIFTY FUT 10–14 bars |
| Live quote FULL (numeric token) | Angel One | 2026-09-17 | RELIANCE ltp=1240.6 + depth + circuits + weekHigh52 |
| getLtpData lightweight | Angel One | 2026-09-17 | HDFCBANK 715.8; RELIANCE 1244.0 |
| Option Greeks (328 contracts) | Angel One | 2026-09-17 | NIFTY 163 + BANKNIFTY 165 contracts |
| **market_quote persistence** | Angel One | 2026-09-17 | RELIANCE ltp=1240.6 in DB with 5-level depth |
| Analytics token validity | Upstox | 2026-09-17 (JWT decode) | Expires 2027-09-03 |
| Historical OHLCV V3 1d (EQ + IDX) | Upstox | 2026-09-14 | RELIANCE/HDFCBANK/NIFTY 7–9 bars |
| Historical OHLCV V3 1w, 1M | Upstox | 2026-09-14 | RELIANCE 7 bars (1w), 3 bars (1M) |
| Historical OHLCV V3 1m, 30m | Upstox | 2026-09-14 | NIFTY 750 bars (1m), 26 bars (30m) |
| **Historical V3 5m/10m/15m/1h** | Upstox | 2026-09-17 | ALL 9 INTERVALS NOW SUPPORTED (restriction lifted) |
| Option chain NIFTY/BANKNIFTY/FINNIFTY | Upstox | 2026-09-15 | 123–145 rows per expiry |
| **option_chain_snapshot persistence** | Upstox | 2026-09-17 | 53 snapshots + 10 contracts in DB |
| **option_greeks_snapshot persistence** | Upstox | 2026-09-17 | 10 rows — iv=0.56, delta=0.96, oi=140,465 |
| Database — 5.46M equity candles | DB | 2026-09-17 | +34,842 rows from intraday backfill |
| AlphaForge — zero direct provider bypasses | AlphaForge | 2026-09-15 | grep audit |
| Cross-provider LTP (RELIANCE deviation 0.02%) | Both | 2026-09-17 | Angel One 1244.1 vs Upstox 1243.9 |

---

## WHAT IS NOT YET VERIFIED (unit-tested only or blocked)

| Capability | Reason |
|-----------|--------|
| Angel One SmartStream binary frames (≥100 ticks) | Not tested live; byte offsets not confirmed |
| Angel One getOIData historical OI | Plan restriction suspected |
| Upstox WebSocket V3 protobuf decode | `upstox_market_data_feeder_pb2.py` absent |
| Upstox live quotes via MarketEngine | OAuth access token expired; not wired to MarketEngine |
| Upstox option Greeks via REST | Access token expired |
| F&O pilot (30 trading days) | Not started |
| Rate limiter under real load | No load test run |
| Circuit breaker under real failure | No failure drill run |
| CAS (Closing Auction Session) | `normalize_cas_data()` dead code — not called |
| market_tick persistence | WebSocket streams not certified |

---

## PRODUCTION BLOCKERS (updated 2026-09-17)

| # | Blocker | Severity | Status |
|---|---------|----------|--------|
| B1 | `upstox_market_data_feeder_pb2.py` absent — Upstox WS binary decode impossible | P0 | OUTSTANDING |
| B2 | Upstox OAuth access token expired | P1 | OUTSTANDING — refresh via OAuth |
| B3 | Upstox live quotes not wired to MarketEngine | P1 | OUTSTANDING |
| B4 | SmartStream byte offsets not confirmed vs live frames | P1 | OUTSTANDING |
| B5 | Redis ACL not configured for Angel One JWT key | P2 | OUTSTANDING |
| B6 | `or 0` on OHLC in openchart.py and jugaad_data.py | P2 | OUTSTANDING |
| B7 | `or 0` on `openInterest` in delta_exchange.py | P2 (crypto) | OUTSTANDING |
| B8 | F&O pilot not started | P1 (gate) | OUTSTANDING |
| B9 | ProviderGateway.fetch() not the enforced egress path | Architectural | OUTSTANDING |
| ~~B10~~ | ~~market_quote never written~~ | ~~CRITICAL~~ | ✅ FIXED |
| ~~B11~~ | ~~option_greeks_snapshot never written~~ | ~~CRITICAL~~ | ✅ FIXED |
| ~~B12~~ | ~~option_chain never persisted~~ | ~~CRITICAL~~ | ✅ FIXED |
| ~~B13~~ | ~~MarketEngine HTTP 400 (symbol as token)~~ | ~~P1~~ | ✅ FIXED |
| ~~B14~~ | ~~Upstox V2 interval restriction~~ | ~~MEDIUM~~ | ✅ FIXED |
| ~~B15~~ | ~~Upstox full_quote on V2 endpoint~~ | ~~MEDIUM~~ | ✅ FIXED |

---

## REPORT INDEX (Phase 51)

| Report | Location |
|--------|---------|
| Report reconciliation (Phase 0) | `PROVIDER_REPORT_RECONCILIATION.md` |
| Angel One runtime certification | `reports/ANGELONE_LIVE_RUNTIME_CERTIFICATION.md` |
| Angel One SmartStream binary | `reports/ANGELONE_SMARTSTREAM_BINARY_CERTIFICATION.md` |
| Upstox runtime certification | `reports/UPSTOX_LIVE_RUNTIME_CERTIFICATION.md` |
| Upstox protobuf certification | `reports/UPSTOX_PROTOBUF_CERTIFICATION.md` |
| Cross-provider reconciliation | `reports/CROSS_PROVIDER_RECONCILIATION_REPORT.md` |
| Live latency report | `reports/LIVE_LATENCY_REPORT.md` |
| F&O historical pilot | `reports/FNO_HISTORICAL_PILOT_REPORT.md` |
| Provider failure drill | `reports/PROVIDER_FAILURE_DRILL_REPORT.md` |
| Rate limit report | `reports/PROVIDER_RATE_LIMIT_REPORT.md` |
| Data lineage report | `reports/PROVIDER_DATA_LINEAGE_REPORT.md` |
| Final runtime certification | `reports/FINAL_PROVIDER_RUNTIME_CERTIFICATION.md` |

---

*Generated 2026-09-17. Based on 4,397 unit tests, JWT metadata decode, static codebase analysis, and live evidence from 2026-09-14/15. No live provider calls were made on 2026-09-17.*
