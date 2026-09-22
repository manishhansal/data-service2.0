# INDIAN DATA SERVICE API GAP MATRIX
**Original date:** 2026-09-13  
**Updated:** 2026-09-17 — all critical gaps resolved  
**Status:** RESOLVED — All path incompatibilities fixed; market_quote/option_greeks persistence fixed  
**Purpose:** Maps what AlphaForge needs vs what data-service2.0 provides

---

## ✅ RESOLVED: API Path Incompatibility (2026-09-13)

The original gap — path mismatch preventing data-service2.0 from serving AlphaForge — was fixed during the 2026-09-13 audit:

| Path called by AlphaForge | Served by | Status |
|---|---|---|
| `GET /scraping/historical` | ~~wrong service~~ | ✅ FIXED — compatibility routes added |
| `GET /scraping/quotes` | ~~wrong service~~ | ✅ FIXED |
| `GET /scraping/option-chain` | ~~wrong service~~ | ✅ FIXED |
| `GET /scraping/instruments` | ~~wrong service~~ | ✅ FIXED |
| `POST /data/gate` | data-service2.0 | ✅ CONNECTED |

---

## API Gap Matrix

| Requirement | AlphaForge Consumer | Current data-service2.0 API | Required fields | Live | Historical | Provider support | Persistence | Missing/Incorrect | Required change |
|---|---|---|---|---|---|---|---|---|---|
| Live OHLCV quote | ScraplingProvider, signal engine, scanner | `/v1/india/quotes/{symbol}` ✅ EXISTS | ltp, open, high, low, volume, oi, provider, fetchedAt | ✅ | N/A | Angel One (stub), Upstox (not wired), Yahoo (not wired) | No (cache only) | MarketEngine uses STUB provider, not real Angel One adapter | Wire AngelOneAdapter into MarketEngine |
| Batch quotes | ScraplingProvider (170 symbols) | `/v1/india/quotes/batch` ❌ MISSING | Same as above, array | ✅ | N/A | None | No | No batch endpoint | Add `GET /v1/india/quotes/batch?symbols=A,B` |
| Historical OHLCV | ScraplingProvider, backfill, signal engine | `/v1/india/historical` ✅ EXISTS | time, open, high, low, close, volume, oi | ❌ | ✅ | Angel One adapter ✅, Upstox adapter ✅, Yahoo ✅, Jugaad ✅, OpenChart ✅ | ✅ (DB schema exists) | DB not running; historical engine wired but needs live Angel One credentials; no compatibility route `/scraping/historical` | Start DB; add `/scraping/historical` compat route |
| Option chain | ScraplingProvider, scalper, signal engine | `/v1/india/option-chain` ✅ EXISTS | strike, ce/pe ltp/oi/volume/iv/greeks, spot, expiry | ✅ | N/A | MarketEngine stub | No | MarketEngine uses stub; no real broker connection; missing `/scraping/option-chain` compat route | Wire AngelOneAdapter for option chain; add compat route |
| Instruments | ScraplingProvider | `/v1/instruments` ✅ EXISTS (different path) | token, tradingSymbol, exchange, instrumentType, expiry, strike, optionType | N/A | On-demand | Angel One ScripMaster | ✅ (instrument_master table) | Path is `/v1/instruments` not `/v1/india/instruments`; missing `/scraping/instruments` compat route | Add `/scraping/instruments` compat route; DB needed |
| Market status | Signal engine, worker scheduler | `/v1/india/market/status` ✅ EXISTS | sessionPhase, tradingDay, nextTradingDay | ✅ | N/A | Local session engine | N/A | ✅ WORKS (no provider needed) | None — already fully implemented |
| Historical gaps | Backfill orchestrator | `/v1/india/historical/gaps` ✅ EXISTS | gapId, instrumentId, gapStart, gapEnd, status | N/A | ✅ | DB | ✅ | DB not running | Start DB |
| Backfill trigger | Backfill orchestrator | `POST /v1/india/historical/backfill` ✅ EXISTS | symbol, exchange, interval, from, to | N/A | ✅ | All adapters | ✅ | DB not running; actual backfill needs credentials | Start DB; configure credentials |
| Broker analytics PCR | AI signals, daily picks | `/v1/india/broker-analytics/pcr` ✅ EXISTS | pcr, putOI, callOI | ✅ | N/A | Angel One | No | Angel One adapter not configured in app.state | Configure AngelOneAdapter in app startup |
| Broker analytics OI buildup | AI signals, daily picks | `/v1/india/broker-analytics/oi-buildup` ✅ EXISTS | symbol, oiBuildup, direction | ✅ | N/A | Angel One | No | Same | Same |
| Broker analytics gainers/losers | AI signals | `/v1/india/broker-analytics/gainers-losers` ✅ EXISTS | symbol, gain, category | ✅ | N/A | Angel One | No | Same | Same |
| Data quality gate | Signal engine (HARD BLOCK) | `POST /data/gate` ✅ EXISTS ✅ CONNECTED | signalEngineAllowed, confidenceScore, quality | ✅ | N/A | Local quality engine | No | ✅ WORKS | None |
| Live tick stream (WebSocket) | Worker candle builder, browser feed | No WebSocket endpoint for Indian market | token, ltp, volume, oi, exchangeTimestampMs | ✅ needed | N/A | Angel One SmartStream, Upstox WS | Redis pub/sub | No WebSocket/SSE endpoint in data-service2.0 for India ticks | Add `/v1/india/stream` WebSocket endpoint |
| Provider health | `GET /api/in/provider-health/route.ts` | `/v1/providers` (may exist) | provider, status, latency, circuitState | ✅ | N/A | All | No | Needs verification | Verify/add |
| Historical coverage | `/api/in/historical-data/coverage/` | `/v1/india/historical/status` ✅ EXISTS | covered instruments, date ranges | N/A | ✅ | DB | ✅ | DB not running | Start DB |
| Historical reconciliation | `/api/in/historical-data/reconciliation/` | `/v1/india/historical/reconciliation` ✅ EXISTS | totalCompared, matched, matchRatePct | N/A | ✅ | DB | ✅ | DB not running | Start DB |
| `/scraping/historical` compat route | ScraplingProvider | ❌ MISSING | Same as `/v1/india/historical` | ✅ | ✅ | All | ✅ | **P0: Missing compatibility route** | Add `/scraping/historical` → proxy to `/v1/india/historical` |
| `/scraping/quotes` compat route | ScraplingProvider | ❌ MISSING | Same as `/v1/india/quotes/batch` | ✅ | N/A | All | No | **P0: Missing compatibility route** | Add `/scraping/quotes` → proxy to batch quotes |
| `/scraping/option-chain` compat route | ScraplingProvider | ❌ MISSING | Same as `/v1/india/option-chain` | ✅ | N/A | All | No | **P0: Missing compatibility route** | Add `/scraping/option-chain` → proxy |
| `/scraping/instruments` compat route | ScraplingProvider | ❌ MISSING | Same as `/v1/instruments` | N/A | On-demand | All | ✅ | **P0: Missing compatibility route** | Add `/scraping/instruments` → proxy |

---

## Severity Classification

### P0 — Blocks AlphaForge from using data-service2.0 at all
1. **`/scraping/*` compatibility routes missing** — ScraplingProvider (priority 0 in AlphaForge) cannot reach data-service2.0
2. **MarketEngine uses STUB provider** — live quotes return simulated data, not real Angel One data
3. **Database not running** — all historical data and persistence is blocked
4. **AngelOneAdapter not wired into startup** — broker analytics endpoints return 503

### P1 — Required for full production functionality
5. **No batch quotes endpoint** — scanner/daily-picks need 170 symbols at once
6. **No WebSocket endpoint for India ticks** — worker realtime candle builder needs tick stream
7. **AlphaForge AngelOneProvider/UpstoxProvider/YahooProvider still call providers directly** — these are fallback paths but represent direct provider access

### P2 — Important but has workaround
8. **AlphaForge internal data-service** (`alpha-forge/data-service/`) still exists as a competing service
9. **Upstox OAuth routes** still manage tokens directly in AlphaForge
10. **yahoo-finance2 npm package** still in AlphaForge (needed by YahooProvider fallback)

---

## Resolution Plan

| Gap | Resolution | File(s) | Priority |
|---|---|---|---|
| `/scraping/*` compat routes | Add new router `src/api/compat.py` with `/scraping/historical`, `/scraping/quotes`, `/scraping/option-chain`, `/scraping/instruments` | `src/api/compat.py`, `src/server.py` | P0 |
| MarketEngine STUB → real Angel One | Wire `AngelOneAdapter` into `MarketEngine.__init__` from `app.state.angel_one_adapter` | `src/engines/market_engine.py`, `src/server.py` | P0 |
| AngelOneAdapter startup config | Initialize `AngelOneAdapter` in `lifespan()` using env vars | `src/server.py` | P0 |
| DB not running | Start Docker Compose services | `docker-compose.yml` | P0 |
| Batch quotes endpoint | Add `GET /v1/india/quotes/batch` | `src/api/india.py` | P1 |
| WebSocket India tick stream | Add `/v1/india/stream` WebSocket endpoint with Redis pub/sub | `src/api/india.py` or new `src/api/india_stream.py` | P1 |
| AlphaForge direct providers → data-service2.0 | Update ScraplingProvider to use data-service2.0 URL; disable fallback direct providers | `src/lib/market-data/providers/scrapling.ts` | P1 |
| Remove AlphaForge internal data-service | After migration verified, deprecate/remove `alpha-forge/data-service/` | `alpha-forge/data-service/` | P2 |

---

## Field Semantics Verification

### Response shape: ScraplingProvider expects from `/scraping/historical`
```typescript
interface HistoricalResponse {
  candles: OHLCVCandle[];  // [{time, open, high, low, close, volume}]
  count: number;
}
```

### data-service2.0 `/v1/india/historical` returns
```json
{
  "data": {
    "symbol": "RELIANCE",
    "exchange": "NSE",
    "interval": "1d",
    "candles": [{"time": 1234567890, "open": 2500.0, ...}],
    "count": 100,
    "truncated": false,
    "provider": "angel_one"
  },
  "metadata": {"requestedAt": "...", "provider": "angel_one", ...}
}
```

**Mismatch:** data-service2.0 wraps in `{"data": ..., "metadata": ...}` envelope. ScraplingProvider expects the unwrapped `{candles, count}` shape.

**Fix:** Compat routes can unwrap the envelope when proxying to ScraplingProvider-expected shape.

### Response shape: ScraplingProvider expects from `/scraping/quotes`
```typescript
interface QuotesResponse {
  quotes: Array<MDQuote | null>;
}
```

### data-service2.0 `/v1/india/quotes/{symbol}` returns
```json
{
  "data": {"symbol": "RELIANCE", "ltp": 2500.0, ...},
  "metadata": {...}
}
```

**Mismatch:** Single symbol endpoint, needs batch. Must add batch + unwrapping compat route.

---

## Summary of What WORKS vs. BLOCKED

| Feature | Status | Notes |
|---|---|---|
| Market status API | ✅ WORKS | No provider/DB needed |
| Data quality gate | ✅ WORKS | Already connected |
| Historical API structure | ✅ CODE EXISTS | DB needed; Angel One credentials present |
| Option chain API structure | ✅ CODE EXISTS | Real provider needed (stub currently) |
| Backfill API | ✅ CODE EXISTS | DB needed |
| Gap detection | ✅ CODE EXISTS | DB needed |
| Provider adapters | ✅ ALL EXIST | angel_one, jugaad, openchart, upstox, yahoo, scrapling_nse |
| `/scraping/*` compat routes | ❌ MISSING | P0 blocker |
| Batch quotes | ❌ MISSING | P1 |
| Real live quotes (not stub) | ❌ MISSING | P0 — Angel One not wired |
| Real option chain (not stub) | ❌ MISSING | P0 — stub in MarketEngine |
| Database persistence | ❌ BLOCKED | Docker not running |
| WebSocket tick stream | ❌ MISSING | P1 |
| Broker analytics (PCR, OI) | ❌ 503 | Angel One adapter not in app.state |
