# REPORT 06 — INDIAN LIVE DATA CERTIFICATION
**Audit date:** 2026-09-13

---

## Status: ⛔ BLOCKED — Credentials Unavailable + Infrastructure Not Running

All Indian live data tests are blocked by two conditions:
1. All broker credentials are empty in `.env.local` (ANGEL_ONE_API_KEY, UPSTOX_API_KEY, etc.)
2. Docker Compose services are not running (no API server, no Redis, no PostgreSQL)

---

## Required Symbol Test Matrix

| Symbol | Type | Exchange | Angel One | Upstox | NSE/Scrapling | Yahoo | DB Rows | Latest TS | Status |
|---|---|---|---|---|---|---|---|---|---|
| NIFTY | Index | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| BANKNIFTY | Index | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| FINNIFTY | Index | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| MIDCPNIFTY | Index | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| RELIANCE | Equity | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| HDFCBANK | Equity | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| ICICIBANK | Equity | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| INFY | Equity | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| TCS | Equity | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| SBIN | Equity | NSE | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | 0 | N/A | BLOCKED |
| NIFTY CE/PE F&O | Option | NFO | ⛔ BLOCKED | ⛔ BLOCKED | ⛔ BLOCKED | N/A | 0 | N/A | BLOCKED |

---

## Code-Level Implementation Check

| Capability | Code Implementation | Test Coverage | Notes |
|---|---|---|---|
| LTP | `fetch_live_quote()` Angel + Upstox | `test_angel_one.py`, `test_upstox.py` | 🟡 |
| OHLC | Same — `open`, `high`, `low`, `close` fields | Same | 🟡 |
| Volume | `volume` field, `volumeUnavailable: true` when absent | Same | 🟡 |
| OI | `oi` from `opnInterest`, null when absent | `test_normaliser.py` | 🟡 |
| Bid/Ask | `bid`, `ask` — null when absent | `test_normaliser.py` | 🟡 |
| IV | Modelled field, null unless provider supplies | Same | 🟡 |
| Greeks | All null unless provider supplies | Same | 🟡 |
| Option chain | `get_option_chain()` with strikes, expiries, CE/PE | `test_market_engine.py` | 🟡 |
| PCR | `fetch_pcr()` Angel One | `test_analytics.py` | 🟡 |
| OI buildup | `fetch_oi_buildup()` | `test_analytics.py` | 🟡 |
| Market status | `MarketSessionEngine` 6 phases | `test_market_session.py` | 🟡 |

---

## Blockers to Resolution

1. Configure ANGEL_ONE_API_KEY + ANGEL_ONE_CLIENT_ID + ANGEL_ONE_TOTP_SECRET in .env.local
2. Configure UPSTOX_API_KEY + UPSTOX_API_SECRET in .env.local
3. Run `docker compose --env-file .env.local up -d redis postgres`
4. Run `alembic upgrade head`
5. Start API: `uvicorn src.server:app --reload`
6. Run live tests during market hours (09:15–15:30 IST weekdays)

**Current status: 0 of the above steps completed.**
