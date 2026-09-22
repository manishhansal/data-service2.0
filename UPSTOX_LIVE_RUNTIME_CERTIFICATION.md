# UPSTOX LIVE RUNTIME CERTIFICATION — SUMMARY

**Last updated:** 2026-09-17  
**Full report:** `reports/UPSTOX_LIVE_RUNTIME_CERTIFICATION.md`

| Dimension | Status | Latest evidence |
|-----------|--------|----------------|
| Analytics token | ✅ LRV | Valid until 2027-09-03 (351 days); accepted by all read-only calls |
| OAuth access token | ❌ EXPIRED | 2026-09-14 — must refresh via OAuth callback |
| LTP V3 | ✅ LRV | 2026-09-17 — RELIANCE=1246.3, HDFCBANK=714.1, NIFTY=23240.7 |
| Full Quote **V3** | ✅ LRV | 2026-09-17 — migrated from V2; ltp=1246.3 + depth + totalBuyQty |
| Historical V3 — ALL 9 intervals | ✅ LRV | 2026-09-17 — 5m now works (V2 restriction lifted) |
| Option chain NIFTY/BANKNIFTY/FINNIFTY | ✅ LRV | 2026-09-17 — 128/150/123 rows; option_chain_snapshot persisted |
| option_chain_snapshot persistence | ✅ LRV | 2026-09-17 — 54 snapshots + 20 contracts in DB |
| option_greeks_snapshot persistence | ✅ LRV | 2026-09-17 — 15 rows; iv/delta/oi confirmed |
| Option Greeks V3 | ✅ LRV | 2026-09-17 — NIFTY 5 contracts; iv=0.83, oi=93,665 |
| 6 Market Information APIs | ✅ IMPLEMENTED | fetch_oi_data/pcr_data/max_pain/fii/dii/change_oi (May 2026) |
| 3 Smartlist APIs | ✅ IMPLEMENTED | fetch_smartlist_futures/options/mtf (May 2026) |
| WebSocket V3 protobuf | ❌ BLOCKED | pb2 file absent; binary decode unreachable |
| Multi-worker token sharing | ❌ NI | Not implemented — instance-only storage |
