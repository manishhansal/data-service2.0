# DATA-SERVICE 2.0 — Complete Technical Guide

> **Version:** 2.0.0 · **Port:** 8200 · **Language:** Python 3.11+  
> This is the authoritative internal reference for DATA-SERVICE 2.0.  
> For the AlphaForge integration contract see [`ALPHAFORGE_DATA_REQUIREMENTS.md`](../ALPHAFORGE_DATA_REQUIREMENTS.md).  
> For production-readiness certification see [`PRODUCTION_CERTIFICATION.md`](../PRODUCTION_CERTIFICATION.md).

---

## Table of Contents

1. [What Is DATA-SERVICE 2.0?](#1-what-is-data-service-20)
2. [Repository Layout](#2-repository-layout)
3. [Environment File System](#3-environment-file-system)
4. [Configuration Reference](#4-configuration-reference)
5. [Architecture Deep-Dive](#5-architecture-deep-dive)
6. [Market Data Coverage](#6-market-data-coverage)
7. [Data Pipeline](#7-data-pipeline)
8. [API Reference](#8-api-reference)
9. [Authentication and Security](#9-authentication-and-security)
10. [Data Quality System](#10-data-quality-system)
11. [Cache Hierarchy](#11-cache-hierarchy)
12. [Circuit Breaker and Rate Limiting](#12-circuit-breaker-and-rate-limiting)
13. [WebSocket Streaming](#13-websocket-streaming)
14. [Data Provenance and Lineage](#14-data-provenance-and-lineage)
15. [Null Semantics — Critical Rules](#15-null-semantics--critical-rules)
16. [Indian Market Session Management](#16-indian-market-session-management)
17. [Historical Data and Gap Recovery](#17-historical-data-and-gap-recovery)
18. [Crypto Market Data](#18-crypto-market-data)
19. [Provider Adapter Reference](#19-provider-adapter-reference)
20. [Observability](#20-observability)
21. [Local Development Workflow](#21-local-development-workflow)
22. [Testing Guide](#22-testing-guide)
23. [Deployment Guide](#23-deployment-guide)
24. [Troubleshooting](#24-troubleshooting)
25. [Glossary](#25-glossary)

---

## 1. What Is DATA-SERVICE 2.0?

DATA-SERVICE 2.0 is a **standalone, production-grade market data platform** that acts as the **single market-data authority** for AlphaForge and all future consumers. It centralises every aspect of market data acquisition, normalisation, validation, caching, persistence, and streaming behind a single versioned REST + WebSocket API.

### Core Mandate

- **No consumer bypass**: AlphaForge (and every other consumer) may **not** call any external market-data provider directly. Every request flows through this service.
- **No fabrication**: If the provider does not supply a field, the field is `null`. Zero is never substituted for a missing `oi`, `iv`, `bid`, or `ask` value.
- **Correctness over availability**: A dataset that fails validation is rejected, never served.
- **3m interval ban**: The `3m` interval is permanently unsupported for Indian market data at every processing layer. Binance crypto data natively supports `3m` and is explicitly exempt.

### What It Provides

| Category | What is served |
|---|---|
| Indian live data | NSE live quotes, ticks, option chains, market session state |
| Indian historical data | OHLCV candles (9 intervals), backfill, gap detection and recovery |
| Broker analytics | Angel One PCR, OI buildup, gainers/losers |
| Crypto live data | Binance spot ticks, perpetual futures (mark price, funding, OI, L/S) |
| Crypto historical data | Binance OHLCV klines |
| Crypto options | Deribit options analytics (IV, OI, max pain, PCR) |
| Data quality | DataConfidenceScore (0–95), DataQualityGate (5 conditions), quality alerts |
| Provenance | Immutable per-observation lineage, trade forensics |
| Streaming | WebSocket tick fan-out to consumers |

---

## 2. Repository Layout

```
data-service2.0/
├── src/
│   ├── api/              REST + WebSocket endpoint handlers
│   │   ├── india.py      Indian market endpoints
│   │   ├── crypto.py     Binance crypto endpoints
│   │   ├── deribit.py    Deribit options endpoints
│   │   ├── quality.py    Quality gate endpoints
│   │   ├── streaming.py  WebSocket /v1/stream/ticks
│   │   ├── analytics.py  Provider health and platform analytics
│   │   ├── provenance.py Lineage and forensics endpoints
│   │   ├── contract.py   DataParityContract endpoint
│   │   ├── replay.py     Replay mode session endpoints
│   │   ├── instruments.py Instrument master endpoints
│   │   ├── internal.py   Internal service-to-service endpoints
│   │   └── errors.py     Canonical error envelope (ErrorCode enum)
│   │
│   ├── auth/             Authentication and credential management
│   │   ├── consumer_auth.py   API key + JWT bearer auth dependency
│   │   ├── angel_one_jwt.py   Angel One JWT store and rotation scheduler
│   │   └── upstox_oauth.py    Upstox OAuth 401 refresh handler
│   │
│   ├── backtest/         Backtest mode utilities
│   │   └── point_in_time.py   Look-ahead bias prevention filter
│   │
│   ├── cache/            Multi-level cache subsystem
│   │   ├── l1_cache.py   In-process LRU cache
│   │   ├── redis_client.py    Redis async client + pool
│   │   ├── cache_manager.py   Three-level hierarchy + request coalescing
│   │   └── event_bus.py  Redis Streams Event Bus
│   │
│   ├── core/             Shared schemas, settings, validators
│   │   ├── settings.py   Pydantic BaseSettings with APP_ENV adapter
│   │   ├── normaliser.py 14-step validation pipeline orchestrator
│   │   ├── shell_safety.py    Log-injection and shell-injection guards
│   │   ├── schemas/      Canonical Pydantic models
│   │   │   ├── instrument.py
│   │   │   ├── provider.py
│   │   │   ├── parity.py     DataParityContract
│   │   │   └── provenance.py DataProvenance + ProvenanceFactory
│   │   └── validators/   Per-step pipeline validators
│   │
│   ├── db/               Database layer
│   │   ├── engine.py     SQLAlchemy async engine factory
│   │   └── migrations/   Alembic migration scripts
│   │
│   ├── engines/          Core business logic engines
│   │   ├── market_engine.py          Indian live data
│   │   ├── historical_engine.py      OHLCV backfill + reconciliation
│   │   ├── gap_recovery.py           Gap detection + state machine
│   │   ├── streaming_engine.py       Tick publisher + dedup
│   │   ├── quality_engine.py         DataConfidenceScore + DataQualityGate
│   │   ├── instrument_master.py      Instrument lifecycle
│   │   ├── market_session.py         NSE session phase state machine
│   │   ├── holiday_calendar.py       NSE holiday management
│   │   ├── fno_universe.py           F&O eligible universe
│   │   └── options_overview.py       Deribit OptionsOverview computation
│   │
│   ├── forensics/        Trade forensics recording
│   │   └── trade_forensics.py
│   │
│   ├── middleware/       ASGI middleware
│   │   ├── credential_stripper.py    Redacts secrets from responses
│   │   ├── rate_limiter.py           Sliding-window per-consumer rate limit
│   │   └── cors_middleware.py        CORS allowlist (no wildcard)
│   │
│   ├── monitors/         Runtime monitors
│   │   ├── clock_skew_monitor.py     NTP skew detection
│   │   └── quality_alerter.py        Quality degradation alerts
│   │
│   ├── observability/    Logging and tracing
│   │   ├── logging.py    structlog JSON configuration
│   │   └── tracing.py    OpenTelemetry SDK setup
│   │
│   ├── providers/        External provider adapters
│   │   ├── capability_matrix.py      Provider × capability routing table
│   │   ├── circuit_breaker.py        Per-provider circuit breaker
│   │   ├── rate_limiter.py           Token-bucket rate limiter
│   │   ├── provider_gateway.py       Request dispatch + fallback
│   │   ├── adapters/
│   │   │   ├── angel_one.py          Angel One SmartAPI adapter
│   │   │   ├── upstox.py             Upstox V2/V3 adapter
│   │   │   ├── scrapling_nse.py      Scrapling/NSE credential-free adapter
│   │   │   ├── jugaad_data.py        Jugaad-data F&O EOD adapter
│   │   │   ├── openchart.py          OpenChart OHLCV adapter
│   │   │   ├── yahoo_finance.py      Yahoo Finance EOD fallback
│   │   │   ├── binance_rest.py       Binance REST client
│   │   │   └── deribit_client.py     Deribit REST client
│   │   ├── binance_normaliser.py     Binance OHLCV normaliser + Pydantic model
│   │   └── binance_persistence.py   Binance OHLCV upsert layer
│   │
│   ├── replay/           Replay mode engine
│   │   └── replay_engine.py
│   │
│   ├── stores/           Persistence stores
│   │   └── lineage_store.py  In-memory LRU + PostgreSQL lineage store
│   │
│   └── server.py         FastAPI app factory + lifespan + router registration
│
├── tests/
│   ├── unit/             Per-module unit tests (95+ test files)
│   ├── integration/      Integration tests (circuit breaker, etc.)
│   ├── property/         Hypothesis property-based tests
│   ├── performance/      Load benchmarks (pytest-based, mocked I/O)
│   ├── mocks/            Shared mock classes and factory functions
│   └── fixtures/         Shared pytest fixtures (conftest.py)
│
├── alembic/              Database migration framework
│   ├── env.py            Alembic env using async engine
│   └── versions/         Migration scripts
│
├── docker/               Docker Compose override files and scripts
├── scripts/              Admin scripts (promote_timescaledb.sql, etc.)
├── docs/                 Extended documentation
│   └── DATA_SERVICE_GUIDE.md  ← this file
│
├── .env.example          Annotated variable reference (safe to commit)
├── .env.local            Local development overrides (safe to commit with placeholders)
├── .env.production       Production environment (NEVER COMMIT)
├── .env                  Active overrides — not tracked by git
├── docker-compose.yml    Docker Compose definition
├── locustfile.py         Locust manual load testing scenarios
├── pyproject.toml        Python packaging and dependency configuration
├── alembic.ini           Alembic configuration
├── Dockerfile            Multi-stage production image
├── ALPHAFORGE_DATA_REQUIREMENTS.md  AlphaForge integration contract
├── PRODUCTION_CERTIFICATION.md      Production readiness sign-off
└── README.md             Quick-start guide
```

---

## 3. Environment File System

### The APP_ENV Adapter

`settings.py` automatically selects the correct `.env.*` file based on the `APP_ENV` environment variable:

| `APP_ENV` | File loaded | When to use |
|---|---|---|
| `local` | `.env.local` | Developer workstation, hot-reload server |
| `production` | `.env.production` | Deployed production containers |
| `staging` | `.env.staging` | Staging / UAT environment |
| *(unset)* | `.env` | Legacy or CI fallback |

The resolver is in `src/core/settings.py → _resolve_env_file()`. It searches in this order:
1. `.env.{APP_ENV}` (if `APP_ENV` is set and the file exists)
2. `.env` (always the final fallback)

### File Inventory

| File | Committed? | Purpose |
|---|---|---|
| `.env.example` | ✅ Yes | Annotated reference with all variables and defaults |
| `.env.local` | ✅ Yes (placeholder values) | Development defaults — replace before running |
| `.env.production` | ❌ Never | Production secrets — inject via CI/CD |
| `.env.staging` | ❌ Never | Staging secrets — create from `.env.example` |
| `.env` | ❌ Never | Personal overrides, not tracked |

### Setting APP_ENV

**Shell / Uvicorn:**
```bash
APP_ENV=local uvicorn src.server:app --reload
APP_ENV=production uvicorn src.server:app
```

**Docker Compose:**
```yaml
# docker-compose.yml
services:
  api:
    environment:
      APP_ENV: production
    env_file:
      - .env.production
```

**pytest / CI:**
```bash
APP_ENV=local pytest tests/unit/ -q
```

### Precedence

When the same variable appears in multiple places, this is the resolution order (highest wins):

1. Actual environment variable (e.g., `export DATABASE_URL=...` in the shell)
2. The `.env.*` file selected by `APP_ENV`
3. Pydantic field default values

This means you can always override a single variable without editing any file:
```bash
APP_ENV=local DATABASE_URL=postgresql+asyncpg://custom@host/db uvicorn src.server:app --reload
```

---

## 4. Configuration Reference

### Full Variable Table

All variables, their defaults, and which environment they are required in:

#### Server

| Variable | Default | Local | Prod | Description |
|---|---|---|---|---|
| `DATA_SERVICE_PORT` | `8200` | 8200 | 8200 | HTTP listening port |
| `ENVIRONMENT` | `development` | `development` | `production` | Controls `/docs` exposure and log format |
| `UVICORN_WORKERS` | `4` | `1` | `4` | Uvicorn worker processes |

#### Infrastructure

| Variable | Default | Local | Prod | Description |
|---|---|---|---|---|
| `REDIS_URL` | `redis://redis:6379/0` | `redis://localhost:6379/0` | `redis://redis:6379/0` | Redis connection string |
| `DATABASE_URL` | — | `postgresql+asyncpg://mds_user:localdevpassword@localhost:5432/mds` | **REQUIRED** | PostgreSQL async URL |
| `POSTGRES_DB` | `mds` | `mds` | `mds` | Database name |
| `POSTGRES_USER` | `mds_user` | `mds_user` | `mds_user` | PostgreSQL username |
| `POSTGRES_PASSWORD` | — | `localdevpassword` | **REQUIRED** | PostgreSQL password |
| `REDIS_MAX_MEMORY` | `512mb` | `256mb` | `1gb` | Redis `maxmemory` |

#### Circuit Breaker

| Variable | Default | Local | Prod | Description |
|---|---|---|---|---|
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | `3` | `5` | Failures before circuit opens (1–100) |
| `CIRCUIT_BREAKER_RECOVERY_WINDOW_SEC` | `60` | `30` | `60` | OPEN→HALF_OPEN delay in seconds (1–3600) |

#### Cache

| Variable | Default | Local | Prod | Description |
|---|---|---|---|---|
| `CACHE_L1_MAX_ENTRIES` | `10000` | `1000` | `10000` | L1 in-process LRU capacity |

#### Provider Gateway

| Variable | Default | Local | Prod | Description |
|---|---|---|---|---|
| `PROVIDER_QUEUE_MAX_DEPTH` | `100` | `50` | `100` | Max queued requests per provider×capability (1–10000) |

#### Gap Recovery

| Variable | Default | Local | Prod | Description |
|---|---|---|---|---|
| `GAP_RECOVERY_MAX_ATTEMPTS` | `5` | `2` | `5` | Max retry attempts before marking gap EXHAUSTED (1–10) |

#### Backfill Chunk Sizes

| Variable | Default (Local) | Default (Prod) | Description |
|---|---|---|---|
| `BACKFILL_CHUNK_ANGEL_1M_DAYS` | `7` | `30` | Angel One 1m — calendar days per chunk |
| `BACKFILL_CHUNK_ANGEL_5M_15M_DAYS` | `14` | `90` | Angel One 5m/15m |
| `BACKFILL_CHUNK_UPSTOX_1M_DAYS` | `3` | `7` | Upstox 1m |
| `BACKFILL_CHUNK_UPSTOX_5M_15M_DAYS` | `7` | `30` | Upstox 5m/15m |
| `BACKFILL_CHUNK_UPSTOX_1D_DAYS` | `90` | `365` | Upstox 1d |

#### Observability

| Variable | Default | Local | Prod | Description |
|---|---|---|---|---|
| `OTEL_EXPORTER` | `noop` | `noop` | `otlp` | `noop` \| `jaeger` \| `otlp` |
| `LOG_LEVEL` | `INFO` | `DEBUG` | `INFO` | Log verbosity |

#### Security

| Variable | Default | Local | Prod | Description |
|---|---|---|---|---|
| `CORS_ALLOWED_ORIGINS` | — | `http://localhost:3000,...` | **REQUIRED** | Comma-separated origin allowlist. No wildcards. |
| `SECRETS_BACKEND` | `env` | `env` | `env` | `env` \| `vault` \| `aws_secrets` |
| `JWT_SECRET` | *(dev placeholder)* | Replace before use | **REQUIRED** | HS256 signing secret — minimum 32 chars |
| `JWT_EXPIRY_SECONDS` | `3600` | `86400` | `3600` | JWT token lifetime in seconds |
| `CONSUMER_API_KEYS` | — | `dev-key-local-1,...` | **REQUIRED** | Comma-separated consumer API keys |

#### Provider Credentials

| Variable | Required for | Description |
|---|---|---|
| `ANGEL_ONE_API_KEY` | Indian live data | Angel One SmartAPI key |
| `ANGEL_ONE_CLIENT_ID` | Indian live data | Angel One client ID |
| `ANGEL_ONE_TOTP_SECRET` | Indian live data | Base32-encoded TOTP seed |
| `UPSTOX_API_KEY` | Upstox data | Upstox V2/V3 API key |
| `UPSTOX_API_SECRET` | Upstox data | Upstox API secret |
| `UPSTOX_REDIRECT_URI` | Upstox OAuth | OAuth callback URI (must match developer console) |

---

## 5. Architecture Deep-Dive

### Request Flow

```
Consumer (AlphaForge, backtest engine, dashboard)
    │
    │  HTTP/WS request with X-API-Key or Bearer token
    ▼
ASGI Stack (Uvicorn → FastAPI)
    │  Middleware chain (order matters):
    │  1. CredentialStripperMiddleware  — redacts secrets from all responses
    │  2. CORSMiddleware               — enforces CORS allowlist
    │  3. RateLimitMiddleware          — 100 req/60s per consumer IP
    ▼
Route handler (src/api/*.py)
    │  validates input, checks auth via ConsumerAuthDependency
    ▼
Core Engine (MarketEngine / HistoricalEngine / StreamingEngine / QualityEngine)
    │  checks L1 cache → L2 Redis → L3 PostgreSQL
    │  on cache miss: calls Provider Gateway
    ▼
Provider Gateway (src/providers/provider_gateway.py)
    │  consults Capability Matrix
    │  checks circuit-breaker state (Redis key: mds:cb:{provider}:{capability})
    │  applies token-bucket rate limit
    ▼
Provider Adapter (Angel One / Upstox / Scrapling / Binance / Deribit / ...)
    │  raw HTTP/WS call to external provider
    ▼
Validation Pipeline (src/core/normaliser.py — 14 steps)
    │  rejects invalid data; attaches provenance; scores quality
    ▼
Cache population (L1 + L2)
    │  writes to Redis Streams Event Bus for streaming consumers
    ▼
Response serialisation
    │  canonical success envelope: {data, metadata}
    ▼
Consumer
```

### Service Topology (Docker Compose)

```
┌─────────────────────────────────────────────────────────────────┐
│  Docker network: data-service-net                               │
│                                                                 │
│  api (port 8200)   ←→   redis (6379)   ←→   postgres (5432)   │
│       │                      ↑                      ↑          │
│   worker ──────────────────────────────────────────┘          │
│   scheduler ────────────────────────────────────────┘          │
└─────────────────────────────────────────────────────────────────┘
```

| Service | Role | Key processes |
|---|---|---|
| `api` | REST + WebSocket server | 4 Uvicorn workers, all API routes, WebSocket fan-out |
| `worker` | Background data jobs | Backfill, gap recovery, cross-provider reconciliation |
| `scheduler` | Cron jobs | F&O universe refresh at 08:45 IST, Angel One JWT rotation at 23:55 IST |
| `redis` | Shared state | L2 cache (mds: namespace), Event Bus (Redis Streams), circuit-breaker state, backfill checkpoints |
| `postgres` | Persistent storage | OHLCV candles, provenance records, instruments, gaps, incidents |

---

## 6. Market Data Coverage

### Indian Markets

| Data type | Endpoint | Providers | Notes |
|---|---|---|---|
| Live quote | `GET /v1/india/quotes/{symbol}` | Scrapling/NSE, Angel One SmartAPI | 500ms p99 publish latency |
| Batch live quotes | `GET /v1/india/quotes?symbols=...` | Same | Up to 50 symbols per request |
| Option chain | `GET /v1/india/option-chain` | Scrapling/NSE, Angel One | IV/Greeks/bid/ask are null when absent |
| Historical OHLCV | `GET /v1/india/historical` | Angel One, Upstox, OpenChart, Jugaad-data | Max 10K candles per response |
| Market session | `GET /v1/india/market/status` | Internal (IST clock + holiday calendar) | 6 session phases |
| Gap records | `GET /v1/india/historical/gaps` | Internal | PENDING/RECOVERING/RECOVERED/EXHAUSTED |
| Reconciliation | `GET /v1/india/historical/reconciliation` | Internal | CONFIRMED/MINOR_DISCREPANCY/MAJOR_DISCREPANCY |
| Broker PCR | `GET /v1/india/broker-analytics/pcr` | Angel One SmartAPI | AlphaForge-specific |
| OI buildup | `GET /v1/india/broker-analytics/oi-buildup` | Angel One SmartAPI | |
| Gainers/losers | `GET /v1/india/broker-analytics/gainers-losers` | Angel One SmartAPI | |

### Supported Intervals (Indian)

`1m`, `5m`, `10m`, `15m`, `30m`, `1h`, `1d`, `1w`, `1M`

> ⛔ `3m` is **permanently banned** for all Indian market data endpoints at every layer.

### Crypto Markets

| Data type | Endpoint | Provider |
|---|---|---|
| Binance OHLCV klines | `GET /v1/crypto/klines/{symbol}` | Binance REST |
| Binance ticker | `GET /v1/crypto/{symbol}/ticker` | Binance REST |
| Binance 24h stats | `GET /v1/crypto/{symbol}/stats` | Binance REST |
| Binance futures overview | `GET /v1/crypto/futures/overview` | Binance FAPI |
| Deribit options overview | `GET /v1/deribit/{currency}/overview` | Deribit REST |
| Deribit ticker | `GET /v1/deribit/ticker/{instrument_name}` | Deribit REST |
| Deribit OHLCV | `GET /v1/deribit/ohlcv/{instrument_name}` | Deribit REST |

### Binance Intervals

`1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d`

> ✅ `3m` is valid for Binance crypto data. The ban applies exclusively to Indian market data.

---

## 7. Data Pipeline

Every dataset traverses 14 ordered steps. A failure at any step stops processing — the dataset does not advance.

```
Step 1   Raw response receipt
          ↓ (reject if HTTP error)
Step 2   JSON schema validation  →  DataIncident on failure
          ↓ (reject if invalid JSON schema)
Step 3   Normaliser → canonical Pydantic schema
          Strict null semantics enforced here (oi, iv, Greeks, bid/ask)
          MetricTag annotation (OBSERVED / DERIVED / MODELLED)
          ↓
Step 4   Timestamp normalisation → UTC epoch ms
          Exchange timezone applied (IST for Indian, UTC for crypto)
          ↓
Step 5   Semantic validation
          oi ≠ tradedValue, iv ≥ 0 (or null), |delta| ≤ 1, high ≥ max(open,close)
          ↓
Step 6   Duplicate detection
          Hash: instrumentId:eventTimeMs:source:ltp:volume
          Duplicates published with isDuplicate:true (not dropped)
          ↓
Step 7   Gap detection
          Compares received timestamps against expected sequence
          Generates DataGapEvent with severity LOW/MEDIUM/HIGH
          ↓
Step 8   Freshness classification
          FRESH / AGING / STALE / EXPIRED per instrument tier and session phase
          ↓
Step 9   Cross-provider reconciliation
          CONFIRMED ≤ 0.5%  |  MINOR_DISCREPANCY 0.5–2.0%  |  MAJOR_DISCREPANCY > 2.0%
          ↓
Step 10  Quality Engine scoring
          DataConfidenceScore 0–95 (never 100)
          score < 60 → POOR_QUALITY flag (persisted but not served)
          score < 30 → BLOCKED (signalEngineAllowed: false)
          ↓
Step 11  Canonical dataset output
          ↓
Step 12  Cache population (L1 + L2 Redis)
          ↓
Step 13  PostgreSQL persistence + provenance record write
          ↓
Step 14  API / Event Bus delivery
```

---

## 8. API Reference

### Response Envelopes

All successful responses:
```json
{
  "data": { ... },
  "metadata": {
    "requestedAt":    "2026-01-15T09:15:00.000Z",
    "dataAsOf":       "2026-01-15T09:14:59.750Z",
    "dataSourceType": "LIVE",
    "provider":       "angel_one",
    "provenance":     { "dataObservationId": "...", "source": "...", ... },
    "marketStatus":   "REGULAR",
    "quality":        { "score": 87, "grade": "VALID" }
  }
}
```

`dataSourceType` values: `LIVE` | `HISTORICAL` | `DERIVED` | `CACHED`

All error responses:
```json
{
  "error": {
    "code":         "INTERVAL_NOT_SUPPORTED",
    "message":      "interval 3m is permanently unsupported",
    "provider":     null,
    "retryAfterMs": null,
    "requestId":    "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

### HTTP Status Codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 201 | Resource created (POST provenance, POST replay session) |
| 400 | Invalid parameter, unsupported interval, malformed date |
| 401 | Missing, invalid, or expired credentials |
| 403 | Authenticated but not authorised |
| 404 | Resource not found |
| 405 | Method not allowed (e.g., modifying a provenance record) |
| 422 | Pydantic request body validation failure |
| 429 | Rate limit exceeded |
| 502 | All provider fallbacks exhausted |
| 503 | Platform unavailable (Redis / PostgreSQL not ready) |

### Complete Endpoint Catalog

#### Health and Platform

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/health/live` | Liveness probe — always 200, never blocks |
| `GET` | `/v1/health/ready` | Readiness probe — 200 when Redis+PG respond within 2s |
| `GET` | `/v1/health/data` | Session state, freshness stats, circuit-breaker states, clock skew |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/v1/monitoring/stats` | Rolling 3600s P50/P99/successRate/requestCount per endpoint |

#### Authentication

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/auth/token?api_key=` | Exchange API key for JWT bearer token |

#### Instruments

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/instruments` | List with filters: exchange, instrumentType, underlying, segment, expiry |
| `GET` | `/v1/instruments/{instrumentId}` | Single instrument. Add `?include=providerTokens` for token mappings |
| `GET` | `/v1/instruments/fno-universe` | Current F&O eligible universe snapshot |
| `GET` | `/v1/instruments/fno-universe/history` | Past snapshots (paginated, 100/page max) |

#### Indian Market — Live

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/india/quotes/{symbol}` | Live quote. `exchange` param (default NSE) |
| `GET` | `/v1/india/option-chain` | Option chain. `underlying` required, `expiry` optional |
| `GET` | `/v1/india/market/status` | Session phase, next change, holidays, calendarStatus |

#### Indian Market — Historical

| Method | Path | Key params | Notes |
|---|---|---|---|
| `GET` | `/v1/india/historical` | symbol, interval, from, to | Max 10K records; interval=3m → 400 |
| `GET` | `/v1/india/historical/status` | — | Coverage stats, gap summary |
| `GET` | `/v1/india/historical/gaps` | symbol, interval, status, limit | Default limit 100, max 1000 |
| `GET` | `/v1/india/historical/reconciliation` | — | Cross-provider stats |
| `POST` | `/v1/india/historical/backfill` | body: symbol, interval, from_date, to_date | Async job, returns job_id |
| `GET` | `/v1/india/historical/backfill/{job_id}` | — | Poll backfill status |

#### Broker Analytics

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/india/broker-analytics/pcr` | Angel One put/call ratio |
| `GET` | `/v1/india/broker-analytics/oi-buildup` | OI buildup analysis |
| `GET` | `/v1/india/broker-analytics/gainers-losers` | Top OI/price movers |

#### Crypto — Binance

| Method | Path | Key params |
|---|---|---|
| `GET` | `/v1/crypto/{symbol}/ohlcv` | interval (required), limit (1–1000), from, to |
| `GET` | `/v1/crypto/{symbol}/ticker` | — |
| `GET` | `/v1/crypto/{symbol}/stats` | — |
| `GET` | `/v1/crypto/exchange-info` | symbol (optional filter) |
| `GET` | `/v1/crypto/futures/overview` | — |

#### Crypto — Deribit

| Method | Path | Key params |
|---|---|---|
| `GET` | `/v1/deribit/{currency}/overview` | currency: BTC, ETH, SOL |
| `GET` | `/v1/deribit/{currency}/instruments` | kind (default: option) |
| `GET` | `/v1/deribit/{currency}/index-price` | — |
| `GET` | `/v1/deribit/ticker/{instrument_name}` | — |
| `GET` | `/v1/deribit/ohlcv/{instrument_name}` | resolution (required), start_ts, end_ts |

#### Data Quality

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/quality/evaluate` | Full gate evaluation. Body: observation dict. Query: min_confidence_score |
| `GET` | `/v1/quality/score` | Compute score from components: freshness, completeness, provider_healthy, timestamp_valid, agreement |

#### Streaming

| Method | Path | Description |
|---|---|---|
| `WS` | `/v1/stream/ticks` | Subscribe/unsubscribe. Heartbeat every 10s. Max 500 concurrent streams. |
| `GET` | `/v1/stream/status` | subscribedSymbols, ticksPublished, brokerConnections |

#### Analytics

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/analytics/providers` | List all providers with health summary |
| `GET` | `/v1/analytics/providers/{provider_id}` | Detailed metrics for one provider |
| `GET` | `/v1/analytics/quality` | Aggregate DataConfidenceScore distribution |
| `GET` | `/v1/analytics/health` | Overall platform health (200 healthy, 503 degraded) |
| `GET` | `/v1/providers/health` | Per-provider per-capability health status |

#### Lineage and Provenance

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/provenance/{observation_id}` | Full provenance record within 500ms |
| `GET` | `/v1/provenance/instrument/{instrument_id}` | Most recent N records (?limit=1–1000) |
| `POST` | `/v1/provenance` | Create a new provenance record (internal) |
| `GET` | `/v1/lineage/trade/{trade_id}` | Trade forensics record |
| `GET` | `/v1/lineage/strategy/{strategy_id}` | Forensics records by strategy (?limit) |
| `GET` | `/v1/lineage/instrument/{instrument_id}` | Forensics records by instrument (?limit) |

#### Data Parity Contract

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/contract` | DataParityContract — read-only |

#### Replay Mode

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/replay/sessions` | Create a replay session from tick data |
| `GET` | `/v1/replay/sessions/{session_id}/status` | Poll session status |

#### Internal

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/internal/dataset-ready` | Publish a dataset-ready event to Redis Streams |

---

## 9. Authentication and Security

### Authentication Methods

#### Method 1 — API Key

```http
X-API-KEY: key-alphaforge-prod-abcdef123456
```

Keys are configured via `CONSUMER_API_KEYS` (comma-separated). Presented keys are checked against the allowlist in O(1).

#### Method 2 — JWT Bearer Token

```http
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

JWTs are issued at `GET /v1/auth/token?api_key=<key>`. They are signed with HS256 using `JWT_SECRET` and contain:
- `sub` — the consumer API key that was exchanged
- `iat` — issued-at timestamp
- `exp` — expiry timestamp (`iat + JWT_EXPIRY_SECONDS`)
- `scopes` — optional list of granted scopes (empty by default)

#### Combined Dependency

`ConsumerAuthDependency` checks in this order:
1. If `X-API-KEY` header is present → validate against allowlist
2. If `Authorization: Bearer` header is present → validate JWT
3. If neither → 401 UNAUTHORIZED

API key takes precedence when both headers are supplied.

### Credential Stripping

`CredentialStripperMiddleware` processes every outgoing JSON response and recursively redacts any field whose key contains `key`, `token`, `secret`, `password`, `apikey`, `api_key`, `credential`, `authorization`, or `auth`. Redacted values are replaced with `"[REDACTED]"`.

This middleware runs **after** CORS and **before** rate limiting in the middleware stack.

### Rate Limiting

`RateLimitMiddleware` enforces a sliding-window rate limit per consumer IP:
- Default: 100 requests per 60 seconds
- Headers added to every response: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`
- On limit exceeded: HTTP 429 with `{"error": {"code": "RATE_LIMIT_EXCEEDED", "retryAfterMs": N}}`
- Exempt paths: `/v1/health/live`, `/metrics`

### CORS

`CORSMiddleware` is configured from `CORS_ALLOWED_ORIGINS`. Wildcard `*` is permanently prohibited by a Pydantic validator in `CorsConfig`. An origin not in the allowlist receives no `Access-Control-Allow-Origin` header.

### Angel One JWT Rotation

The scheduler runs `AngelOneJwtRotator` at **23:55 IST daily**:
1. Generates a TOTP code using `ANGEL_ONE_TOTP_SECRET`
2. POSTs to Angel One SmartAPI login endpoint
3. Stores the new JWT in `AngelOneJwtStore` (in-memory + Redis)
4. Retries up to 3 times at 60-second intervals on failure
5. Raises a `CRITICAL` alert if all retries are exhausted

### Upstox OAuth 401 Refresh

`UpstoxOAuthRefreshHandler.with_retry()` wraps every Upstox API call:
1. On HTTP 401, calls `refresh()` which POSTs to the Upstox token endpoint
2. Retries the original request with the new access token
3. If refresh fails, marks the Upstox provider as unavailable and returns 502

---

## 10. Data Quality System

### DataConfidenceScore

Every dataset gets a score in `[0, 95]`. The formula weights five components:

| Component | Weight | Max contribution | Scoring |
|---|---|---|---|
| Freshness | 35% | 35 | FRESH=35, AGING=25, STALE=10, EXPIRED=0, UNKNOWN=5 |
| Completeness | 25% | 25 | `int(completeness_percent × 0.25)` |
| Provider health | 20% | 20 | Healthy=20, Unhealthy=0 |
| Timestamp validity | 10% | 10 | Valid=10, Invalid=0 |
| Cross-source agreement | 10% | 10 | `int(agreement × 10)` |

**Broken sequence integrity penalty**: `score = int(score × 0.8)` applied before the 95 cap.

**The 95 cap is intentional and permanent** — it reflects inherent market-data uncertainty.

### Quality Grades

| Grade | Score range | `signalEngineAllowed` |
|---|---|---|
| HIGH | ≥ 80 | `true` (when all gate conditions pass) |
| MEDIUM | 50–79 | Depends on gate conditions |
| LOW | 30–49 | `false` |
| BLOCKED | < 30 | `false` — unconditionally, no exceptions |

Datasets with score < 60 (`POOR_QUALITY`) are **persisted for audit** but **not delivered** via API or Event Bus.

### DataQualityGate — Five Conditions

`signalEngineAllowed = dataFresh AND dataComplete AND dataTimestampValid AND dataProviderHealthy AND dataSemanticallyValid`

| Condition | Passes when |
|---|---|
| `dataFresh` | `quoteAgeMs ≤ freshnessFreshMs`, or `eventTimeMs` is within 30 days |
| `dataComplete` | All of: symbol, timestamp, open, high, low, close, volume are present and non-null |
| `dataTimestampValid` | OHLCV invariants hold + timestamp not negative/future/too-old |
| `dataProviderHealthy` | `providerAvailable=true` OR `source` is non-empty OR `provider` is non-empty |
| `dataSemanticallyValid` | `confidenceScore ≥ min_confidence_score` (default 60; floor 30) |

### Freshness Thresholds (Indian Market)

During `REGULAR` session:

| Tier | FRESH threshold |
|---|---|
| INDEX, FO_LIQUID | ≤ 10 seconds |
| FO_NORMAL | ≤ 15 seconds |
| EQUITY | ≤ 30 seconds |

Outside `REGULAR` (extended windows):

| Tier | FRESH threshold |
|---|---|
| INDEX, FO_LIQUID | ≤ 60 seconds |
| FO_NORMAL | ≤ 90 seconds |
| EQUITY | ≤ 120 seconds |

For interval-based candle freshness (India):

| Interval | FRESH ≤ | STALE ≤ | EXPIRED > |
|---|---|---|---|
| 1m | 2 min | 10 min | 10 min |
| 5m | 7 min | 30 min | 30 min |
| 15m | 20 min | 60 min | 60 min |
| 30m | 40 min | 90 min | 90 min |
| 1h | 70 min | 3h | 3h |
| 1d | 26h | 48h | 48h |

Crypto multiplies all India thresholds by 1.5 (24/7 market).

---

## 11. Cache Hierarchy

```
Request arrives
      │
      ▼
L1: In-process LRU (src/cache/l1_cache.py)
    Capacity: CACHE_L1_MAX_ENTRIES (default 10,000)
    Latency:  ≤1ms p99
    Eviction: LRU on capacity exceeded
      │ miss
      ▼
L2: Redis 7 (src/cache/redis_client.py)
    Namespace: mds:{dataType}:{provider}:{exchange}:{symbol}:{interval}:{from}:{to}
    Latency:   ≤5ms p99
    TTLs:
      live quote          3 seconds
      batch quotes        3 seconds
      intraday candles    30 seconds
      daily+ candles      4 hours
      option chain        15 seconds
      instrument master   12 hours
      provider health     5 seconds
      │ miss
      ▼
L3: PostgreSQL (candle_bar + provenance tables)
      │ miss
      ▼
Provider call → populate all three cache levels
```

### Stale-While-Revalidate

When a cache entry's remaining TTL ≤ 20% of its configured TTL, the platform returns the cached data **immediately** and triggers a single background refresh. Concurrent refresh attempts for the same key are coalesced into one.

### Request Coalescing

If 10 consumers request the same live quote simultaneously while an in-flight provider call is running, all 10 wait for that single result — no duplicate provider calls are made.

### Cache-Control Headers

| Response type | Header |
|---|---|
| Live quote | `public, s-maxage=3, stale-while-revalidate=5` |
| Intraday candles | `public, s-maxage=30, stale-while-revalidate=60` |
| Daily candles | `public, s-maxage=300` |
| Option chain | `public, s-maxage=15, stale-while-revalidate=20` |
| Error responses | `no-store` |

---

## 12. Circuit Breaker and Rate Limiting

### Circuit Breaker

Operates per `(provider, capability)` pair. State is stored in Redis at `mds:cb:{provider}:{capability}`.

```
CLOSED ──(threshold failures)──→ OPEN ──(recovery_window)──→ HALF_OPEN
  ↑                                                               │
  └──────────────────(probe success)────────────────────────────┘
  OPEN ←─────────────(probe failure)──────────────────────────┘
```

| Configuration | Variable | Default | Valid range |
|---|---|---|---|
| Failure threshold | `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | 5 | 1–100 |
| Recovery window | `CIRCUIT_BREAKER_RECOVERY_WINDOW_SEC` | 60 | 1–3600 |

**Does NOT increment failure counter:**
- HTTP 429 (rate limit) — Requirement 5.5 / 5.6
- `MARKET_CLOSED` response — Requirement 5.7
- `UNSUPPORTED_CAPABILITY` response — Requirement 5.8

Circuit state is visible at `GET /v1/analytics/providers/{provider_id}` and `GET /metrics` (`mds_circuit_breaker_state` gauge: 0=CLOSED, 1=OPEN, 2=HALF_OPEN).

### Token-Bucket Rate Limiting (Provider)

The Provider Gateway enforces per-provider, per-capability token buckets to respect provider rate limits. On HTTP 429 with `Retry-After`, the gateway respects the indicated delay. Without `Retry-After`, a default 60-second backoff applies. Neither increments the circuit-breaker failure counter.

---

## 13. WebSocket Streaming

### Connecting

```
ws://localhost:8200/v1/stream/ticks
```

Authenticate using `X-API-KEY` or `Authorization: Bearer <token>` headers at connection time.

### Control Messages (Client → Server)

**Subscribe:**
```json
{"action": "subscribe", "symbols": ["NIFTY", "BANKNIFTY"], "market": "india"}
```

**Unsubscribe:**
```json
{"action": "unsubscribe", "symbols": ["NIFTY"]}
```

### Server Frames (Server → Client)

**Acknowledgement:**
```json
{"type": "ack", "action": "subscribe", "symbols": ["NIFTY", "BANKNIFTY"]}
```

**Heartbeat (every 10 seconds):**
```json
{"type": "heartbeat", "timestamp": "2026-01-15T09:15:00.000Z"}
```

**Error:**
```json
{"type": "error", "code": "MAX_SUBSCRIPTIONS_EXCEEDED", "message": "..."}
```

**Live tick:**
```json
{
  "tickId":       "550e8400-e29b-41d4-a716-446655440000",
  "instrumentId": "NSE:NIFTY:IDX",
  "symbol":       "NIFTY",
  "exchange":     "NSE",
  "eventTimeMs":  1705300000123,
  "receivedAtMs": 1705300000234,
  "ltp":          22150.50,
  "change":       45.25,
  "changePct":    0.20,
  "volume":       1234567,
  "oi":           null,
  "tradedValue":  5678901234,
  "source":       "angel_one",
  "quality":      0.92,
  "isDuplicate":  false
}
```

### Connection Rules

| Rule | Detail |
|---|---|
| Heartbeat timeout | 3 missed heartbeats within 30 seconds → server closes connection |
| Max subscriptions | 500 concurrent symbol streams platform-wide |
| Cleanup on close | All subscriptions released within 5 seconds |
| Duplicates | Published with `isDuplicate: true` — never silently dropped |

---

## 14. Data Provenance and Lineage

### What Is Provenance?

Every market data observation is assigned a UUID v4 `dataObservationId` at ingestion. An immutable `DataProvenance` record is written immediately containing:

| Field | Value |
|---|---|
| `dataObservationId` | UUID v4, globally unique |
| `source` | DataSource enum (ANGEL_ONE, UPSTOX, SCRAPLING_NSE, …) |
| `eventTimeMs` | Exchange event timestamp in UTC ms |
| `receivedAtMs` | Platform ingestion wall-clock time |
| `availableAtMs` | When data became available to consumers (after pipeline) |
| `normalisationVersion` | Semver string of the normaliser version |
| `validationApplied` | Whether the full 14-step pipeline ran |
| `isFallback` | True when a fallback provider was used |
| `sourceChain` | Ordered list of providers in the processing path |

### Lineage Store

Retention policy:
- In-memory LRU: most recent 100,000 observations (evicted insertion-order at capacity)
- PostgreSQL: all records, indefinitely

### Trade Forensics

When a trade is executed, record `dataObservationId`, `dataConfidenceAtExecution`, `signalEngineAllowed`, and `dataProviderAtExecution` so any future audit can reconstruct the full `data → signal → trade` chain.

### Backtest Point-in-Time Correctness

When serving backtest data for time `T`:
- Only records with `availableAtMs ≤ T` are returned
- Records missing `availableAtMs` are **rejected** and logged
- `PointInTimeFilter.filter_by_available_at()` enforces this strictly

---

## 15. Null Semantics — Critical Rules

These rules are enforced at every layer (Normaliser → semantic validator → persistence → API).

| Field | Rule | What happens when absent |
|---|---|---|
| `oi` (open interest) | **Never** populated from `tradedValue` | `oi: null, oiMissing: true` |
| `tradedValue` | Total traded value in INR — semantically distinct from `oi` | `tradedValue: null` |
| `iv` (implied volatility) | **Zero is not a substitute** for absent IV | `iv: null, ivMissing: true` |
| `delta`, `gamma`, `theta`, `vega`, `rho` | Placeholder zeros are prohibited | Respective field `null` |
| `bid`, `ask` | Placeholder zeros are prohibited | `bid: null, ask: null` |
| `volume` | When source doesn't supply it: `volumeUnavailable: true, volume: 0` | Distinguishes from genuine 0-volume bar |

**Canonical null rule: `null` means the value is genuinely absent. `0` means the value is zero. These are different things.**

---

## 16. Indian Market Session Management

### NSE Session Phases

All IST (UTC+5:30) boundaries. Each phase is exclusive at the end time.

| Phase | IST window | Notes |
|---|---|---|
| `PRE_OPEN` | 09:00–09:08 | — |
| `PRE_OPEN_CALL_AUCTION` | 09:08–09:15 | — |
| `REGULAR` | 09:15–15:30 | Ends 13:00 on half-days |
| `POST_MARKET` | 15:30–16:00 | 13:00–13:30 on half-days |
| `CLOSED` | All other times | Weekends, full holidays |
| `MUHURAT` | Diwali only | Special evening session |

### Holiday Calendar

- Refreshed from NSE official API at the **start of each new calendar year**
- Cached indefinitely on refresh success
- `calendarStatus` in `GET /v1/india/market/status` shows date of last successful refresh
- If the API is unavailable at refresh time: retains last successful calendar; logs a structured warning

### Behaviour When Market is Closed

Returns the most recent available data (≤ 24 hours old) with:
- `marketStatus: "CLOSED"` (or the actual current phase)
- No HTTP 5xx
- Does **not** increment circuit-breaker failure counters

---

## 17. Historical Data and Gap Recovery

### Backfill Routing (Capability Matrix)

| Instrument type | Interval | Primary source |
|---|---|---|
| Equity | 1m (multi-day) | Angel One SmartAPI |
| Index | 1m–1h | Upstox V3 |
| All | 1d EOD with OI | Jugaad-data |
| All | Any interval | OpenChart (reconciliation + fallback) |

### Chunk Sizes

Provider requests are chunked to respect per-provider API limits:

| Provider | Interval | Max chunk |
|---|---|---|
| Angel One SmartAPI | 1m | 30 calendar days |
| Angel One SmartAPI | 5m/15m | 90 calendar days |
| Upstox V3 | 1m | 7 calendar days |
| Upstox V3 | 5m/15m | 30 calendar days |
| Upstox V3 | 1d | 365 calendar days |

### Gap State Machine

```
PENDING → RECOVERING → RECOVERED
           │
           └→ EXHAUSTED  (after GAP_RECOVERY_MAX_ATTEMPTS failures)
```

`EXHAUSTED` gaps are retained for manual review and generate a `DataIncident` record. They are never automatically retried further.

### Reconciliation Thresholds

Applied per `(instrumentId, exchange, interval, timestamp)` tuple:

| Deviation formula | Status |
|---|---|
| `\|A−B\| / max(\|A\|,\|B\|) × 100 ≤ 0.5%` for all OHLCV fields | `CONFIRMED` |
| Any field 0.5–2.0% | `MINOR_DISCREPANCY` |
| Any field > 2.0% | `MAJOR_DISCREPANCY` → `DataIncident` generated |

---

## 18. Crypto Market Data

### Binance Integration

The Binance adapter (`src/providers/adapters/binance_rest.py`) connects to both spot (`api.binance.com`) and futures (`fapi.binance.com`) APIs.

**Retry policy**: 3 attempts on transient HTTP 5xx with exponential backoff (1s, 2s, 4s). HTTP 429 is raised immediately — the gateway rate limiter handles it.

**Kline normalisation**: Raw Binance arrays `[openTime, open, high, low, close, volume, closeTime, ...]` are normalised to `{time, open, high, low, close, volume, closeTime}`.

**OHLCV validation**: `BinanceOHLCVNormaliser` enforces all OHLC invariants before persistence. Invalid candles are silently dropped and logged.

### Deribit Integration

The Deribit adapter (`src/providers/adapters/deribit_client.py`) uses JSON-RPC 2.0 over HTTPS. The `result` field is automatically unwrapped from the envelope.

**Null preservation**: `mark_iv`, `open_interest`, `best_bid_price`, `best_ask_price`, `underlying_price` are preserved as `null` when Deribit returns them as null. Zero is never substituted.

**OHLCV normalisation**: Deribit TradingView parallel arrays (`ticks`, `open`, `high`, …) are normalised to the canonical `{time, open, high, low, close, volume}` format.

**OptionsOverview computation** (`src/engines/options_overview.py`):
1. Fetches all option instruments for the currency
2. Fetches the index price
3. Fetches ticker for each instrument (mark price, IV, OI, bid/ask)
4. Computes `total_call_oi`, `total_put_oi`, `put_call_oi_ratio` (null when call OI is zero — never infinity)

---

## 19. Provider Adapter Reference

### Capability Matrix

Located at `src/providers/capability_matrix.py`. Each entry declares:
- `provider` (DataSource enum)
- `instrument_classes` (EQ, FO, IDX, …)
- `intervals` supported
- `history` (bool)
- `live` (bool)
- `max_chunk_days`
- `requests_per_second`
- `source_type` (BROKER_AUTHENTICATED / OPEN_SOURCE_NSE_DERIVED / CREDENTIAL_FREE / SECONDARY_FALLBACK)

### Provider Health Endpoint

`GET /v1/providers/health` returns per-provider, per-capability:

| Field | Description |
|---|---|
| `status` | UP / DOWN / DEGRADED / UNKNOWN |
| `circuitState` | CLOSED / OPEN / HALF_OPEN |
| `availability` | Float 0.0–1.0 |
| `latencyP50Ms` | Milliseconds |
| `latencyP99Ms` | Milliseconds |
| `errorRate` | Float 0.0–1.0 |
| `lastSuccessAt` | UTC ISO-8601 |
| `lastFailureReason` | Human-readable string or null |
| `semanticIntegrity` | True when last response passed all semantic checks |

---

## 20. Observability

### Prometheus Metrics (`GET /metrics`)

| Metric | Type | Labels | Description |
|---|---|---|---|
| `mds_request_duration_seconds` | Histogram | endpoint, method, status | Request latency |
| `mds_provider_call_duration_seconds` | Histogram | provider, capability | Provider latency |
| `mds_cache_hits_total` | Counter | level (l1/l2/l3) | Cache hits |
| `mds_cache_misses_total` | Counter | level | Cache misses |
| `mds_circuit_breaker_state` | Gauge | provider, capability | 0=CLOSED, 1=OPEN, 2=HALF_OPEN |
| `mds_tick_publish_rate` | Gauge | — | Ticks/second |
| `mds_gap_count` | Gauge | severity | Open gaps by severity |
| `mds_quality_score` | Gauge | instrument_id | DataConfidenceScore per instrument |
| `mds_quality_score_distribution` | Histogram | — | Score distribution |
| `mds_duplicate_rate` | Gauge | — | Duplicate tick percentage |

### Structured Logs (structlog)

Format in `production`/`staging`: JSON. Format in `development`: human-readable console output.

Minimum fields per log entry:

```json
{
  "timestamp":   "2026-01-15T09:15:00.000Z",
  "level":       "info",
  "service":     "data-service",
  "component":   "market_engine",
  "event":       "quote_published",
  "instrumentId": "NSE:NIFTY:IDX",
  "provider":    "angel_one",
  "durationMs":  12,
  "requestId":   "req-uuid-v4"
}
```

### OpenTelemetry Tracing

Configure via `OTEL_EXPORTER`:

| Value | Behaviour |
|---|---|
| `noop` | All spans discarded — zero overhead (default for local dev) |
| `otlp` | Export via OTLP gRPC. Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317` |
| `jaeger` | Direct Jaeger export |

### Clock-Skew Monitor

`ClockSkewMonitor` samples NTP clock offset at intervals ≤ 30 seconds. When median skew > 500ms:
- Sets `clockDegraded: true` in `GET /v1/health/data`
- Emits `clock_skew_warning` structured log at WARN level
- `GET /v1/analytics/health` returns HTTP 503

---

## 21. Local Development Workflow

### Prerequisites

- Python 3.11 or 3.12
- Docker Desktop with Compose V2
- `pip` or `uv` (uv is faster)

### First-Time Setup

```bash
# 1. Clone the repository
git clone <repo-url>
cd data-service2.0

# 2. Install dependencies
pip install -e ".[dev]"
# or: uv pip install -e ".[dev]"

# 3. Start infrastructure (Redis + PostgreSQL only)
docker compose up -d redis postgres

# 4. Run migrations
APP_ENV=local alembic upgrade head

# 5. Start the API server with hot-reload
APP_ENV=local uvicorn src.server:app --host 0.0.0.0 --port 8200 --reload
```

### Verify it works

```bash
curl http://localhost:8200/v1/health/live
# {"status":"alive","version":"2.0.0","uptimeMs":1234,"timestamp":"..."}

# Browse API docs (development mode only)
open http://localhost:8200/docs
```

### Start the Worker and Scheduler

```bash
APP_ENV=local python -m src.worker
APP_ENV=local python -m src.scheduler
```

### Start Everything with Docker Compose

```bash
docker compose --env-file .env.local up -d
docker compose exec api alembic upgrade head
```

### Using Your Own Provider Credentials

Fill in `ANGEL_ONE_API_KEY`, `ANGEL_ONE_CLIENT_ID`, `ANGEL_ONE_TOTP_SECRET` in `.env.local`. The platform gracefully degrades for any provider whose credentials are absent.

### Code Quality

```bash
# Lint
ruff check src/ tests/

# Auto-fix
ruff check --fix src/ tests/

# Format
ruff format src/ tests/

# Type-check
mypy src/
```

---

## 22. Testing Guide

### Test Structure

```
tests/
├── unit/           Per-module unit tests — no external dependencies
├── integration/    Tests requiring live Redis + PostgreSQL
├── property/       Hypothesis property-based tests
├── performance/    Load benchmarks (mocked I/O, CI-safe)
├── mocks/          Shared mock objects and factory functions
└── fixtures/       Shared pytest fixtures (conftest.py)
```

### Running Tests

```bash
# Unit tests only (no infrastructure needed)
APP_ENV=local pytest tests/unit/ -q

# Unit + property tests
APP_ENV=local pytest tests/unit/ tests/property/ -q

# Integration tests (requires Docker infrastructure)
docker compose --env-file .env.local up -d redis postgres
APP_ENV=local pytest -m integration -q

# Performance benchmarks (mocked I/O, CI-safe)
APP_ENV=local pytest tests/performance/ -m performance -v -s

# All tests
APP_ENV=local pytest -q

# With coverage
APP_ENV=local pytest tests/unit/ --cov=src --cov-report=term-missing -q
```

### Pytest Markers

| Marker | Meaning |
|---|---|
| `integration` | Requires live Redis + PostgreSQL |
| `performance` | Load/benchmark tests |
| `property` | Hypothesis property-based tests |
| `asyncio` | Async test using pytest-asyncio |

### Using Mock Fixtures

Import from `tests/mocks/provider_mocks.py`:

```python
from tests.mocks.provider_mocks import (
    MockAngelOneProvider, MockBinanceClient, MockDeribitClient, MockRedis,
    make_valid_tick, make_valid_ohlcv_candle, make_option_chain_row,
    SAMPLE_OHLCV_KLINES, SAMPLE_INDIA_TICKS,
)
```

Or use pytest fixtures from `tests/fixtures/conftest.py` (auto-discovered):
```python
async def test_quote(angel_one_provider, valid_tick):
    result = await angel_one_provider.fetch_live_quote("NSE:RELIANCE:EQ")
    assert result["ltp"] > 0
```

---

## 23. Deployment Guide

### Docker Compose (Recommended)

```bash
# Copy and fill in all REQUIRED fields
cp .env.example .env.production
# Edit .env.production — fill DATABASE_URL, POSTGRES_PASSWORD, JWT_SECRET,
# CONSUMER_API_KEYS, CORS_ALLOWED_ORIGINS, and provider credentials.

# Deploy
APP_ENV=production docker compose --env-file .env.production up -d

# Run migrations
docker compose exec api alembic upgrade head

# Verify
curl http://your-server:8200/v1/health/ready
```

### Kubernetes / ECS

For multi-replica deployments, ensure all shared state is in Redis/PostgreSQL (not process memory). The application is stateless by design:
- All cache state: Redis (`mds:` namespace)
- All backfill checkpoints: Redis (no TTL)
- All circuit-breaker state: Redis (`mds:cb:*`)
- All persistent data: PostgreSQL

Pass `APP_ENV=production` as an environment variable; inject secrets via your secrets manager.

### Pre-Deployment Checklist

- [ ] `alembic upgrade head` runs successfully
- [ ] `GET /v1/health/ready` returns HTTP 200
- [ ] Unit test suite passes with zero failures: `pytest tests/unit/ -q`
- [ ] `CORS_ALLOWED_ORIGINS` is set to your consumer origin(s), not `*`
- [ ] `JWT_SECRET` is a random 64-character hex string, not the dev placeholder
- [ ] `CONSUMER_API_KEYS` contains real keys, not `dev-key-local-*`
- [ ] Provider credentials are injected from your secrets manager, not as plaintext in `.env.production`
- [ ] `ENVIRONMENT=production` (disables Swagger UI)
- [ ] `.env.production` is in `.gitignore` and not committed

### Graceful Shutdown (SIGTERM)

On `SIGTERM`:
1. Stop accepting new connections
2. Drain in-flight requests (30-second window)
3. Close provider WebSocket connections
4. Flush pending cache writes to Redis
5. Flush pending Event Bus publishes
6. Export pending OpenTelemetry spans
7. Close Redis connection pool
8. Dispose PostgreSQL engine
9. Exit code 0

---

## 24. Troubleshooting

### "DATABASE_URL must not be empty"

You haven't set `DATABASE_URL`. Set `APP_ENV=local` to load `.env.local` (which has a development default), or export `DATABASE_URL` directly.

### "interval 3m is permanently unsupported" (HTTP 400)

You're requesting `interval=3m` on an Indian market endpoint. This is banned permanently at every layer. Use `1m`, `5m`, or another supported interval. If you need 3m, switch to the Binance crypto endpoint where it is supported.

### Circuit breaker keeps opening for a provider

1. Check `GET /v1/providers/health` for the provider's `errorRate` and `lastFailureReason`.
2. Reduce `CIRCUIT_BREAKER_FAILURE_THRESHOLD` to diagnose the trigger.
3. If the provider is returning HTTP 429, confirm `PROVIDER_QUEUE_MAX_DEPTH` is not too low.
4. Force-reset via `await circuit_breaker.reset()` in the Python shell if needed.

### "signalEngineAllowed: false" unexpectedly

Check `blockReasons` in the quality gate response. Common causes:
- `dataFresh=False`: data is older than the freshness threshold for the instrument tier.
- `dataSemanticallyValid=False`: `confidenceScore` is below `min_confidence_score` (default 60) or below 30 (BLOCKED floor).
- `dataProviderHealthy=False`: no `source`, `provider`, or `providerAvailable` field.
- `dataComplete=False`: one of the required OHLCV fields is null.

### High memory usage on the API process

Lower `CACHE_L1_MAX_ENTRIES`. Each L1 entry holds the full serialised response payload (~1KB typical). 10,000 entries ≈ 10MB baseline.

### Redis connection errors on startup

The API logs `redis_unavailable` and enters degraded mode. Check `REDIS_URL` and that the Redis container is running. Run `docker compose logs redis` to see if there are startup errors.

### TimescaleDB extension missing

The platform works correctly with plain PostgreSQL 15. TimescaleDB is optional. If you want to enable it:
```bash
docker compose exec postgres psql -U mds_user -d mds -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
psql $DATABASE_URL -f scripts/promote_timescaledb.sql
```

---

## 25. Glossary

| Term | Definition |
|---|---|
| **DataConfidenceScore** | Integer 0–95 representing dataset quality. Formula: freshness(35%) + completeness(25%) + provider_health(20%) + timestamp_validity(10%) + cross_source_agreement(10%). |
| **DataQualityGate** | Five-condition signal-safety contract: `dataFresh ∧ dataComplete ∧ dataTimestampValid ∧ dataProviderHealthy ∧ dataSemanticallyValid`. |
| **DataProvenance** | Immutable record attached to every market observation: UUID, source, timestamps, normalisation version, fallback flag. |
| **Capability Matrix** | Table mapping each (provider, instrument_class, interval) triple to supported operations. |
| **POOR_QUALITY** | Score < 60. Dataset persisted for audit but NOT delivered via API or Event Bus. |
| **BLOCKED** | Score < 30. `signalEngineAllowed: false` unconditionally. No exceptions. |
| **3m ban** | The 3m candle interval is permanently unsupported for Indian market data at six independent enforcement layers. It is permitted for Binance crypto data. |
| **Null semantics** | `null` = value genuinely absent. `0` = value is zero. These are different and are never interchangeable for `oi`, `iv`, Greeks, `bid`, `ask`. |
| **OI** | Open interest (number of outstanding derivative contracts). Semantically distinct from `tradedValue`. |
| **tradedValue** | Total traded value in INR. Never used to populate `oi`. |
| **IV** | Implied volatility. Null when not supplied by the source. Zero IV is not a valid substitute. |
| **IST** | Indian Standard Time = UTC+5:30. Used for session phase determination. |
| **REGULAR session** | NSE trading session 09:15–15:30 IST (13:00 on half-days). Freshness thresholds are tighter during REGULAR. |
| **Event Bus** | Redis Streams used for internal publish-subscribe. Streams: `mds:events:ticks`, `mds:events:dataset-ready`. |
| **MetricTag** | OBSERVED (from exchange) / DERIVED (computed) / MODELLED (pricing model). Annotates every canonical field. |
| **HALF_OPEN** | Circuit-breaker probe state. Exactly one probe request is allowed through. Success → CLOSED, failure → OPEN. |
| **availableAtMs** | UTC epoch ms when the platform first made a datum available to consumers. Used for backtest point-in-time filtering. |
| **look-ahead bias** | Using data in a backtest that would not have been available at the simulated time. Prevented by the `PointInTimeFilter`. |
