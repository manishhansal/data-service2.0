# ANGEL ONE LIVE RUNTIME CERTIFICATION — SUMMARY

**Last updated:** 2026-09-17  
**Full report:** `reports/ANGELONE_LIVE_RUNTIME_CERTIFICATION.md`

| Dimension | Status | Latest evidence |
|-----------|--------|----------------|
| Authentication (TOTP + JWT + Redis) | ✅ LRV | 2026-09-17 — 4-worker confirmation, 294ms |
| Historical OHLCV EQ 1m/5m/1d | ✅ LRV | 2026-09-17 — HDFCBANK 889×1m, NIFTY 152×5m |
| Historical OHLCV F&O 1d | ⚠️ EMPTY | Token resolution gap — instrument_provider_mapping lookup needed |
| Live quote FULL (numeric token) | ✅ LRV | 2026-09-17 — RELIANCE ltp=1246.3 + depth + circuits |
| getLtpData lightweight | ✅ LRV | 2026-09-17 — HDFCBANK 715.8; RELIANCE 1244.0 |
| Option Greeks REST | ✅ LRV | 2026-09-17 — NIFTY 163 + BANKNIFTY 165 = 328 contracts |
| market_quote persistence | ✅ LRV | 2026-09-17 — 439 rows; depth_json + weekHigh52 + circuits |
| Historical OI (getOIData) | ❌ BLOCKED | Plan restriction on account M495775 |
| SmartStream WebSocket | ❌ NVL | Binary offsets not confirmed against live frames |
| Broker Analytics (PCR) | ✅ FIXED | Schema bug fixed 2026-09-17 |
| MarketEngine live routing | ✅ FIXED | Token resolution fixed 2026-09-17; zero HTTP 400s |
