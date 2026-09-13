# REPORT 16 — ALPHAFORGE INTEGRATION CERTIFICATION
**Audit date:** 2026-09-13

---

## Status: ❌ NOT READY — Critical bypasses active

AlphaForge does NOT use DATA-SERVICE as its single market-data authority. Multiple direct provider calls exist in production code paths.

---

## DATA-SERVICE URL Configuration

```
DATA_SERVICE_URL=http://localhost:8200
```

This is correctly configured in AlphaForge's `.env.local`. When set, `ScraplingProvider` uses this URL as Priority 0 in the Indian market data chain.

---

## Indian Market Integration

| Path | Status | Notes |
|---|---|---|
| `DataServiceClient.market.*` → ScraplingProvider → DATA-SERVICE | 🟡 Correct architecture | When DATA_SERVICE_URL is set |
| `/api/v1/market/*` routes | 🟡 All use DataServiceClient | Correct |
| `/api/in/*` routes | ⚠️ Mixed — some use registry, some use direct angel/yahoo | Partially migrated |
| Worker scraping-tick-listener | ✅ Listens to DATA-SERVICE Redis pub/sub | Correct |
| Angel One fallback (when DATA_SERVICE unavailable) | 🟡 Acceptable fallback | Direct call, not bypass |
| Yahoo fallback | 🟡 Acceptable fallback | Direct call via chain |

---

## Crypto Integration

| Path | Status | Notes |
|---|---|---|
| `src/services/binance/rest.ts` calls `api.binance.com` directly | 🔴 **P0 BYPASS** | Used by broker/binance/adapter |
| `src/services/binance/futures.ts` calls `fapi.binance.com` directly | 🔴 **P0 BYPASS** | Used everywhere |
| `src/services/binance/ws.ts` connects `wss://stream.binance.com` directly | 🔴 **P0 BYPASS** | Used by useBinanceTickers hook |
| `src/services/brokers/delta/rest.ts` calls `api.india.delta.exchange` directly | 🔴 **P0 BYPASS** | Active broker |
| `src/services/brokers/delta/ws.ts` connects delta WS directly | 🔴 **P0 BYPASS** | Client-side |
| `src/services/deribit/rest.ts` calls `www.deribit.com/api/v2` directly | 🔴 **P0 BYPASS** | Used by options feature |

---

## Credential Integration

### Frontend-saved broker credentials flow

AlphaForge allows users to save broker credentials (Angel One, Upstox) via the Settings UI. The flow:

```
Frontend Settings UI
→ /api/settings/credentials (POST, encrypted with AES-256-GCM ENCRYPTION_KEY)
→ Prisma → PostgreSQL (alpha-forge DB, NOT data-service DB)
→ Per-request: getAngelConfigForRequest() / getUpstoxConfig()
→ AlphaForge providers (not DATA-SERVICE providers)
```

DATA-SERVICE credential flow:
```
DATA-SERVICE .env.local / env vars
→ src/core/settings.py Settings model
→ AngelOneAdapter / UpstoxAdapter
```

**These are two completely separate credential stores.** Frontend-saved credentials in AlphaForge's Prisma DB are NOT accessible to DATA-SERVICE. DATA-SERVICE reads ONLY from its own environment variables.

**This is DS2-RCA-011 — P1 severity.** If a user saves Angel One credentials via the AlphaForge UI, DATA-SERVICE cannot authenticate with them. DATA-SERVICE will use whatever is in its own `.env` / environment.

The `worker-credentials.ts` in AlphaForge partially addresses this for the worker: `getWorkerAngelCredentials()` can load DB credentials for a specific user. But DATA-SERVICE is a separate process with no access to AlphaForge's DB.

---

## E2E Test Checklist

| AlphaForge Flow | DATA-SERVICE Used | Direct Bypass | Status |
|---|---|---|---|
| Market dashboard (Indian) | 🟡 via ScraplingProvider | Angel/Yahoo fallback | ⛔ BLOCKED (no service running) |
| Live quotes (Indian) | 🟡 | Some direct | ⛔ BLOCKED |
| F&O universe | 🟡 | — | ⛔ BLOCKED |
| Option chain | 🟡 | Some direct | ⛔ BLOCKED |
| Historical charts (Indian) | 🟡 | Some direct | ⛔ BLOCKED |
| Crypto dashboard | ❌ NO — Delta/Binance direct | Delta direct | ⛔ BLOCKED |
| Crypto futures analytics | ❌ NO — Binance direct | Binance direct | ⛔ BLOCKED |
| AI signals (crypto) | ❌ NO — broker direct | Broker direct | ⛔ BLOCKED |
| Strategy lab backtests | ❌ NO — broker direct | Broker direct | ⛔ BLOCKED |
| Paper trading | ❌ NO — broker direct | Broker direct | ⛔ BLOCKED |
| Options analytics | ❌ NO — Deribit direct | Deribit direct | ⛔ BLOCKED |
