# DATA-SERVICE 2.0 — Architecture Reference

> **Version:** 2.2.0 · **Port:** 8200 · **Language:** Python 3.11+  
> Last updated: 2026-09-22 (v2.2.0 — 5y backfill, fo_universe, OHLCV catch-up worker, NSE_INDEX routing fix)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Layout](#2-repository-layout)
3. [Docker Compose Services](#3-docker-compose-services)
4. [Request Lifecycle](#4-request-lifecycle)
5. [Layer 1 — Provider Gateway](#5-layer-1--provider-gateway)
6. [Layer 2 — Core Engines](#6-layer-2--core-engines)
7. [Layer 3 — 14-Step Validation Pipeline](#7-layer-3--14-step-validation-pipeline)
8. [Layer 4 — Three-Level Cache](#8-layer-4--three-level-cache)
9. [Layer 5 — Persistence (PostgreSQL + TimescaleDB)](#9-layer-5--persistence-postgresql--timescaledb)
10. [Layer 6 — REST API + WebSocket](#10-layer-6--rest-api--websocket)
11. [Authentication and Security](#11-authentication-and-security)
12. [Scheduler and Background Worker](#12-scheduler-and-background-worker)
13. [Observability Stack](#13-observability-stack)
14. [Provider Adapter Reference](#14-provider-adapter-reference)
15. [Data Flow Diagrams](#15-data-flow-diagrams)
16. [Configuration and Settings](#16-configuration-and-settings)
17. [Design Invariants](#17-design-invariants)

---

## 1. System Overview

DATA-SERVICE 2.0 is the **single market-data authority** for the AlphaForge platform. It centralises acquisition, normalisation, validation, caching, persistence, and streaming for all market data sources behind one versioned REST + WebSocket API.

### Core mandates

- **No consumer bypass** — no upstream consumer may call any external data provider directly. All market data flows through this service.
- **No fabrication** — if a provider does not supply a field, the field is `null`. Zero is never substituted for a missing `oi`, `iv`, `bid`, or `ask`.
- **Correctness over availability** — a dataset that fails validation is rejected, never served.
- **3m interval permanently blocked for Indian market data** — enforced at adapter, engine, normaliser, DB constraint, and API levels. Binance/Delta crypto explicitly permits `3m`.

### High-level data flow

```
External Providers
        │
        ▼
  Provider Gateway       ← capability matrix, circuit breakers, rate limiting
        │
        ▼
   Core Engines          ← market, historical, streaming, quality, instruments
        │
        ▼
  14-Step Pipeline       ← validate, normalise, deduplicate, score, cache, persist
        │
        ▼
   Three-Level Cache     ← L1 LRU → L2 Redis → L3 PostgreSQL (canonical tables)
        │
        ▼
  REST API / WebSocket   ← FastAPI, port 8200, auth enforced on all /v1/* routes
```

---

## 2. Repository Layout

```
data-service2.0/
│
├── src/                         Application source code
│   ├── server.py                FastAPI app factory + lifespan startup
│   ├── worker.py                Background worker entry point
│   ├── scheduler.py             APScheduler cron jobs entry point
│   │
│   ├── api/                     REST + WebSocket endpoint handlers
│   │   ├── health.py            GET /v1/health/live|ready|data
│   │   ├── metrics.py           GET /metrics (Prometheus)
│   │   ├── india.py             Indian market endpoints
│   │   ├── instruments.py       Instrument master + F&O universe
│   │   ├── broker_analytics.py  PCR, OI buildup, gainers/losers
│   │   ├── crypto.py            Binance spot + futures endpoints
│   │   ├── deribit.py           Deribit options endpoints
│   │   ├── quality.py           DataQualityGate endpoints
│   │   ├── streaming.py         WebSocket /v1/stream/ticks
│   │   ├── analytics.py         Provider health + platform analytics
│   │   ├── provenance.py        Per-observation lineage endpoints
│   │   ├── lineage.py           Trade forensics endpoints
│   │   ├── contract.py          DataParityContract endpoint
│   │   ├── replay.py            Replay session endpoints
│   │   ├── internal.py          Internal service-to-service endpoints
│   │   ├── compat.py            AlphaForge ScraplingProvider compat routes
│   │   └── errors.py            Canonical ErrorCode enum + envelope builder
│   │
│   ├── auth/                    Authentication subsystem
│   │   ├── consumer_auth.py     API key + JWT bearer (ApiKeyAuth, JwtBearerAuth,
│   │   │                        ConsumerAuthDependency, token-exchange endpoint)
│   │   ├── angel_one_jwt.py     Angel One JWT store + scheduled rotation
│   │   └── upstox_oauth.py      Upstox OAuth 401 token refresh handler
│   │
│   ├── backtest/                Backtest mode
│   │   └── point_in_time.py     Look-ahead bias prevention filter
│   │                            (filter_by_available_at, BacktestContext dep)
│   │
│   ├── cache/                   Multi-level cache subsystem
│   │   ├── l1_cache.py          In-process OrderedDict-backed LRU (10K entries)
│   │   ├── redis_client.py      Redis async connection pool factory
│   │   ├── cache_manager.py     CacheManager: L1→L2→L3→provider chain,
│   │   │                        request coalescing, stale-while-revalidate
│   │   └── event_bus.py         Redis Streams Event Bus publisher/consumer
│   │
│   ├── core/                    Shared schemas, settings, pipeline, normaliser
│   │   ├── settings.py          Pydantic BaseSettings + APP_ENV env-file adapter
│   │   ├── normaliser.py        Canonical normaliser (OHLCV, quote, option chain)
│   │   ├── pipeline.py          14-step ValidationPipeline orchestrator
│   │   ├── shell_safety.py      Log-injection + shell-injection guards
│   │   ├── schemas/             Canonical Pydantic v2 models
│   │   │   ├── instrument.py    OHLCVCandle, Quote, OptionRow, SessionPhase
│   │   │   ├── provider.py      ProviderCapability, SourceType, ProviderId
│   │   │   ├── parity.py        DataParityContract
│   │   │   └── provenance.py    DataProvenance, ProvenanceFactory
│   │   ├── validators/          Per-step validators (one file per pipeline step)
│   │   │   ├── schema_validate.py
│   │   │   ├── timestamp.py
│   │   │   ├── semantic.py
│   │   │   ├── dedup.py         DedupStore + SHA-256 dedup hash
│   │   │   ├── gap_detection.py
│   │   │   ├── freshness.py     FRESH/AGING/STALE/EXPIRED classification
│   │   │   ├── reconciliation.py
│   │   │   └── quality.py
│   │   └── parsers/             Provider-specific raw response parsers
│   │
│   ├── db/                      Database layer
│   │   ├── engine.py            SQLAlchemy async engine factory
│   │   ├── timescale.py         TimescaleDB detection + hypertable status logging
│   │   └── models/              ORM models — equity_candle, futures_candle,
│   │                            options_candle, market_tick, market_quote,
│   │                            continuous_futures (new v2.2.0), fo_universe (new v2.2.0),
│   │                            exchange_calendar, instrument_provider_mapping, …
│   │
│   ├── engines/                 Core business logic engines
│   │   ├── market_engine.py     Indian live quotes, option chain, tick ingestion
│   │   ├── historical_engine.py OHLCV backfill → equity_candle / futures_candle / options_candle
│   │   ├── trading_calendar_service.py  DB-backed trading day resolver (exchange_calendar table)
│   │   ├── gap_recovery.py      PENDING→RECOVERING→RECOVERED/EXHAUSTED state machine
│   │   ├── streaming_engine.py  Tick dedup + Event Bus fan-out (≤200ms p99)
│   │   ├── quality_engine.py    DataConfidenceScore (0–95) + DataQualityGate
│   │   ├── instrument_master.py NSE instrument universe, symbol normalisation
│   │   ├── fno_universe.py      F&O universe refresh (08:45 IST daily)
│   │   ├── holiday_calendar.py  NSE holiday list + half-day detection
│   │   ├── market_session.py    NSE session state machine (PRE_OPEN→REGULAR→CLOSED)
│   │   ├── options_overview.py  Deribit options analytics aggregation
│   │   └── streaming_engine.py  WebSocket tick fan-out
│   │
│   ├── forensics/               Trade forensics
│   │   └── trade_forensics.py   TradeForensicsReport: per-trade data lineage
│   │
│   ├── middleware/              HTTP middleware
│   │   └── credential_stripper.py  Strips provider credentials from all responses
│   │
│   ├── monitors/                Monitoring
│   │   ├── clock_skew_monitor.py   NTP clock-skew sampling (every ≤30s)
│   │   └── quality_alerter.py      DataQualityGate alert emission
│   │
│   ├── observability/           Logging + tracing
│   │   ├── logging.py           structlog configuration (JSON in prod, console in dev)
│   │   └── tracing.py           OpenTelemetry SDK + OTLP exporter setup
│   │
│   ├── providers/               Data provider layer
│   │   ├── gateway.py           ProviderGateway: routes requests through cap matrix
│   │   ├── capability_matrix.py CAPABILITY_MATRIX: provider × capability definitions
│   │   ├── circuit_breaker.py   Redis-backed CLOSED/OPEN/HALF_OPEN state machine
│   │   ├── rate_limiter.py      Token-bucket rate limiter (Redis-backed, cross-replica)
│   │   ├── binance_normaliser.py  Binance-specific field mapping
│   │   ├── binance_persistence.py Binance candle DB write path
│   │   ├── delta_normaliser.py  Delta Exchange field mapping
│   │   ├── delta_persistence.py Delta Exchange candle DB write path
│   │   ├── deribit_client.py    Deribit REST client (options analytics)
│   │   ├── adapters/            One adapter file per external provider
│   │   │   ├── base.py          ProviderAdapter ABC
│   │   │   ├── angel_one.py     Angel One SmartAPI (TOTP + JWT auth)
│   │   │   ├── upstox.py        Upstox V3 REST (OAuth2 access token)
│   │   │   ├── binance_rest.py  Binance spot + futures REST
│   │   │   ├── delta_exchange.py Delta Exchange India REST
│   │   │   ├── scrapling_nse.py NSE Scrapling (WAF bypass via curl-cffi)
│   │   │   ├── jugaad_data.py   Jugaad-data NSE historical
│   │   │   ├── openchart.py     OpenChart NSE intraday
│   │   │   └── yahoo_finance.py Yahoo Finance (NSE index symbol mapping)
│   │   └── streams/             WebSocket stream adapters
│   │       ├── angel_one_stream.py   Angel One SmartStream WS
│   │       ├── upstox_stream.py      Upstox V3 WS (Protobuf)
│   │       └── binance_stream.py     Binance WS streams
│   │
│   ├── publishers/              Event publishers
│   │   └── dataset_publisher.py DatasetReady events → Event Bus
│   │
│   ├── replay/                  Replay engine
│   │   └── replay_engine.py     Replay session lifecycle management
│   │
│   ├── scheduler_jobs/          Individual cron job implementations
│   └── worker_tasks/            Individual worker task implementations
│       ├── ohlcv_catchup.py     Continuous OHLCV catch-up — startup + EOD 17:00 IST + intraday 4h
│       ├── stores/
│       │   └── lineage_store.py     ProvenanceRecord persistence
│       └── forensics/
│           └── trade_forensics.py   Per-trade lineage report builder
│
├── alembic/                     Database migration management
│   ├── env.py                   Alembic environment configuration
│   └── versions/                Migration scripts
│       └── 20240101_000000_initial_schema.py
│
├── tests/                       Test suite (4 485+ passing)
│   ├── unit/                    Unit tests (no infrastructure needed)
│   ├── integration/             Integration tests (requires Redis + PG)
│   ├── property/                Hypothesis property-based tests
│   ├── performance/             Load benchmarks (mocked I/O, CI-safe)
│   ├── mocks/                   Shared provider mock fixtures
│   └── fixtures/                Test data factories
│
├── scripts/                     Operational and validation scripts
│   ├── test_angel_live.py       Live Angel One auth + data test
│   ├── test_binance_live.py     Live Binance connectivity test
│   ├── test_deribit_live.py     Live Deribit connectivity test
│   ├── test_provider_auth.py    All provider auth validation
│   ├── test_api_curl.sh         Full API smoke test (curl-based)
│   ├── load_fno_instrument_master.py  Loads 34K+ F&O contracts + 35,940 Upstox NSE_FO keys
│   ├── load_fno_universe.py     Populates fno_universe_membership (238 active rows)
│   ├── populate_exchange_calendar.py  Loads NSE/NFO calendar (2024–2028, 3,654 rows)
│   ├── backfill_india_1y.py     1-year NSE historical candle backfill (EQ/IDX only)
│   ├── backfill_india_5y.py     5-year OHLCV backfill (ALL_SPOT via fo_universe + fallback)
│   ├── load_fo_bhavcopy_5y.py   NSE F&O bhavcopy daily loader → futures_candle + options_candle
│   ├── load_continuous_futures.py  Front-month roll → continuous_futures table
│   ├── seed_fo_universe.py      Seed fo_universe master table (314 rows)
│   ├── load_options_chain_snapshots.py  Load per-strike CE/PE option chain snapshots
│   ├── _live_verify.py          Comprehensive live runtime verification
│   ├── _cred_check.py           Credential validation helper
│   ├── _check_oauth.py          Upstox OAuth token status checker
│   ├── _rl_load_test.py         Rate-limit load test
│   ├── _test_survivorship.py    Survivorship bias constraint tests
│   ├── _test_tick_persistence.py  Tick persistence pipeline test
│   ├── _test_upstox_ws.py       Upstox WebSocket live decode test
│   ├── _redis_token_sharing.py  Multi-worker Redis token sharing test
│   ├── _verify_refresh_lock.py  Distributed refresh lock test
│   ├── deploy.sh                Core deploy logic (build → rolling restart → health check)
│   ├── webhook_server.py        HTTP webhook receiver (GitHub / GitLab / generic)
│   ├── setup-autodeploy.sh      Auto-deploy management CLI
│   └── post-push.hook           Git hook that fires on every `git push`
│
├── docker/postgres/init/        PostgreSQL init scripts (TimescaleDB extension)
├── docs/                        Technical documentation
├── reports/                     Audit, certification, and RCA reports
├── Dockerfile                   Multi-stage build (builder → runtime)
├── docker-compose.yml           5-service Compose definition
├── pyproject.toml               Project metadata + dependency pins
└── alembic.ini                  Alembic configuration
```

---

## 3. Docker Compose Services

| Service | Image | Port | Role |
|---|---|---|---|
| `api` | `data-service:2.2.0` | 8200 | FastAPI + Uvicorn (4 workers), REST + WebSocket |
| `worker` | `data-service:2.2.0` | — | Continuous OHLCV catch-up (startup + EOD 17:00 IST + intraday 4h), gap recovery, reconciliation |
| `scheduler` | `data-service:2.2.0` | — | APScheduler cron jobs (F&O refresh 08:45 IST, EOD catch-up 17:00 IST, JWT rotation 23:55 IST, NTP monitor) |
| `redis` | `redis:7-alpine` | 6379 (internal) | L2 cache, Event Bus (Streams), circuit-breaker state, rate-limiter state, OHLCV checkpoints |
| `postgres` | `timescale/timescaledb:latest-pg15` | 5444 (host) | L3 persistent store — equity_candle (~123M), futures_candle (246K+), options_candle (242K+), continuous_futures (131K), fo_universe (314), instruments |

All services share the `data-service-net` bridge network. The host only exposes ports 8200 (API) and 5444 (PostgreSQL for local tooling).

### Dockerfile build stages

The Dockerfile uses two stages:

1. **builder** — installs Python dependencies via hatchling into `/install`
2. **runtime** — copies `/install` + application source, creates a non-root `appuser` (UID 1001)

The runtime stage contains no build tools, minimising attack surface.

---

## 4. Request Lifecycle

A typical authenticated read request follows this path:

```
Client
  │
  │  HTTP GET /v1/india/historical?symbol=RELIANCE&interval=1d
  │  X-API-KEY: <key>
  ▼
FastAPI (Uvicorn, 4 workers)
  │
  ├─ CORS middleware (checks Origin against CORS_ALLOWED_ORIGINS)
  ├─ CredentialStripperMiddleware (removes provider secrets from responses)
  ├─ ConsumerAuthDependency (validates X-API-KEY or JWT Bearer)
  │
  ▼
india.py router handler
  │
  ├─ Check L1 LRU cache (in-process, sub-ms)  → HIT: return immediately
  ├─ Check L2 Redis cache (mds: namespace)    → HIT: populate L1, return
  ├─ Check L3 PostgreSQL (async query)        → HIT: populate L2+L1, return
  │
  ▼ (cache miss)
HistoricalEngine
  │
  ├─ ProviderGateway.route() — selects provider via capability matrix
  ├─ CircuitBreaker.check()  — rejects if provider circuit is OPEN
  ├─ TokenBucketRateLimiter  — enforces per-provider rate cap
  │
  ▼
ProviderAdapter (e.g. UpstoxAdapter or AngelOneAdapter)
  │  external HTTP call
  ▼
Raw provider response
  │
  ▼
14-Step ValidationPipeline
  │  1. raw_receipt → 2. schema_validate → 3. normalise → 4. timestamp_normalise
  │  5. semantic_validate → 6. dedup → 7. gap_detect → 8. freshness_classify
  │  9. reconcile → 10. quality_score → 11. canonical_output
  │  12. cache_populate (L2 Redis + L1 LRU)
  │  13. persist (PostgreSQL)
  │  14. deliver → API response
  ▼
JSON response with canonical envelope:
  {"data": [...], "metadata": {"provider": "...", "score": 87, ...}}
```

---

## 5. Layer 1 — Provider Gateway

**Module**: `src/providers/gateway.py`, `capability_matrix.py`, `circuit_breaker.py`, `rate_limiter.py`

### Capability Matrix

The single source of truth for what each provider can serve. Every provider is registered with:

- **DataType**: `OHLCV_HISTORICAL`, `LIVE_QUOTE`, `OPTION_CHAIN`, `TICK`, `FUTURES_OVERVIEW`, etc.
- **InstrumentClass**: `EQ` (equity), `FO` (futures/options), `IDX` (index), `CRYPTO_SPOT`, `CRYPTO_FUTURES`, `CRYPTO_OPTIONS`
- **intervalSupport**: list of supported canonical intervals (`3m` is excluded from all Indian instrument classes by construction)
- **maxChunkDays**: maximum date range per single provider request
- **priority**: routing order (lower = higher priority)
- **sourceType**: `BROKER_AUTHENTICATED` or `OPEN_SOURCE_NSE_DERIVED` or `CREDENTIAL_FREE`

### Circuit Breaker

Three-state machine per provider × capability pair, backed by Redis (shared across all replicas):

```
CLOSED ──(failures ≥ threshold)──► OPEN
OPEN   ──(recovery_window elapsed)─► HALF_OPEN
HALF_OPEN ──(probe succeeds)───────► CLOSED
HALF_OPEN ──(probe fails)──────────► OPEN
```

**Does NOT count** as a failure: HTTP 429 (rate limit), `MARKET_CLOSED`, `UNSUPPORTED_CAPABILITY`.  
**Configuration**: `CIRCUIT_BREAKER_FAILURE_THRESHOLD` (default 5), `CIRCUIT_BREAKER_RECOVERY_WINDOW_SEC` (default 60).  
**Fallback**: if Redis is unavailable, state falls back to in-memory (providers stay accessible).

### Token-Bucket Rate Limiter

Redis-backed cross-replica rate limiter per provider. Prevents API-level throttling from external providers. Angel One enforces a 3 req/s NSE proxy ceiling; this is enforced in-adapter via an asyncio semaphore and at the gateway via the Redis-backed bucket.

---

## 6. Layer 2 — Core Engines

### MarketEngine (`src/engines/market_engine.py`)

Orchestrates all Indian live market data:
- Live quotes via Angel One SmartAPI (classified `BROKER_AUTHENTICATED`)
- Option chain snapshots (underlying + optional expiry)
- Delegates to AngelOneAdapter; falls back gracefully when credentials are absent

### HistoricalEngine (`src/engines/historical_engine.py`)

Manages OHLCV backfill across all Indian intervals:
- Chunks requests by `maxChunkDays` per provider (avoids API date-range limits)
- Primary: Upstox V3 (all 9 intervals supported — lifted 2026-09-17; EOD-only for NSE_INDEX — fixed 2026-09-22)
- Secondary: Angel One SmartAPI (1m: 30-day; 5m/15m: 90-day; sole intraday source for NSE_INDEX instruments)
- **NSE_INDEX intraday routing** (2026-09-22): `_resolve_provider()` routes IDX instruments to Upstox only for EOD intervals (`1d`/`1w`/`1M`). All `1m`–`1h` requests for index instruments use Angel One. `MIDCPNIFTY` unavailable on Upstox at any interval.
- **Angel One → Upstox auto-fallback**: when `angel_one_token_unknown` fires and the symbol has a registered ISIN key in `_UPSTOX_INSTRUMENT_KEYS` (301 symbols), the engine silently re-routes to Upstox. Logs `angel_one_token_unknown_upstox_fallback`.
- **301 `NSE_EQ|ISIN` keys**: all 229 NSE F&O universe stocks + NSE index display name aliases mapped.
- Source timestamp, underlying_id, and source_type written on every candle row
- Cross-provider reconciliation reports CONFIRMED / MINOR_DIVERGENCE / MAJOR_DIVERGENCE

### GapRecovery (`src/engines/gap_recovery.py`)

State machine for missing candle sequences:

```
PENDING → RECOVERING → RECOVERED
                   └→ EXHAUSTED (after max_attempts, emits DataIncident)
```

Max attempts: `GAP_RECOVERY_MAX_ATTEMPTS` (default 5, range 1–10). Exhausted gaps are retained in the DB for manual review.

### StreamingEngine (`src/engines/streaming_engine.py`)

Tick deduplication + fan-out:
- Dedup ID: `SHA-256(instrumentId:eventTimeMs:source:ltp:volume)[:32]`
- 24-hour rolling dedup window
- Duplicate ticks are published with `isDuplicate: true` (never silently dropped — audit trail preserved)
- Publish latency target: ≤200ms p99 from `receivedAtMs` to Event Bus
- Channel pattern: `mds:ticks:{symbol}`

### QualityEngine (`src/engines/quality_engine.py`)

**DataConfidenceScore formula** (score always in [0, 95]):

| Component | Weight | Max |
|---|---|---|
| `freshness_score` | 35% | 35 |
| `completeness_score` | 25% | 25 |
| `provider_score` | 20% | 20 |
| `timestamp_score` | 10% | 10 |
| `agreement_score` | 10% | 10 |

Broken sequence integrity applies a 20% penalty: `score = int(score * 0.8)`.

**DataQualityGate** — 5 conditions, all must be true for `signalEngineAllowed = True`:

1. `dataFresh` — timestamp within freshness threshold
2. `dataComplete` — no required fields null/missing
3. `dataTimestampValid` — OHLCV consistency + timestamp in valid range
4. `dataProviderHealthy` — at least one source is available
5. `dataSemanticallyValid` — DCS ≥ minimum threshold

**Score grades:**

| Grade | Range | `signalEngineAllowed` |
|---|---|---|
| HIGH | ≥ 80 | `true` (when gate passes) |
| MEDIUM | 50–79 | Depends on gate |
| LOW | 30–49 | `false` |
| BLOCKED | < 30 | `false` — unconditional |

### InstrumentMaster (`src/engines/instrument_master.py`)

- NSE symbol normalisation (canonical → provider-specific format)
- Instrument universe management
- F&O universe: refreshed daily at 08:45 IST by the scheduler

### MarketSession (`src/engines/market_session.py`)

NSE session state machine. Classifies any UTC instant into:

| Phase | IST window |
|---|---|
| `PRE_OPEN` | 09:00–09:08 |
| `PRE_OPEN_CALL_AUCTION` | 09:08–09:15 |
| `REGULAR` | 09:15–15:30 (13:00 on half-days) |
| `POST_MARKET` | 15:30–16:00 (13:00–13:30 on half-days) |
| `CLOSED` | all other times, weekends, NSE holidays |

`ZoneInfo("Asia/Kolkata")` is the sole IST reference — `timedelta(hours=5, minutes=30)` is prohibited.

---

## 7. Layer 3 — 14-Step Validation Pipeline

**Module**: `src/core/pipeline.py`

Every dataset from every provider must traverse all 14 steps in order. A failure at any step stops the pipeline; the dataset is not advanced.

| Step | Name | Action on failure |
|---|---|---|
| 1 | `raw_receipt` | Accept raw provider response | — |
| 2 | `schema_validate` | JSON schema validation | Reject + emit DataIncident |
| 3 | `normalise` | Map to canonical Pydantic schema; enforce null rules | Reject |
| 4 | `timestamp_normalise` | Convert all timestamps to UTC epoch ms | Reject |
| 5 | `semantic_validate` | OI/volume/IV/bid-ask integrity | Reject |
| 6 | `dedup` | Duplicate detection via SHA-256 hash | Flag `isDuplicate`, continue |
| 7 | `gap_detect` | Gap detection vs expected candle sequence | Record gap, continue |
| 8 | `freshness_classify` | Classify as FRESH/AGING/STALE/EXPIRED/UNKNOWN | Continue |
| 9 | `reconcile` | Cross-provider reconciliation | Continue (record divergence) |
| 10 | `quality_score` | DataConfidenceScore + DataQualityGate | Score < 60 → no delivery |
| 11 | `canonical_output` | Build final canonical dataset | Reject |
| 12 | `cache_populate` | Write to L2 Redis + L1 LRU | Continue (non-fatal) |
| 13 | `persist` | Write to L3 PostgreSQL | Continue (non-fatal) |
| 14 | `deliver` | API response / Event Bus publish | Suppressed if score < 60 |

**POOR_QUALITY rule**: datasets with DCS < 60 after step 10 are persisted (step 13) but NOT delivered via API or Event Bus (step 14 is suppressed). They remain available for audit.

**Step-skip policy**: no step may be skipped without an explicit `OVERRIDE_REASON` (1–500 chars) and a high-severity alert emitted within 5 seconds.

---

## 8. Layer 4 — Three-Level Cache

**Module**: `src/cache/`

```
Request
  │
  ├─ L1: In-process LRU     sub-millisecond, 10K entries (configurable),
  │       l1_cache.py        evicts LRU on overflow
  │
  ├─ L2: Redis 7             TTL-based per data type, namespaced mds:*
  │       redis_client.py    Falls back gracefully when Redis is unavailable
  │       cache_manager.py
  │
  └─ L3: PostgreSQL 15       Async SQLAlchemy read path
          + TimescaleDB       Canonical tables: equity_candle, futures_candle,
                              options_candle, market_tick, market_quote, …
```

### Cache behaviours

| Behaviour | Implementation |
|---|---|
| **Request coalescing** | Concurrent requests for the same key share one `asyncio.Future`. First caller fetches; all others await. |
| **Stale-while-revalidate** | When remaining TTL < 20% of configured TTL, cached value is returned and a background refresh is launched. |
| **Metadata tagging** | Responses from L1/L2 carry `dataSourceType = "CACHED"`. |
| **L2 unavailability** | Redis errors fall back to direct provider call; `cache_l2_unavailable` warning is logged. Redis error is never surfaced to consumers. |
| **Write-through** | After a provider call: write to L2 (Redis), then L1, then return. |

### Event Bus (`src/cache/event_bus.py`)

Redis Streams-based event bus for real-time data distribution:
- `DatasetReady` events published after successful backfill/option-chain completion
- WebSocket consumers subscribe via `mds:ticks:{symbol}` channels
- Provides an ordered, replayable stream for each symbol

---

## 9. Layer 5 — Persistence (PostgreSQL + TimescaleDB)

**Module**: `src/db/`, `alembic/`

### Schema architecture — v2 (revision `b1c2d3e4f5a6`, 2026-09-15)

The v2 schema adds 16 new tables across 6 logical layers, replacing the monolithic `candle_bar` table as the write target for all production data.

#### Layer 1 — Instrument Identity

| Table | Type | Purpose |
|---|---|---|
| `instrument_master` | Regular | NSE instrument universe — enhanced with `instrument_class` (EQ\|IDX\|FUT\|OPT\|CRYPTO\|ETF) and `name` columns |
| `instrument_provider_mapping` | Regular | Normalises provider-specific tokens (Angel One token `2885`, Upstox key `NSE_EQ|INE002A01018`, etc.) |
| `instrument_identity_history` | Regular | Audit trail for symbol/token changes, corporate actions, delistings |

#### Layer 2 — Canonical Candles (TimescaleDB hypertables, 7-day chunks)

| Table | Purpose |
|---|---|
| `equity_candle` | NSE/BSE equities + indices OHLCV (segments: EQ, IDX, ETF). **Primary write target for all NSE historical data.** |
| `futures_candle` | NSE F&O futures OHLCV + open interest (exchange: NFO/BFO). Also holds NSE bhavcopy daily rows. |
| `options_candle` | NSE F&O options OHLCV + open interest, with `strike` and `option_type` (CE/PE). Also holds NSE bhavcopy daily rows. |

All three enforce: no `3m` interval (CHECK constraint), OHLC validity (high≥open, high≥close, low≤open, low≤close), volume≥0. Unique index on `(instrument_id, exchange, interval_str, time)` enables `ON CONFLICT DO NOTHING`.

#### Layer 3 — Live Data (TimescaleDB hypertables, 1-day chunks)

| Table | Purpose |
|---|---|
| `market_tick` | Live WebSocket ticks — LTP, bid/ask, quantities, sequence number (pending WebSocket certification) |
| `market_quote` | Polled live quote snapshots — full OHLCV, circuit limits, 52-week range, depth_json (JSONB), source_type. **Written after every live quote fetch (fixed 2026-09-17).** |

#### Layer 4 — Option Chain

| Table | Purpose |
|---|---|
| `option_chain_snapshot` | Point-in-time chain header (UUID PK) — spot price, ATM strike, PCR, max pain, ATM IV. **Written after every chain fetch (fixed 2026-09-17).** |
| `option_chain_contract` | Per-strike rows (FK → snapshot, CASCADE DELETE) — full Greeks nullable by design. **Written after every chain fetch (fixed 2026-09-17).** |
| `option_greeks_snapshot` | Greeks time-series (TimescaleDB hypertable) — iv, delta, gamma, theta, vega, **oi, volume, ltp, prev_close, ltq** (columns added 2026-09-17). **Written after every REST Greeks API call (fixed 2026-09-17).** |

#### Layer 5 — Calendar + Sessions

| Table | Purpose |
|---|---|
| `exchange_calendar` | NSE/BSE holiday + trading day registry (2024–2028, 3 654 rows). Unique on `(exchange, segment, calendar_date)`. Day types: TRADING_DAY, WEEKEND, OFFICIAL_HOLIDAY, SPECIAL_SESSION, NOT_PUBLISHED, UNKNOWN |
| `market_session` | Actual session open/close records per day |
| `fno_universe_membership` | Point-in-time F&O eligibility — `effective_from`/`effective_to` for survivorship-bias-free backfill |

#### Layer 6 — Operations

| Table | Purpose |
|---|---|
| `ingestion_job` | Backfill/live/reconcile/gap-recovery job tracking (UUID PK, self-referencing `parent_job_id`) |
| `ingestion_checkpoint` | Resumable job state — `last_successful_timestamp` per `(provider, dataset, instrument_id, exchange, interval)` |
| `candle_bar_quarantine` | Rows from `candle_bar` that could not be classified during migration |

#### Layer 7 — F&O Universe + Derived Series (new in v2.2.0)

| Table | Migration | Purpose |
|---|---|---|
| `fo_universe` | `20260920_000000` | Master F&O instrument registry — 314 rows (293 active, 21 retired). Columns: `instrument_id`, `symbol`, `company_name`, `isin`, `exchange`, `instrument_class`, `sector`, `is_index`, `backfill_priority`, `fo_listed_date`, `fo_delisted_date`, `is_fo_active`, `spot_listed_date`, `spot_delisted_date`. Used by the OHLCV catch-up worker as the instrument source (FO→IDX→EQ priority order). |
| `continuous_futures` | `20260919_000000` | Front-month-rolled continuous futures series — 131,265 rows, 305 underlyings, Sep 2021 → Sep 2026. Columns: `symbol`, `date`, `open`, `high`, `low`, `close`, `volume`, `open_interest`, `roll_date`. |

#### `candle_bar` — deprecated archive

`candle_bar` is **retained as a read-only archive** and marked with a deprecation `COMMENT`. It is not dropped and not structurally modified. No production code writes to it. It contains 5,425,725 rows (all NSE data migrated to `equity_candle`; 6 Binance rows tagged `CRYPTO_PENDING`). Scheduled for removal after 2026-10-15.

#### Production data as of 2026-09-22

| Table | Rows | Notes |
|---|---|---|
| `equity_candle` | **~123M** | 298 instruments, all 9 intervals; Nifty50: 5y intraday; F&O universe: ~2y intraday |
| `futures_candle` | **~302K** | 246,986 bhavcopy 1d (Sep 2021→live) + ~55K broker intraday (near-month) |
| `options_candle` | **242,255** | Bhavcopy 1d, Sep 2021 → live, ~304 underlyings |
| `continuous_futures` | **131,265** | 305 underlyings, Sep 2021 → Sep 2026 |
| `fo_universe` | **314** | 293 active, 21 retired |
| `market_quote` | 3+ | Real live quotes with depth_json |
| `option_greeks_snapshot` | 10+ | NIFTY Sep options with iv/delta/gamma/theta/vega/oi |
| `option_chain_snapshot` | 53+ | NIFTY/BANKNIFTY/FINNIFTY snapshots |
| `option_chain_contract` | 10+ | Per-strike CE/PE rows with Greeks |
| `instrument_provider_mapping` | **70,629** | 34,505 Angel One + 35,940 Upstox NSE_FO keys (1:1 active) |
| `exchange_calendar` | 3,654 | NSE/EQ + NFO/FO, 2024–2028 |
| `fno_universe_membership` | 238 active | Point-in-time F&O eligibility |

### Migration management

```bash
# Apply all pending migrations (current head: 20260920_000000)
docker compose --env-file .env.local exec api alembic upgrade head

# Check current revision
docker compose --env-file .env.local exec api alembic current

# Create a new migration
docker compose --env-file .env.local exec api \
  alembic revision --autogenerate -m "description"
```

> **TimescaleDB hypertables** are created automatically by the Alembic migration (`b1c2d3e4f5a6`) using `create_hypertable(..., if_not_exists => TRUE)`. No manual DDL step is needed.

**Migration chain (key revisions in order):**

| Migration | Description |
|---|---|
| `b1c2d3e4f5a6` | v2 schema — 16 new tables, TimescaleDB hypertables |
| `20260917_000000` | `depth_json`/`source_type` on `market_quote`; columns on `option_greeks_snapshot` |
| `20260918_000000` | `source_timestamp`/`underlying_id` on candle tables; `fc_candle_not_after_expiry` CHECK; `available_at_ms` column |
| `20260919_000000` | `continuous_futures` table |
| `20260920_000000` | `fo_universe` table (current HEAD) |

---

## 10. Layer 6 — REST API + WebSocket

**Module**: `src/api/`, `src/server.py`

### Application factory

`src/server.py:create_app()` builds the FastAPI instance:
1. Configures CORS (`CORS_ALLOWED_ORIGINS` env var — wildcard `*` is prohibited)
2. Adds `CredentialStripperMiddleware` (removes provider secrets from responses)
3. Registers unauthenticated routers: `health`, `metrics`, `auth`, `compat`
4. Registers authenticated routers: all `/v1/*` data routes, each wrapped with `ConsumerAuthDependency`

### Uvicorn configuration

- 4 worker processes (configurable via `UVICORN_WORKERS`)
- `uvloop` event loop for maximum async throughput
- `--no-access-log` (structlog handles all logging)
- Port configurable via `DATA_SERVICE_PORT` (default 8200)

### Response envelope

All API responses use the canonical two-envelope pattern:

```json
// Success
{
  "data": { ... },
  "metadata": {
    "provider": "upstox",
    "score": 87,
    "grade": "HIGH",
    "requestId": "uuid",
    "dataAsOf": "2026-09-15T10:00:00.000Z"
  }
}

// Error
{
  "error": {
    "code": "INVALID_INTERVAL",
    "message": "The 3m interval is permanently unsupported for Indian market data.",
    "requestId": "uuid"
  }
}
```

### WebSocket

`WS /v1/stream/ticks` — real-time tick fan-out.

- Clients connect with `X-API-KEY` header or JWT in the connection query string
- Server pushes `TickEvent` JSON frames: `{symbol, ltp, volume, eventTimeMs, isDuplicate, provenance}`
- Dedup is applied before publish; `isDuplicate: true` ticks are forwarded (not dropped)

---

## 11. Authentication and Security

**Module**: `src/auth/consumer_auth.py`

### Two authentication methods

**API Key** (`X-API-KEY` header):
- Validated against `CONSUMER_API_KEYS` (comma-separated allowlist)
- Plain string comparison — keys are stored in env, never hashed in transit
- Keys are configured per deployment environment

**JWT Bearer** (`Authorization: Bearer <token>`):
- HS256-signed token issued by `GET /v1/auth/token?api_key=<key>`
- Required claims: `sub` (consumer ID), `exp` (expiry timestamp)
- Optional: `scopes` (list of strings)
- Token lifetime: `JWT_EXPIRY_SECONDS` (default 3600s in production, 86400s in local dev)

### `ConsumerAuthDependency`

Applied as a router-level FastAPI dependency on every `/v1/*` data route. Accepts either method. Returns HTTP 401 with canonical error envelope for:
- Missing credentials → `UNAUTHORIZED`
- Invalid API key → `INVALID_API_KEY`
- Invalid token → `INVALID_TOKEN`
- Expired token → `TOKEN_EXPIRED`

### Settings singleton and caching

`get_settings()` is decorated with `@lru_cache(maxsize=1)`. Settings are frozen for the process lifetime. After changing environment variables, the container must be restarted to pick up new values.

**Critical**: `CONSUMER_API_KEYS` and `JWT_SECRET` must be explicitly listed in `docker-compose.yml`'s `environment` block to be injected into the container (they are not automatically inherited from the `.env` file when using `--env-file`).

### Security middleware

- `CredentialStripperMiddleware` — ensures provider API keys, TOTP secrets, and MPIN values never appear in any HTTP response body, regardless of error conditions
- CORS — wildcard `*` is prohibited on all endpoints (Requirement 19.6)
- No `/docs`, `/redoc`, or `/openapi.json` in production (`ENVIRONMENT=production`)

---

## 12. Scheduler and Background Worker

### Scheduler (`src/scheduler.py`, APScheduler + IST timezone)

| Job | Schedule | Purpose |
|---|---|---|
| `fno_universe_refresh` | 08:45 IST daily | Refresh the F&O instrument universe from NSE |
| `angel_one_jwt_rotation` | 23:55 IST daily | Rotate Angel One JWT before market open; new token stored in Redis |
| `catchup_eod_job` | 17:00 IST daily | Trigger OHLCVCatchUpWorker EOD pass (1d/1w/1M intervals) |
| `catchup_intraday_job` | Every `CATCHUP_INTRADAY_INTERVAL` hours (default 4h) | Trigger OHLCVCatchUpWorker intraday pass |
| `clock_skew_monitor` | Every ≤30 seconds | NTP clock-skew sampling (Requirement 18.8) |

### Worker (`src/worker.py` + `src/worker_tasks/ohlcv_catchup.py`)

The worker service now runs the **OHLCVCatchUpWorker** as its primary function:

**Architecture (`src/worker_tasks/ohlcv_catchup.py`)**:
- Loads instrument universe from `fo_universe` DB table, ordered by priority: Index futures → Stock futures → Indices → Nifty50 → Others. Falls back to static `ALL_SPOT` list if `fo_universe` is not yet populated.
- Calls `HistoricalEngine.run_backfill()` — same code path as manual backfill scripts. Engine auto-advances `from_ts` to the Redis checkpoint, so only genuinely missing candles are fetched.
- **EOD pass** — intervals `1d`, `1w`, `1M`; runs once at 17:00 IST and immediately on startup.
- **Intraday pass** — intervals `1m`–`1h`; runs every `CATCHUP_INTRADAY_INTERVAL` hours (default 4). **Skips NSE_INDEX instruments entirely** — Upstox returns UDAPI100011 for index intraday; these are handled by routing logic in `HistoricalEngine`.
- Sessions are created and disposed per cycle (stateless pattern — no memory leak across cycles).

**Configuration env vars:**

| Variable | Default | Description |
|---|---|---|
| `CATCHUP_EOD_INTERVAL_HOURS` | `24` | Hours between EOD passes |
| `CATCHUP_INTRADAY_INTERVAL` | `4` | Hours between intraday passes |
| `CATCHUP_MAX_INSTRUMENTS` | `500` | Max instruments per pass |
| `CATCHUP_CHUNK_DELAY_S` | `0.5` | Seconds between chunk fetches |
| `CATCHUP_INSTRUMENT_DELAY_S` | `0.1` | Seconds between instruments |

Additional worker responsibilities (unchanged):
- Gap detection and recovery (PENDING→RECOVERING→RECOVERED state machine)
- Cross-provider reconciliation jobs
- Provenance persistence retries
- DataIncident archival for EXHAUSTED gaps

### TradingCalendarService (`src/engines/trading_calendar_service.py`)

DB-backed authoritative trading day resolver. Replaces hardcoded weekday logic for all backfill and scheduling decisions.

- **Primary source**: `exchange_calendar` table (populated by `scripts/populate_exchange_calendar.py`)
- **Fallback**: in-memory `HolidayCalendar` when DB is unavailable
- **API**: `is_trading_day(date)`, `get_previous_trading_day(date)`, `get_next_trading_day(date)`, `get_trading_days(start, end)`
- **Design guarantees**: weekend ≠ official holiday; `NOT_PUBLISHED` ≠ trading day; `UNKNOWN` ≠ trading day; no look-ahead bias

All F&O backfill jobs **must** use `TradingCalendarService` rather than inline weekday checks.

---

## 13. Observability Stack

**Module**: `src/observability/`

| Signal | How | Config |
|---|---|---|
| Structured logs | structlog, JSON in production, console in development | `LOG_LEVEL` env var |
| Prometheus metrics | `GET /metrics`, `prometheus-fastapi-instrumentator` | Scraped at `data-service-api:8200/metrics` |
| Distributed tracing | OpenTelemetry SDK | `OTEL_EXPORTER=noop\|jaeger\|otlp` |
| Health dashboard | `GET /v1/health/data` | Session state, gap counts, DCS stats, circuit-breaker states, clock-skew flag |
| Provider health | `GET /v1/analytics/health` | 200 = healthy, 503 = degraded |

### Key log events

| Event | Level | Meaning |
|---|---|---|
| `data_service_ready` | info | Startup complete |
| `redis_connected` | info | Redis pool created |
| `postgres_connected` | info | DB engine created |
| `angel_one_adapter_authenticated` | info | Angel One TOTP+JWT succeeded |
| `angel_one_jwt_stored_in_redis` | info | JWT cached in Redis (TTL 6 h) — other workers will load it |
| `angel_one_jwt_loaded_from_redis` | info | Worker loaded shared JWT from Redis (no TOTP needed) |
| `instrument_master_loaded` | info | InstrumentMasterService loaded from DB into memory |
| `historical_engine_ready` | info | HistoricalEngine pre-wired with shared adapters |
| `upstox_adapter_ready` | info | Upstox access token accepted |
| `ohlcv_catchup_worker_started` | info | OHLCV catch-up worker started; `startup_pass=true` on first run |
| `ohlcv_catchup_pass_complete` | info | Catch-up pass finished; `instruments_processed`, `candles_fetched` |
| `angel_one_token_unknown_upstox_fallback` | info | Auto-routed to Upstox because Angel One token not mapped |
| `api_key_rejected` | warning | Unknown key in X-API-KEY header |
| `circuit_open` | warning | A provider circuit transitioned to OPEN |
| `cache_l2_unavailable` | warning | Redis unavailable; falling back to provider |
| `gap_exhausted` | warning | A gap hit max recovery attempts; DataIncident emitted |
| `poor_quality_dataset` | warning | DCS < 60; dataset persisted but not delivered |

---

## 14. Provider Adapter Reference

All adapters implement `src/providers/adapters/base.py:ProviderAdapter`.

| Adapter | Module | Auth | Data types | Notes |
|---|---|---|---|---|
| **Angel One SmartAPI** | `angel_one.py` | TOTP + JWT (MPIN); Redis-shared JWT across workers | Live quotes, option chain, OHLCV historical, broker analytics (PCR, OI, gainers) | 3 req/s limit; 1m historical max 30 days per chunk; 5m/15m max 90 days. Redis key `mds:angel_one:jwt:{client_id}` (TTL 6 h) prevents TOTP conflicts across 4 Uvicorn workers |
| **Upstox V3** | `upstox.py` | OAuth2 access token (pre-obtained) or Analytics Token (1-year validity) | OHLCV historical V3 (all 9 intervals), LTP V3, OHLC V3, Full Quote **V3** (migrated Apr 2025), Option Greeks V3, Option Chain V2, Market Info V2 (PCR/OI/MaxPain/FII/DII), Smartlist V2 | 1m: 28-day; 5m/15m: 90-day; 1d: 365-day; Protobuf WS (pb2 pending) |
| **NSE Scrapling** | `scrapling_nse.py` | None (public) | Index live quotes via `/api/allIndices` (139+ indices, no JS cookies required); market status | NSE `quote-equity` returns HTTP 403 (Akamai WAF requires JS/behavioral challenge); equity quotes unsupported as of Sep 2026. Use `fetch_all_indices()` for all index symbols (NIFTY, BANKNIFTY, FINNIFTY, etc.) |
| **Jugaad-data** | `jugaad_data.py` | None (public) | NSE historical OHLCV (EOD) | Open-source fallback for Indian historical data |
| **OpenChart** | `openchart.py` | None (public) | NSE intraday OHLCV | Open-source fallback |
| **Yahoo Finance** | `yahoo_finance.py` | None (public) | NSE index historical, some equity | NSE symbol suffix mapping (`.NS`) |
| **Binance REST** | `binance_rest.py` | Optional API key | Spot OHLCV, ticker, 24hr stats, exchange-info; Futures: mark price, funding, OI, L/S ratio | `3m` interval is allowed here |
| **Delta Exchange India** | `delta_exchange.py` | Optional API key | Spot + perpetual OHLCV, tickers, product metadata | Base URL: `api.india.delta.exchange`; candles returned descending — adapter sorts ascending; time in Unix seconds converted to ms |
| **Deribit REST** | `deribit_client.py` | None for public endpoints | Options analytics: IV, OI, max pain, PCR, index price, instrument list | BTC, ETH, SOL currencies |

### WebSocket stream adapters (`src/providers/streams/`)

| Adapter | Protocol | Instruments |
|---|---|---|
| Angel One SmartStream | WebSocket (JSON) | NSE live ticks |
| Upstox V3 Stream | WebSocket + Protobuf | NSE live ticks (l1, l2, full mode) |
| Binance Stream | WebSocket (JSON) | Spot kline streams, mini-ticker |

---

## 15. Data Flow Diagrams

### Live Indian quote request

```
Client  →  FastAPI  →  ConsumerAuth  →  india.py
                                            │
                                    Check L1 → L2 → L3
                                            │ (miss)
                                    MarketEngine
                                            │
                                    AngelOneAdapter.get_quote()
                                            │
                                    Raw JSON response
                                            │
                                    normalise_quote()  (Normaliser)
                                            │
                                    QualityEngine.score()
                                            │
                                    cache_populate (L2+L1)
                                            │
                                    Return {"data": {...}, "metadata": {...}}
```

### WebSocket tick stream

```
Angel One SmartStream WS
        │  raw tick (JSON)
        ▼
StreamingEngine.publish_tick()
        │
        ├─ compute_dedup_hash()  →  DedupStore (24h window)
        │                           set isDuplicate flag
        │
        ├─ Event Bus publish  →  mds:ticks:{symbol}  (Redis Streams)
        │
        └─ L1 LRU update
        
Client WS connection  ←  Event Bus fan-out  ←  /v1/stream/ticks handler
```

### Historical backfill (worker)

```
Worker task: backfill(symbol, interval, from, to)
        │
        ▼
HistoricalEngine.backfill()
        │
        ├─ ProviderGateway.route() → select best provider
        ├─ chunk_request() by maxChunkDays
        │
        ▼
ProviderAdapter.get_ohlcv(chunk)   [repeated per chunk]
        │
        ▼
14-Step Pipeline (per chunk)
        │
        ├─ step 7: gap_detect → create PENDING gap record
        ├─ step 13: persist → write to canonical table
        │           (equity_candle / futures_candle / options_candle)
        │           NEVER writes to candle_bar (deprecated archive)
        └─ step 14: publish DatasetReady event
        
GapRecovery background task
        │
        └─ PENDING → RECOVERING → RECOVERED / EXHAUSTED
```

---

## 16. Configuration and Settings

**Module**: `src/core/settings.py`

Settings are a Pydantic `BaseSettings` instance, loaded once at startup and cached via `@lru_cache(maxsize=1)`.

### Environment variable resolution order

1. OS environment variables (highest priority — Docker Compose injects these)
2. Env file selected by `APP_ENV` (e.g. `.env.local` when `APP_ENV=local`)
3. Pydantic field defaults (lowest priority)

### Key settings

| Variable | Default | Description |
|---|---|---|
| `DATA_SERVICE_PORT` | `8200` | Uvicorn listen port |
| `UVICORN_WORKERS` | `4` | Worker process count |
| `ENVIRONMENT` | `development` | Controls /docs visibility and log format |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `DATABASE_URL` | *(required)* | `postgresql+asyncpg://...` |
| `POSTGRES_PASSWORD` | *(required)* | PostgreSQL password |
| `JWT_SECRET` | *(required in prod)* | HS256 signing key — min 32 chars |
| `JWT_EXPIRY_SECONDS` | `3600` | Token lifetime in seconds |
| `CONSUMER_API_KEYS` | *(required)* | Comma-separated API key allowlist |
| `CORS_ALLOWED_ORIGINS` | `""` | Comma-separated origins (no wildcard) |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Failures before circuit opens |
| `CIRCUIT_BREAKER_RECOVERY_WINDOW_SEC` | `60` | Seconds before OPEN→HALF_OPEN |
| `CACHE_L1_MAX_ENTRIES` | `10000` | In-process LRU max entries |
| `PROVIDER_QUEUE_MAX_DEPTH` | `100` | Max queued requests per provider×capability |
| `GAP_RECOVERY_MAX_ATTEMPTS` | `5` | Max auto-retry attempts per gap |
| `LOG_LEVEL` | `INFO` | `DEBUG\|INFO\|WARNING\|ERROR\|CRITICAL` |
| `OTEL_EXPORTER` | `noop` | `noop\|jaeger\|otlp` |
| `SECRETS_BACKEND` | `env` | `env\|vault\|aws_secrets` |

Full variable reference: [`.env.example`](../.env.example)

---

## 17. Design Invariants

These rules are non-negotiable and enforced at multiple layers simultaneously.

### 1. No consumer bypass
No upstream service may call any external provider directly. Every market data request must flow through DATA-SERVICE 2.0.

### 2. No fabrication / null semantics
`null` means the field is absent. `0` means the field has a zero value. They are never interchangeable.

| Field | Rule |
|---|---|
| `oi` | `null + oiMissing: true` when provider does not supply it. Never derived from `tradedValue`. |
| `iv` | `null + ivMissing: true`. Zero IV is prohibited. |
| `delta`, `gamma`, `theta`, `vega`, `rho` | `null` when absent. Placeholder zeros prohibited. |
| `bid`, `ask` | `null` when not provided. |
| `volume` | `volume: 0, volumeUnavailable: true` when source does not supply it. |

### 3. 3m interval block for Indian market data
The `3m` interval is permanently unsupported for `EQ`, `FO`, and `IDX` instrument classes at every layer: adapter, capability matrix, normaliser, DB constraint, and API. `3m` is explicitly permitted for Binance and Delta Exchange crypto.

### 4. Score cap at 95
`DataConfidenceScore` is always in `[0, 95]`. A score of 100 is never issued — inherent market-data uncertainty.

### 5. BLOCKED datasets are not delivered
Datasets with DCS < 30 are persisted for audit but unconditionally blocked from delivery.

### 6. POOR_QUALITY datasets are persisted but not delivered
Datasets with DCS < 60 are stored in PostgreSQL for audit but suppressed from API responses and Event Bus publication.

### 7. No step-skip without OVERRIDE_REASON
No pipeline step may be bypassed without an explicit `OVERRIDE_REASON` (1–500 chars) and a high-severity alert emitted within 5 seconds.

### 8. Backtest look-ahead prevention
The `point_in_time.py` filter uses `availableAtMs` (not `time`) for strict provenance-based filtering. Records missing `availableAtMs` are rejected, not included.

### 9. Settings singleton immutability
`get_settings()` is `@lru_cache(maxsize=1)`. Settings are frozen for the process lifetime. Container restart is required to pick up env changes.

### 10. Credential stripping
`CredentialStripperMiddleware` ensures provider credentials never appear in any HTTP response body.
