# Implementation Plan: DATA-SERVICE 2.0 — Market Data Platform

## Overview

This plan converts the DATA-SERVICE 2.0 design into an ordered sequence of coding tasks. Each task builds on the previous and ends with all code wired together. Tasks are organised into 17 phases matching the architecture layers. Every task references the specific requirements and design sections it implements.

**Critical invariants that must be enforced at every relevant task:**
- `3m` interval is permanently banned for Indian market data at every layer (acquisition, normaliser, persistence, API)
- `oi` is never populated from `tradedValue`; absent `oi` is `null` + `oiMissing: true`
- `iv`, Greeks (`delta`, `gamma`, `theta`, `vega`, `rho`), `bid`, `ask` are `null` when not provided — zero is never a substitute
- No field is ever fabricated if the upstream source did not supply it
- No runtime dependency on the AlphaForge repository

---

## Tasks

- [x] 1. Project Scaffold and Infrastructure
  - [x] 1.1 Initialise repository layout and Python project configuration
    - Create top-level directory structure: `src/`, `tests/`, `alembic/`, `scripts/`, `docker/`
    - Add `src/` sub-packages: `api/`, `core/`, `engines/`, `providers/`, `cache/`, `db/`, `observability/`, `scheduler/`, `worker/`
    - Write `pyproject.toml` with Python 3.11+ constraint and all dependencies pinned (fastapi, uvicorn[standard], pydantic[email]>=2, httpx, aiohttp, redis[hiredis], asyncpg, sqlalchemy[asyncio]>=2, alembic, structlog, opentelemetry-sdk, prometheus-client, hypothesis, pytest, pytest-asyncio, polars, apscheduler, curl-cffi)
    - Add `src/__init__.py`, `src/server.py` (FastAPI app factory), `src/worker.py`, `src/scheduler.py` entry points
    - _Requirements: 22.1, 22.2, 20.1_

  - [x] 1.2 Implement settings and environment variable management
    - Write `src/core/settings.py` using Pydantic `BaseSettings` with all environment variables listed in the design's configuration table
    - Include `DATA_SERVICE_PORT`, `REDIS_URL`, `DATABASE_URL`, `CIRCUIT_BREAKER_FAILURE_THRESHOLD`, `CIRCUIT_BREAKER_RECOVERY_WINDOW_SEC`, `CACHE_L1_MAX_ENTRIES`, `PROVIDER_QUEUE_MAX_DEPTH`, `GAP_RECOVERY_MAX_ATTEMPTS`, `BACKFILL_CHUNK_*` variables, `CORS_ALLOWED_ORIGINS`, `OTEL_EXPORTER`, `LOG_LEVEL`, `SECRETS_BACKEND`
    - No tunable parameter may be hardcoded; all must be sourced from environment variables
    - _Requirements: 20.7, 19.1_

  - [x] 1.3 Set up structlog structured JSON logging
    - Write `src/observability/logging.py` configuring structlog with JSON renderer in production and console renderer in development
    - Every log entry must include: `timestamp` (UTC ISO-8601), `level`, `service`, `component`, `event`, `requestId`; optional `instrumentId`, `provider`, `durationMs`
    - Write within 100ms; cap entries at 10KB
    - _Requirements: 18.1, 22.6_

  - [x] 1.4 Set up OpenTelemetry distributed tracing
    - Write `src/observability/tracing.py` initialising the OpenTelemetry SDK
    - Support three exporters selectable via `OTEL_EXPORTER` env var: `jaeger`, `otlp`, `noop`
    - Every provider call, cache lookup, and pipeline step must emit a span with `traceId`, `spanId`, `mds.provider`, `mds.instrumentId`, `mds.durationMs`, `mds.step`, `mds.cacheHit`
    - Spans exported within 5 seconds of completion
    - _Requirements: 18.3, 22.7_

  - [x] 1.5 Implement health and Prometheus endpoints
    - Write `src/api/health.py` with three handlers:
      - `GET /v1/health/live` → `{status, version, uptimeMs, timestamp}` always HTTP 200 within 200ms; must not block on any external dependency
      - `GET /v1/health/ready` → HTTP 200 when Redis+PG respond within 2000ms; HTTP 503 with `capabilities` map otherwise
      - `GET /v1/health/data` → session state, freshness stats (P50ms/P99ms/successRate), gap counts, duplicate rates, circuit-breaker states, clock-skew (`clockDegraded` bool)
    - Write `src/api/metrics.py` exposing `GET /metrics` with all Prometheus metrics defined in the design; endpoint responds within 500ms
    - Register all metrics: `mds_request_duration_seconds`, `mds_provider_call_duration_seconds`, `mds_cache_hits_total`, `mds_cache_misses_total`, `mds_circuit_breaker_state`, `mds_tick_publish_rate`, `mds_gap_count`, `mds_quality_score`, `mds_quality_score_distribution`, `mds_duplicate_rate`
    - _Requirements: 16.5, 16.6, 16.7, 18.2, 20.3, 20.4_

  - [x] 1.6 Write Dockerfile and docker-compose
    - Write multi-stage `Dockerfile`: stage 1 installs dependencies; stage 2 is `python:3.11-slim` runtime; expose port 8200
    - Write `docker-compose.yml` with services: `api` (port 8200), `worker`, `scheduler`, `redis:7-alpine`, `timescale/timescaledb:latest-pg15`
    - Add healthchecks for `api`, `redis`, `postgres`
    - Add `.env.example` listing all required variables with documented defaults; no secrets committed
    - _Requirements: 20.1, 20.2, 19.1_

- [x] 2. Database and Cache Foundation
  - [x] 2.1 Set up asyncpg and SQLAlchemy 2.x async engine
    - Write `src/db/engine.py` creating an `AsyncEngine` using `asyncpg` and `DATABASE_URL` from settings
    - Write `src/db/session.py` providing an `AsyncSession` context manager and a `get_session` FastAPI dependency
    - Handle connection-pool exhaustion and PostgreSQL unavailability gracefully (return degraded mode rather than crash)
    - _Requirements: 22.4, 20.2_

  - [x] 2.2 Initialise Alembic and create migration for all six tables
    - Configure Alembic (`alembic.ini`, `alembic/env.py`) to use the async engine
    - Write initial migration creating all six tables with the exact DDL from the design:
      - `candle_bar` with `UNIQUE (instrument_id, exchange, interval_str, time)`, `CHECK (interval_str <> '3m')` constraint, indexes
      - `instrument_master` with all fields and indexes
      - `fno_universe_snapshot` with `UNIQUE (checksum)`
      - `data_gap` with pending-status index
      - `data_incident` with severity and instrument+timestamp indexes
      - `data_provenance` with instrument+received index
      - `provider_health` with provider+capability+timestamp index
    - The `CHECK (interval_str <> '3m')` on `candle_bar` is a non-negotiable defense-in-depth constraint
    - _Requirements: 4.2, 10.11, 20.6_

  - [x] 2.3 Implement TimescaleDB hypertable promotion
    - Write `scripts/promote_timescaledb.sql` containing the single DDL command `SELECT create_hypertable('candle_bar', 'time', chunk_time_interval => INTERVAL '1 day')` with `if_not_exists => TRUE`
    - Write `src/db/timescale.py` that attempts the hypertable promotion on startup if TimescaleDB extension is detected; falls back gracefully if plain PostgreSQL
    - _Requirements: 20.6_

  - [x] 2.4 Implement Redis async client and L2 cache primitives
    - Write `src/cache/redis_client.py` using `redis[hiredis]` async client with connection pool
    - Implement key-namespace helper: `mds:{dataType}:{provider}:{exchange}:{symbol}:{interval}:{from}:{to}` with `_` as placeholder for inapplicable fields
    - Implement `get`, `set_with_ttl`, `delete`, `ttl_remaining` primitives
    - Apply all TTLs from the design: single live quote 3s, batch 3s, intraday 30s, daily+ 4h, option chain 15s, instrument master 12h, provider health 5s
    - _Requirements: 9.2, 9.3_

  - [x] 2.5 Implement L1 in-process LRU cache
    - Write `src/cache/l1_cache.py` implementing an LRU cache with configurable max capacity (default 10,000 entries from `CACHE_L1_MAX_ENTRIES`)
    - On capacity reached, evict the least-recently-used entry before inserting
    - Access latency target ≤ 1ms at p99 (use `collections.OrderedDict` or equivalent)
    - _Requirements: 9.1, 9.8_

  - [x] 2.6 Implement three-level cache hierarchy with request coalescing and stale-while-revalidate
    - Write `src/cache/cache_manager.py` implementing lookup order L1 → L2 → L3 → provider call
    - Implement request coalescing: when multiple concurrent requests arrive for the same cache key while a provider call is in-flight, deduplicate to a single call and broadcast result to all waiters; on provider failure, return error to all waiters
    - Implement stale-while-revalidate: when remaining TTL is within 20% of configured TTL, return cached data immediately and trigger a single background refresh; do not start a second refresh if one is already in progress
    - When serving from L1 or L2, set `dataSourceType: "CACHED"` and preserve original provider in `provenance.sourceChain[0]`
    - When L2 Redis is unavailable, fall back to direct provider call; log `cache_l2_unavailable` warning; do not return error to consumer
    - When a live provider call returns a result, write to L2 then L1 before returning to consumer
    - _Requirements: 9.1, 9.4, 9.5, 9.6, 9.7, 9.9_

- [x] 3. Instrument Master
  - [x] 3.1 Define Instrument canonical Pydantic schemas
    - Write `src/core/schemas/instrument.py` defining `Instrument`, `FnoUniverseSnapshot`, `InstrumentLifecycleEvent` Pydantic v2 models with all fields from the design
    - Define enumerations: `InstrumentType`, `ExchangeEnum`, `SegmentEnum`
    - Provider token fields (`angelToken`, `angelSymbol`, `upstoxKey`, `upstoxSymbol`) must be excluded from consumer responses by default
    - _Requirements: 2.2, 2.7, 11.4_

  - [x] 3.2 Implement InstrumentMaster service with point-in-time tracking
    - Write `src/engines/instrument_master.py`
    - Implement `get_instrument(instrumentId)`, `search_instruments(filters)` respecting `activeFrom`/`activeTo` for point-in-time accuracy
    - Implement `resolve_provider_tokens(instrumentId, provider)` returning the provider-specific token without exposing it in consumer-facing output
    - Implement loading from database snapshot on startup
    - _Requirements: 2.3, 2.7, 2.8_

  - [x] 3.3 Implement F&O universe refresh scheduler
    - Write `src/engines/fno_universe.py`
    - At 08:45 IST on every trading day: fetch F&O eligible list from NSE upstream source, compute SHA-256 checksum of sorted instrument IDs
    - If checksum matches current snapshot → no write, log idempotent skip
    - If checksum differs → write new `FnoUniverseSnapshot`, emit `lifecycleEvent` records (ADDED/REMOVED/SUSPENDED) for each changed instrument
    - On expiry day by 09:15 IST: set `activeTo` on expired contracts; do not delete records
    - If upstream unavailable: retain last successful snapshot; emit structured warning log
    - _Requirements: 11.1, 11.2, 11.3, 11.6, 11.7, 2.4_

  - [x] 3.4 Implement instrument API endpoints
    - Write `src/api/instruments.py` with:
      - `GET /v1/instruments` accepting filters `exchange`, `instrumentType`, `underlying`, `segment`, `expiry`; return empty list with HTTP 200 when no match
      - `GET /v1/instruments/{instrumentId}` for single lookup; HTTP 404 if not found; strip provider tokens unless `?include=providerTokens`
      - `GET /v1/instruments/fno-universe` returning current snapshot; HTTP 503 with `FNO_UNIVERSE_UNAVAILABLE` if no snapshot loaded
      - `GET /v1/instruments/fno-universe/history` returning past snapshots paginated (max 100/page) with `status` and `version` filters
    - All responses use the canonical success envelope
    - _Requirements: 2.5, 2.6, 2.8, 2.9, 11.5_

- [x] 4. Provider Gateway
  - [x] 4.1 Define ProviderCapability schema and Capability Matrix
    - Write `src/core/schemas/provider.py` defining `ProviderCapability`, `ProviderId` enum, `DataType` enum, `SourceType` enum
    - Write `src/providers/capability_matrix.py` containing the full static Capability_Matrix mapping each provider × data type × instrument class to `supported`, `liveSupported`, `historySupported`, `maxChunkDays`, `requestsPerSecond`, `intervalSupport`, `sourceType`, `priority`
    - Encode all provider chunk limits from the design table (Angel One 1m→30d, 5m/15m→90d; Upstox 1m→7d, 5m/15m→30d, 1d→365d; OpenChart→365d; Jugaad→3650d)
    - _Requirements: 5.1, 10.3, 10.4_

  - [x] 4.2 Implement circuit breaker state machine with Redis-backed state
    - Write `src/providers/circuit_breaker.py` implementing `CLOSED → OPEN → HALF_OPEN → CLOSED` transitions
    - State stored in Redis key `mds:cb:{provider}:{capability}` → `{state, opened_at_ms, failure_count}`
    - In `HALF_OPEN`, dispatch exactly one probe request; success → CLOSED, failure → OPEN
    - Configurable failure threshold (1–100) and recovery window (1–3600s) from settings
    - HTTP 429 from provider: do NOT increment failure counter; honour `Retry-After` header or apply 60s default backoff
    - `MARKET_CLOSED` semantics: do NOT count as failure
    - `UNSUPPORTED_CAPABILITY` semantics: do NOT count as failure
    - Emit `circuit_open` structured log event at WARN level within 1 second of `OPEN` transition with `provider`, `capability`, `failureRate`, `transitionTimestamp`
    - _Requirements: 5.4, 5.5, 5.6, 5.7, 5.8, 18.5_

  - [x] 4.3 Implement token-bucket rate limiter with Redis atomic operations
    - Write `src/providers/rate_limiter.py` implementing per-provider token-bucket using Redis Lua scripts for atomic cross-replica consistency
    - `capacity = requestsPerSecond * burst_multiplier` (default 2x); `refill_rate = requestsPerSecond` tokens/second
    - Queue depth limit configurable 1–10,000 (default 100 from settings); overflow returns `PROVIDER_QUEUE_FULL` error immediately
    - _Requirements: 5.2, 5.3_

  - [x] 4.4 Implement provider switch logger
    - Write `src/providers/gateway.py` `_log_provider_switch` method
    - When primary provider fails and fallback is selected, emit structured log with `fromProvider`, `toProvider`, `reason`, `dataset`, `instrumentId`, `timestamp`
    - _Requirements: 5.10_

  - [x] 4.5 Implement Scrapling/NSE provider adapter
    - Write `src/providers/adapters/scrapling_nse.py`
    - Use `curl_cffi` with Chrome TLS fingerprint for WAF bypass; use `Scrapling` for JS-rendered pages
    - Implement: live quote fetch, option chain snapshot fetch, instrument master fetch
    - Credential-free; classify as `OPEN_SOURCE_NSE_DERIVED` source type
    - _Requirements: 5.9, 22.1_

  - [x] 4.6 Implement Angel One SmartAPI provider adapter
    - Write `src/providers/adapters/angel_one.py`
    - Implement TOTP + JWT authentication; JWT rotation at 23:55 IST (see Phase 13)
    - Implement: historical OHLCV fetch (primary for multi-day intraday equity `1m`–`1h`), live quote feed, PCR/OI-buildup/gainers-losers (broker-analytics)
    - Respect 3 req/s rate limit via token-bucket
    - On HTTP 401: trigger JWT rotation and retry once
    - _Requirements: 5.9, 19.7_

  - [x] 4.7 Implement Upstox V2/V3 provider adapter
    - Write `src/providers/adapters/upstox.py`
    - Implement OAuth token management; on provider HTTP 401 → refresh token, retry original request once; if refresh fails → mark provider unavailable
    - Implement: historical OHLCV fetch (primary for index intraday and unsupported Angel One intervals), live quote feed
    - Upstox V3 Protobuf WebSocket handled in Streaming Engine (Phase 8)
    - Respect 10 req/s rate limit
    - _Requirements: 5.9, 19.8_

  - [x] 4.8 Implement Jugaad-data, OpenChart, and Yahoo Finance provider adapters
    - Write `src/providers/adapters/jugaad_data.py`: credential-free; primary for F&O EOD history with OI
    - Write `src/providers/adapters/openchart.py`: credential-free; supplement for all Canonical_Timeframes; reconciliation fallback
    - Write `src/providers/adapters/yahoo_finance.py`: credential-free; restricted to equity EOD historical only; enforce `SECONDARY_FALLBACK` provenance tag; maximum quality grade `B`; blocked from any other use
    - _Requirements: 5.9, 5.12_

  - [x] 4.9 Implement provider health endpoint
    - Write `src/api/providers.py` with `GET /v1/providers/health`
    - Return per-provider, per-capability: `status` (UP/DOWN/DEGRADED/UNKNOWN), `circuitState` (CLOSED/OPEN/HALF_OPEN), `availability`, `latencyP50Ms`, `latencyP99Ms`, `errorRate`, `lastSuccessAt`, `lastFailureReason`, `semanticIntegrity`
    - _Requirements: 5.11_

- [x] 5. Validation Pipeline and Normaliser
  - [x] 5.1 Implement the 14-step Validation Pipeline skeleton
    - Write `src/core/pipeline.py` defining the `ValidationPipeline` class with all 14 steps as ordered methods
    - Enforce the step-skip policy: any configuration that disables a step requires an explicit `OVERRIDE_REASON` audit entry (1–500 chars) and emits a high-severity alert within 5 seconds
    - A dataset that fails at any step does not advance; each step returns `(dataset, ok, incident)` tuple
    - Wire all 14 steps in order: raw receipt → schema validation → normalise → timestamp normalisation → semantic validation → duplicate detection → gap detection → freshness classification → reconciliation → quality scoring → canonical output → cache population → persistence → delivery
    - _Requirements: 17.1, 17.2_

  - [x] 5.2 Implement Normaliser with strict null semantics
    - Write `src/core/normaliser.py`
    - Enforce all null rules from the design (non-negotiable):
      - `oi`: `null` + `oiMissing: true` when provider does not supply it; **never** populated from `tradedValue`
      - `tradedValue`: never interchangeable with `oi`; represents total traded value in INR
      - `iv`: `null` when not provided; zero is not a substitute
      - `delta`, `gamma`, `theta`, `vega`, `rho`: `null` when not provided; placeholder zeros prohibited
      - `bid`, `ask`: `null` when not provided; placeholder zeros prohibited
      - `volume` when source does not supply it: `volumeUnavailable: true`, `volume = 0`
    - Implement partial response handling: process valid fields, set invalid fields to `null` with missing flags, continue processing
    - On complete schema validation failure: log structured error with `provider`, `endpoint`, `rawResponseHash`, `validationErrors`, `receivedAt`; drop response
    - Attach `normalisationVersion` semver string to every provenance record
    - Annotate every analytics field with `MetricTag` (OBSERVED / DERIVED / MODELLED); default to OBSERVED when undetermined
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.9, 6.10, 6.11, 6.12_

  - [x] 5.3 Implement OHLCV candle invariant enforcement
    - Write `src/core/validators/ohlcv.py`
    - Enforce: `high >= max(open, close)`, `low <= min(open, close)`, `volume >= 0`, all price values `> 0`
    - Reject any candle failing these invariants; generate `DataIncident` with `instrumentId`, `intervalStr`, `timestamp`, `failedInvariant`, `rejectedValues`, `detectedAt`
    - Block `3m` interval at this layer for Indian market data: raise `ValueError` before processing
    - _Requirements: 4.3, 1.5, 17.1_

  - [x] 5.4 Implement timestamp normalisation (pipeline step 4)
    - Write `src/core/validators/timestamps.py`
    - Convert all provider-supplied timestamps to UTC epoch milliseconds using the configured exchange timezone (`zoneinfo.ZoneInfo("Asia/Kolkata")` for Indian markets, UTC for crypto)
    - Timestamps that cannot be normalised → reject dataset with `DataIncident` containing raw timestamp value, source identifier, and rejection reason
    - All API response timestamps formatted as UTC ISO-8601 strings with `Z` suffix
    - IST interpretation occurs only inside the Market Engine's session-management component
    - _Requirements: 6.7, 6.8, 17.3_

  - [x] 5.5 Implement semantic validation (pipeline step 5)
    - Write `src/core/validators/semantic.py`
    - Validate OI≠tradedValue substitution: if `oi` field value equals `tradedValue` field value and OI was not directly supplied by provider, reject and set `oi: null`, `oiMissing: true`
    - Validate IV not zero-substituted: if `iv == 0.0` and provider did not explicitly supply 0.0 as a value, set `iv: null`
    - Validate Greeks not zero-substituted
    - Validate bid/ask not zero-substituted
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.6, 3.3_

  - [x] 5.6 Implement duplicate detection (pipeline step 6)
    - Write `src/core/validators/dedup.py`
    - Compute deterministic deduplication ID: `SHA-256(instrumentId:eventTimeMs:source:ltp:volume)[:32]`
    - Maintain 24-hour rolling window of seen hashes in Redis
    - Duplicate datasets: set `isDuplicate: true`, do NOT persist, count in quality statistics
    - Preserve duplicates in Event Bus publish with `isDuplicate: true` for auditability
    - _Requirements: 3.6, 17.4_

  - [x] 5.7 Implement gap detection (pipeline step 7)
    - Write `src/core/validators/gap_detection.py`
    - Compare received candle `time` values against expected sequence for the given interval
    - Emit `DataGapEvent` with `gapStartMs`, `gapEndMs`, `expectedCount`, `actualCount`, and `severity` (LOW when 1 missing, MEDIUM when 2–5, HIGH when >5)
    - _Requirements: 17.5_

  - [x] 5.8 Implement Deribit instrument name parser with round-trip validation
    - Write `src/core/parsers/deribit_parser.py`
    - Parse `{CURRENCY}-{DD}{MON}{YY}-{STRIKE}-{C|P}` into canonical fields: `baseCurrency`, `expiryTs` (UTC epoch ms at 08:00 UTC on expiry date), `strike`, `optionType` (C→CE, P→PE)
    - Instruments that fail parsing: silently drop with logged warning
    - Implement `serialise(canonical) -> str` and verify round-trip: `currency`, `strike`, `optionType` (as C/P), expiry as `{DD}{MON}{YY}` (day zero-padded, month uppercase 3-letter)
    - _Requirements: 14.3, 14.7_

  - [x] 5.9 Implement round-trip parser property enforcement
    - Write `src/core/parsers/round_trip.py` exposing a `verify_round_trip(raw, parser, serialiser)` utility
    - This enforces `parse(serialise(parse(raw))) == parse(raw)` for all valid inputs
    - Integrate into: JSON parser, Upstox Protobuf parser, bhavcopy CSV parser, NSE charting response parser
    - Add this check as part of the validation pipeline step 3 (normalise) for each parser type
    - _Requirements: 4.11, 17.8_

  - [x] 5.10 Implement POOR_QUALITY flag and pipeline delivery control
    - Write `src/core/pipeline.py` `_apply_poor_quality_rule` method
    - Datasets with `DataConfidenceScore < 60` are flagged `POOR_QUALITY`
    - `POOR_QUALITY` datasets: persist to L3 with the flag, do NOT deliver via API or Event Bus
    - _Requirements: 17.7_

- [x] 6. NSE Market Session Engine
  - [x] 6.1 Implement NSE session phase state machine
    - Write `src/engines/market_session.py` with `MarketSessionEngine`
    - Use `zoneinfo.ZoneInfo("Asia/Kolkata")` as the sole IST reference; all other timestamps remain UTC
    - Classify every IST instant into exactly one phase (inclusive start, exclusive end boundaries):
      - `PRE_OPEN`: 09:00–09:08
      - `PRE_OPEN_CALL_AUCTION`: 09:08–09:15
      - `REGULAR`: 09:15–15:30 (13:00 on half-days)
      - `POST_MARKET`: 15:30–16:00 (13:00–13:30 on half-days)
      - `CLOSED`: all other instants, full NSE holidays, weekends
      - `MUHURAT`: Diwali Muhurat session only
    - On NSE holidays and full non-trading days: always return `CLOSED`
    - _Requirements: 12.1, 12.2_

  - [x] 6.2 Implement NSE holiday calendar management
    - Write `src/engines/holiday_calendar.py`
    - Fetch NSE official holiday API before the first trading session of each new calendar year; cache in Redis + in-memory
    - If unavailable: retain most recently fetched calendar; expose `calendarStatus` in `/v1/india/market/status` as date of last successful refresh
    - Implement `is_trading_day(date)` and `next_trading_day(date)` helpers
    - _Requirements: 12.3, 12.4_

  - [x] 6.3 Implement live quote acquisition and normalisation
    - Write `src/engines/market_engine.py` `get_live_quote(instrumentId)` method
    - During `REGULAR` session: acquire from broker WebSocket feed (Angel One SmartStream or Upstox) and publish normalised quote within 500ms of event timestamp
    - Include all required fields: `instrumentId`, `symbol`, `exchange`, `ltp`, `open`, `high`, `low`, `prevClose`, `change`, `changePct`, `volume`, `oi` (F&O only — null for equities, never from tradedValue), `tradedValue`, `totalBuyQty`, `totalSellQty`, `upperCircuit`, `lowerCircuit`, `weekHigh52`, `weekLow52`, `lastTradeTime`, `bid` (null when unavailable), `ask` (null when unavailable), full `provenance`
    - When session is `CLOSED`: return last available quote with `marketStatus: "CLOSED"`; do NOT classify as provider failure or increment circuit-breaker counters
    - When session is `CLOSED`/`PRE_OPEN`/`POST_MARKET`: return most recent `REGULAR`-session data (no older than 24 hours)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 12.5_

  - [x] 6.4 Implement option chain pipeline
    - Write `src/engines/market_engine.py` `get_option_chain(underlying, expiry)` method
    - Acquire and publish option chain snapshot within 5 seconds of provider update
    - Per-contract fields: `strike`, `optionType`, `ltp`, `bid`, `ask`, `oi`, `oiChange`, `volume`, `tradedValue`, `iv` (null when unavailable — zero is never a substitute), `delta`, `gamma`, `theta`, `vega`, `rho`, completeness flags (`oiMissing`, `oiChangeMissing`, `volumeMissing`, `ivMissing`)
    - If underlying spot price is older than 60s → set `chainQuality: "DEGRADED"`, include `spotAgeMs`
    - Compute analytics: `pcrOi`, `pcrVolume`, `maxCeOiStrike`, `maxPeOiStrike`, `totalCeOi`, `totalPeOi`, `atmIv`, `maxPain`
    - Tag each computed field with `MetricTag` (OBSERVED/DERIVED/MODELLED)
    - `GET /v1/india/option-chain`: if market closed → `rows: []` with `marketStatus: "CLOSED"` (not HTTP 5xx); if no contracts listed → `rows: []` with `marketStatus: "NO_DATA"`
    - _Requirements: 3.7, 3.8, 3.9, 3.10_

  - [x] 6.5 Implement market status API endpoint
    - Write `src/api/india.py` `GET /v1/india/market/status` handler
    - Return: `sessionPhase`, `nextSessionChange` (UTC ISO-8601), `tradingDay` (bool), `nextTradingDay` (IST date as `YYYY-MM-DD`), `holidays` (remaining NSE holiday dates within current IST calendar month), `calendarStatus` (date of last successful holiday calendar refresh)
    - _Requirements: 12.6_

- [x] 7. Historical Engine
  - [x] 7.1 Implement resumable checkpointed backfill
    - Write `src/engines/historical_engine.py` `run_backfill(symbol, exchange, interval, from_ts, to_ts)` method
    - Checkpoint key: `mds:backfill:checkpoint:{symbol}:{exchange}:{interval}` (no TTL — persistent)
    - On each successful chunk: persist candles, update checkpoint with UTC timestamp of last persisted candle
    - On interruption (process termination, unhandled exception, provider timeout >30s): resume from last checkpoint on next run
    - Chunk acquisition loop: `for date_range in chunk_date_ranges(from_ts, to_ts, max_chunk_days): fetch → validate → bulk_upsert → update_checkpoint`
    - Enforce `3m` interval hard block at acquisition planner: raise `ValueError` before any I/O for Indian market requests with `interval="3m"`
    - Route acquisition per Capability_Matrix: Angel One for equity intraday, Upstox for index intraday, OpenChart for reconciliation, Jugaad for F&O EOD
    - Minimum history depths: 60 calendar days for `1m`, 180d for `5m`–`30m`, 365d for `1h`, 10 years for `1d`–`1M`
    - _Requirements: 4.1, 4.2, 4.6, 10.1, 10.2, 10.3, 10.4, 10.11_

  - [x] 7.2 Implement cross-provider OHLCV reconciliation
    - Write `src/engines/historical_engine.py` `reconcile(instrumentId, exchange, interval, timestamp, a_values, b_values)` method
    - Deviation formula: `|A − B| / max(|A|, |B|) × 100`
    - `≤ 0.5%` for all OHLCV fields → `CONFIRMED`
    - Any field between `0.5%` and `2.0%` → `MINOR_DISCREPANCY`
    - Any field exceeds `2.0%` → `MAJOR_DISCREPANCY`
    - Overall tuple status = worst-case field deviation
    - `MAJOR_DISCREPANCY` → generate `DataIncident`
    - _Requirements: 10.5, 10.6, 10.7_

  - [x] 7.3 Implement gap detection and recovery state machine
    - Write `src/engines/gap_recovery.py`
    - Gap record schema: `DataGap` with `gapId`, `instrumentId`, `exchange`, `intervalStr`, `gapStart`, `gapEnd`, `durationSec`, `recoveryStatus`, `recoveryAttempts`, `expectedProvider`, `recoveryProvider`
    - Recovery state machine: `PENDING → RECOVERING → RECOVERED | EXHAUSTED`
    - Max recovery attempts: configurable (default 5, range 1–10); after max reached → `EXHAUSTED`, retain for manual review, emit alert, no further auto-retry
    - Persist gap records to `data_gap` table
    - _Requirements: 4.7, 4.8, 10.8, 10.10_

  - [x] 7.4 Implement historical API endpoints
    - Write `src/api/india.py` historical handlers:
      - `GET /v1/india/historical` accepting `symbol`, `exchange`, `interval`, `from`, `to`; max 10,000 records; `metadata.truncated: true` when limit reached; include `metadata.provider`, `metadata.provenance`, `metadata.quality`, `metadata.gaps`, `metadata.dataAsOf`; reject `interval=3m` with HTTP 400
      - `GET /v1/india/historical/status` returning timeframe coverage, gap summary, provider activity, reconciliation status, supported timeframes list
      - `GET /v1/india/historical/gaps` accepting `symbol`, `interval`, `status` (PENDING/RECOVERED/FAILED), `limit` (default 100, max 1000)
      - `GET /v1/india/historical/reconciliation` returning `totalCompared`, `matched`, `matchRatePct`, `distribution`, `byProviderPair`
    - _Requirements: 4.9, 4.10, 10.8, 10.9_

- [x] 8. Streaming Engine and Event Bus
  - [x] 8.1 Implement Redis Streams Event Bus topology
    - Write `src/cache/event_bus.py` with publish/consume helpers for all 9 stream keys
    - Streams: `mds:events:ticks` (24h), `mds:events:candles` (24h), `mds:events:option-chain` (24h), `mds:events:dataset-ready` (24h), `mds:events:incidents` (7d), `mds:events:quality` (24h) plus `mds:events:provider-switch`, `mds:events:reconnect`, `mds:events:connection-failed`
    - At-least-once delivery semantics; sequence numbers for consumer deduplication
    - Support at least 500 concurrently subscribed symbol tick streams; exceed limit → return error
    - _Requirements: 15.1, 15.4_

  - [x] 8.2 Implement Angel One SmartStream WebSocket adapter
    - Write `src/providers/streams/angel_one_stream.py`
    - The Streaming Engine is the sole owner of this connection; no consumer can connect directly
    - Reconnect policy: exponential backoff base 1s, doubles each attempt, max 60s, max 10 attempts
    - On each reconnect attempt: publish `{"type": "reconnect", "attempt": N}` to Event Bus
    - After exhaustion: publish `{"type": "connection_failed", "provider": "angel_one"}`
    - Decoded ticks: normalise to canonical schema before publishing
    - _Requirements: 15.9_

  - [x] 8.3 Implement Upstox V3 Protobuf WebSocket adapter
    - Write `src/providers/streams/upstox_stream.py`
    - Same reconnect policy as Angel One SmartStream (base 1s, max 60s, max 10 attempts)
    - Protobuf decoding inside the Streaming Engine; decoded fields normalised to canonical schema before publishing
    - _Requirements: 15.7, 15.9_

  - [x] 8.4 Implement Binance WebSocket adapter
    - Write `src/providers/streams/binance_stream.py`
    - Exponential backoff base 1s, max 30s; heartbeat pings every 30 seconds
    - On `error` or `closed` state: log `symbol`, `reason`, `reconnectAttempt`; do NOT expose raw WebSocket error to consumers
    - When established/re-established: publish received mini-ticker ticks to Event Bus within 2 seconds of connection confirmation
    - _Requirements: 13.5, 13.6, 13.11_

  - [x] 8.5 Implement tick publisher with deduplication
    - Write `src/engines/streaming_engine.py` `publish_tick(tick)` method
    - Tick format: `tickId`, `instrumentId`, `symbol`, `exchange`, `eventTimeMs`, `receivedAtMs`, `ltp`, `change`, `changePct`, `volume`, `oi`, `tradedValue`, `source`, `quality`, `isDuplicate`
    - Deduplication ID: `SHA-256(instrumentId:eventTimeMs:source:ltp:volume)[:32]`
    - Duplicate ticks: publish with `isDuplicate: true` (not silently dropped — audit trail)
    - Publish latency from `receivedAtMs` to Event Bus ≤ 200ms at p99 (tick publisher)
    - Publish latency from upstream provider WebSocket to Event Bus ≤ 500ms at p99
    - Channel pattern: `mds:ticks:{symbol}`
    - _Requirements: 3.5, 3.6, 15.2_

  - [x] 8.6 Implement consumer WebSocket endpoint with subscription management
    - Write `src/api/streaming.py` with `WS /v1/stream/ticks`
    - Accept control messages: `{"action": "subscribe", "symbols": [...]}` and `{"action": "unsubscribe", "symbols": [...]}`
    - Heartbeat messages every 10 seconds; if no heartbeat response for 3 consecutive heartbeats within 30s window → close connection, unsubscribe all streams
    - On connection close: unsubscribe within 5 seconds; decrement `subscribedSymbols` count
    - `GET /v1/stream/status` returning `subscribedSymbols`, `ticksPublished`, `validationFailures`, `lastPublishedAt`, `brokerConnections` (Angel One SmartStream and Upstox V3 connection state)
    - _Requirements: 15.5, 15.6, 15.7, 15.8_

  - [x] 8.7 Implement dataset-ready event publisher
    - Write `src/engines/streaming_engine.py` `publish_dataset_ready(event)` method
    - When backfill chunk, option chain snapshot, or gap recovery completes: publish `dataset_ready` event with `dataType`, `instrumentId`, `exchange`, `intervalStr`, `fromTs`, `toTs`, `rowCount`, `provider`
    - _Requirements: 15.3_

- [x] 9. Quality Engine
  - [x] 9.1 Implement DataConfidenceScore formula
    - Write `src/engines/quality_engine.py` `compute_confidence_score(freshness, completeness, provider_health, timestamp_valid, cross_source_agreement, sequence_integrity)` method
    - Score = freshness (0–35, weight 35%) + completeness (0–25) + provider_health (0–20) + timestamp_score (0–10) + agreement_score (0–10)
    - Cap at 95 — never 100 (inherent market-data uncertainty)
    - Freshness scoring: FRESH→35, AGING→25, STALE→10, EXPIRED→0, UNKNOWN→5
    - If sequence integrity broken: `score = int(score * 0.8)` (20% penalty)
    - `DataConfidenceScore < 30` → grade `BLOCKED` → `signalEngineAllowed: false` with no exceptions
    - _Requirements: 7.1, 7.11_

  - [x] 9.2 Implement DataQualityGate with five conditions
    - Write `src/engines/quality_engine.py` `evaluate_gate(...)` method returning `DataQualityGate`
    - Five boolean conditions: `dataFresh`, `dataComplete`, `dataTimestampValid`, `dataProviderHealthy`, `dataSemanticallyValid`
    - `signalEngineAllowed = true` if and only if all five conditions are `true`
    - When `signalEngineAllowed` is `false`: include at least one `blockReason` string explaining which condition failed and the measured value
    - _Requirements: 7.2, 7.9_

  - [x] 9.3 Implement freshness threshold classification
    - Write `src/engines/quality_engine.py` `classify_freshness(eventTimeMs, instrumentType, sessionPhase)` method
    - REGULAR session thresholds: Index ≤10s → FRESH; F&O liquid ≤10s → FRESH; F&O normal ≤15s → FRESH; Equity ≤30s → FRESH
    - Non-REGULAR session extended thresholds: Index ≤60s; F&O liquid ≤60s; F&O normal ≤90s; Equity ≤120s
    - _Requirements: 7.3, 7.4_

  - [x] 9.4 Implement quality classification
    - Write `src/engines/quality_engine.py` `classify_quality(score, fresh, complete)` returning `DataQuality` enum
    - `VALID`: score ≥ 80 and fresh and complete
    - `DEGRADED`: score 50–79
    - `PARTIAL`: completenessPercent in [40, 99] with at least one required field missing
    - `STALE`: data outside freshness window
    - `INVALID`: fundamental integrity failure
    - `UNKNOWN`: cannot be determined
    - _Requirements: 7.5_

  - [x] 9.5 Implement option chain quality checks
    - Write `src/engines/quality_engine.py` `validate_option_chain_record(record)` method
    - Reject records with: crossed markets (`bid > ask`), negative IV, negative OI, invalid Greeks (`|delta| > 1`, `gamma < 0`)
    - Each violation generates a `DataIncident` record with `symbol`, `violationType`, `measuredValue`, `detectionTimestamp`
    - _Requirements: 7.10_

  - [x] 9.6 Implement strategy-specific quality overrides
    - Write `src/engines/quality_engine.py` `apply_strategy_overrides(gate, strategy_config)` method
    - Strategy declares: `maxQuoteAgeMs`, `minConfidenceScore` (minimum 30), `requiresOI`, `requiresOptionChain`, `requiresConfirmedCandle`
    - Strategy overrides apply in addition to the global gate; strategy overrides cannot relax any global gate condition
    - _Requirements: 7.8_

  - [x] 9.7 Implement quality API endpoints
    - Write `src/api/quality.py` with:
      - `POST /v1/quality/gate`: accept `symbol`, `quoteAgeMs`, `completenessPercent`, `timestampValid`, `crossSourceAgreement`, `sequenceIntegrity`, optional strategy overrides; HTTP 400 if required field missing or out of range; return `signalEngineAllowed`, `confidenceScore`, `quality`, `gates`, `blockReasons`, `circuitBreakers`
      - `GET /v1/quality/gate/{symbol}`: current gate state per symbol for dashboard monitoring
    - _Requirements: 7.6, 7.7_

  - [x] 9.8 Implement clock-skew monitor
    - Write `src/observability/clock_monitor.py` sampling NTP offset at intervals ≤ 30 seconds
    - When skew exceeds 500ms: emit `clock_skew_warning` event with `measuredSkewMs`, `timestamp`; set `clockDegraded: true` in `/v1/health/data`
    - When returns to ≤ 500ms: set `clockDegraded: false`
    - _Requirements: 18.8_

  - [x] 9.9 Implement quality alerting
    - Write `src/observability/quality_alerting.py`
    - When `DataConfidenceScore` drops below 50 for any active instrument: emit `quality_degraded` event at WARN level with `instrumentId`, `score`, `previousScore`, `blockReasons`
    - When score rises back to ≥ 50: emit `quality_restored` event with `instrumentId`, `score`, `previousScore`
    - _Requirements: 18.6_

- [x] 10. Data Provenance and Lineage
  - [x] 10.1 Implement DataProvenance schema and observation ID assignment
    - Write `src/core/schemas/provenance.py` defining `DataProvenance` Pydantic model with all fields: `dataObservationId` (UUID v4), `source`, `sourceVersion`, `eventTimeMs`, `receivedAtMs`, `availableAtMs`, `normalisationVersion`, `validationApplied`, `isFallback`, `fallbackReason`, `sourceChain`
    - Assign a unique UUID v4 `dataObservationId` to every market data observation at the moment of ingestion from the provider
    - _Requirements: 8.1, 8.2_

  - [x] 10.2 Implement lineage store (in-memory LRU + PostgreSQL)
    - Write `src/engines/lineage_store.py`
    - In-memory LRU: most recent 100,000 observation records (evict in insertion order at limit)
    - PostgreSQL: persist all records indefinitely until explicitly deleted by authorised admin
    - Persist to database within 1,000ms of successful data acquisition
    - On persistence failure: retry up to 3 times at 500ms intervals; log persistence failure; discard if all retries fail
    - _Requirements: 8.3, 8.6_

  - [x] 10.3 Implement trade forensics provenance recording
    - Write `src/engines/lineage_store.py` `record_trade_provenance(trade_record, provenance)` method
    - When a paper trade or signal is generated, record: `dataObservationId`, `quoteAgeAtEntryMs`, `dataConfidenceAtEntry`, `dataQualityAtEntry`, `dataProviderAtEntry`, `dataIsFallback`, `observationEventTime` in the trade record
    - _Requirements: 8.7_

  - [x] 10.4 Implement provenance and lineage API endpoints
    - Write `src/api/lineage.py` with:
      - `GET /v1/lineage/{observationId}` → return full lineage record within 500ms; HTTP 404 if not found
      - `GET /v1/lineage/instrument/{instrumentId}?limit=N` → most recent N records ordered by `receivedAtMs` DESC; N must be 1–1000; HTTP 400 if absent or out of range; include `storeSize` and `totalRecorded` in response
      - `GET /v1/lineage/forensics/{tradeId}` → joined trade+signal+provenance+lineage within 1000ms; HTTP 404 identifying which record is missing if any
    - All provenance fields are read-only; any mutation request → HTTP 405; existing record unchanged
    - _Requirements: 8.4, 8.5, 8.8, 8.9_

- [x] 11. Crypto Data — Binance
  - [x] 11.1 Implement Binance REST client
    - Write `src/providers/adapters/binance_rest.py`
    - Implement: spot OHLCV klines for BTC/ETH/SOL (intervals: `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d` — `3m` is allowed for Binance crypto only, document this exception explicitly)
    - Implement: perpetual futures data (`markPrice`, `indexPrice`, `fundingRate`, `fundingRateAnnualized`, `nextFundingTime`, `openInterest`, `openInterestNotionalUsd`)
    - Implement: OI history (periods: `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `12h`, `1d`) with `ts`, `openInterest`, `notionalUsd`
    - Implement: long/short account ratio with `ts`, `longShortRatio`, `longAccount`, `shortAccount`
    - Respect 20 req/s rate limit
    - If REST request fails after 3 consecutive attempts: discard, emit error event to Event Bus with affected symbol and data type; preserve existing stored data
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.9_

  - [x] 11.2 Implement Binance OHLCV validation and persistence
    - Write validation for Binance candles: OHLC invariants before persistence; store `openTime` (UTC epoch ms), `open`, `high`, `low`, `close`, `volume` (base asset), `closeTime` in canonical `OHLCVCandle` schema
    - If OHLC invariant validation fails: reject record, log violation with `symbol`, `interval`, `openTime`; do NOT persist
    - _Requirements: 13.8, 13.10_

  - [x] 11.3 Implement Binance crypto API endpoints
    - Write `src/api/crypto.py` with:
      - `GET /v1/crypto/klines/{symbol}` accepting `interval`, `from`, `to`
      - `GET /v1/crypto/futures/overview` returning `markPrice`, `fundingRate`, `fundingRateAnnualized`, `nextFundingTime`, `openInterest`, `openInterestNotionalUsd`, `oiChangePct1h`, `longShortRatio`, `longAccount`, `shortAccount` for all tracked symbols
      - `GET /v1/crypto/futures/funding-history/{symbol}`
      - `GET /v1/crypto/futures/oi-history/{symbol}` accepting `period`
      - `GET /v1/crypto/futures/long-short/{symbol}`
    - _Requirements: 13.7_

- [x] 12. Crypto Data — Deribit
  - [x] 12.1 Implement Deribit REST client
    - Write `src/providers/adapters/deribit_rest.py`
    - Implement: options book summary for BTC/ETH/SOL with all per-instrument fields: `instrumentName`, `markPrice`, `markIv`, `openInterest`, `volume`, `volumeUsd`, `underlyingPrice`, `last`, `bid`, `ask`
    - Implement: index price via `get_index_price` endpoint returning `index_price` as float
    - Preserve `null` for `mark_iv`, `open_interest`, `volume`, `underlying_price` when Deribit returns null; do NOT substitute zero
    - Refresh interval ≤ 60 seconds; on failure: retain most recent successful data; emit structured warning log
    - Respect 5 req/s rate limit
    - _Requirements: 14.1, 14.2, 14.5, 14.8_

  - [x] 12.2 Implement OptionsOverview computation for Deribit
    - Write `src/engines/deribit_engine.py` `compute_options_overview(currency, book_summary, index_price)` method
    - Compute: `underlyingPrice`, `totalCallOi`, `totalPutOi`, `totalCallVolume`, `totalPutVolume`
    - `pcrOi`: null when total call OI is zero or null (not zero — null to signal unavailability)
    - `pcrVolume`: null when total call volume is zero or null
    - `atmIv`: IV of contract with strike closest to `underlyingPrice` on the nearest expiry
    - Per-expiry `ExpiryStats`: `maxPainStrike` (strike minimising aggregate OI monetary loss), `pcrOi`, `pcrVolume`, `topStrikes` (top 5 by combined OI), `daysToExpiry` (whole calendar days rounded down)
    - _Requirements: 14.4_

  - [x] 12.3 Implement Deribit API endpoints
    - Write `src/api/crypto.py` Deribit handlers:
      - `GET /v1/crypto/options/{currency}/overview` returning `OptionsOverview` with `provenance` and `quality` metadata; if currency not in BTC/ETH/SOL → HTTP 400 listing supported currencies
      - `GET /v1/crypto/options/{currency}/book` returning book summary
    - _Requirements: 14.6_

- [x] 13. Security
  - [x] 13.1 Implement credential stripping middleware
    - Write `src/api/middleware/credential_stripper.py` as FastAPI middleware
    - Before serialising any API response, recursively remove all fields whose names contain (case-insensitive): `"key"`, `"token"`, `"secret"`, `"password"`, `"credential"`
    - Applies regardless of originating handler or data source
    - _Requirements: 16.8, 19.4_

  - [x] 13.2 Implement consumer authentication (API key + JWT bearer)
    - Write `src/api/middleware/auth.py`
    - Require valid API key or JWT bearer token in `Authorization` header for all production API endpoints
    - Invalid, malformed, or expired credentials → HTTP 401 without disclosing token details or internal state
    - _Requirements: 19.2_

  - [x] 13.3 Implement consumer rate limiting
    - Write `src/api/middleware/rate_limiter.py`
    - Per-consumer rate limit configurable; maximum 10,000 requests/minute per consumer
    - Exceeded → HTTP 429 with `Retry-After` header indicating seconds until reset
    - _Requirements: 19.3_

  - [x] 13.4 Implement CORS allowlist middleware
    - Write `src/api/middleware/cors.py` using FastAPI's `CORSMiddleware`
    - Allowlist loaded from `CORS_ALLOWED_ORIGINS` env var (comma-separated)
    - Wildcard CORS (`*`) is prohibited on production endpoints
    - _Requirements: 19.6_

  - [x] 13.5 Implement Angel One JWT rotation scheduler
    - Write `src/scheduler/angel_one_jwt_rotation.py`
    - Schedule rotation at 23:55 IST daily using APScheduler
    - On failure: retry up to 3 times at 60-second intervals; raise alert if all retries exhausted
    - _Requirements: 19.7_

  - [x] 13.6 Implement Upstox OAuth 401 refresh handler
    - Wire into `src/providers/adapters/upstox.py` (already scaffolded in Phase 4)
    - When Upstox returns HTTP 401: immediately attempt OAuth token refresh; retry original request once with new token
    - If refresh also fails: mark Upstox provider as unavailable; return error indicating provider authentication failure
    - _Requirements: 19.8_

  - [x] 13.7 Implement canonical error response envelope with shell command safety
    - Write `src/api/errors.py` implementing `format_error_response(code, message, provider, retryAfterMs, requestId)` returning the canonical error envelope: `{ "error": { "code", "message", "provider", "retryAfterMs", "requestId" } }`
    - No stack traces, no credentials, no internal file paths in any error response
    - When constructing shell commands or HTTP requests with provider-supplied values (symbol names, dates, query params): use parameterised construction only; no string interpolation
    - Implement HTTP status code mapping from the design table: 400/401/403/404/405/429/502/503
    - _Requirements: 16.3, 16.4, 19.5_

- [x] 14. AlphaForge Integration Endpoints
  - [x] 14.1 Implement broker analytics endpoints
    - Write `src/api/india.py` broker-analytics handlers:
      - `GET /v1/india/broker-analytics/pcr` sourced from Angel One SmartAPI; return payload with `provenance` and `quality` metadata
      - `GET /v1/india/broker-analytics/oi-buildup` sourced from Angel One SmartAPI
      - `GET /v1/india/broker-analytics/gainers-losers` sourced from Angel One SmartAPI
    - If Angel One SmartAPI call fails: return HTTP 502 with `PROVIDER_UNAVAILABLE`; do NOT return stale data without explicit `stale: true` metadata
    - _Requirements: 21.3, 21.4_

  - [x] 14.2 Implement DataParityContract model and endpoint
    - Write `src/core/schemas/parity.py` defining `DataParityContract` Pydantic model with `contractVersion`, `liveDataPath`, `paperDataPath`, `replayDataPath`, `backtestDataPath`, `parityVerified` (bool), `lastVerifiedAt` (ISO 8601 or null)
    - If parity contract never verified: `parityVerified: false`, `lastVerifiedAt: null`
    - Implement verification logic: if all modes share the same normalisation/validation pipeline → `parityVerified: true`, update `lastVerifiedAt`; if any mode diverges → `parityVerified: false`, do not update `lastVerifiedAt`
    - Divergence from parity contract is a critical finding that blocks release
    - `GET /v1/parity/contract` endpoint
    - _Requirements: 23.1, 23.2, 23.6_

  - [x] 14.3 Implement look-ahead bias prevention for backtest mode
    - Write `src/engines/backtest_engine.py` `get_backtest_data(symbol, interval, T)` method
    - For time `T`: return only records with `availableAtMs ≤ T`
    - Records missing `availableAtMs` or with unparseable values: reject from backtest dataset; log record identifier and rejection reason
    - _Requirements: 23.4, 23.5_

  - [x] 14.4 Implement replay mode
    - Write `src/engines/replay_engine.py` serving historical data at a configurable `playbackMultiplier` (positive float; 1.0 = real-time; >1.0 accelerates)
    - Response envelope identical to live: `dataSourceType: "HISTORICAL"`, `provenance`, quality fields all present
    - _Requirements: 23.3_

  - [x] 14.5 Produce ALPHAFORGE_DATA_REQUIREMENTS.md
    - Write `.kiro/specs/data-service-platform/ALPHAFORGE_DATA_REQUIREMENTS.md`
    - Document all 14 AlphaForge consumer features from the requirements matrix table (India Scalping, India Daily Picks, India Expiry Trades, etc.)
    - For each of the five migration files in AlphaForge, document: current provider, data type accessed, and replacement Platform endpoint: `src/services/india/angelone/derivatives.ts`, `src/features/india/scanner/engine.ts`, `src/features/india/daily-picks/builder.ts`, `src/features/india/expiry-trades/builder.ts`, `src/features/ai-signals/india-builder.ts`
    - This document is produced by code review of the requirements spec only; no runtime dependency on the AlphaForge repository
    - _Requirements: 21.1, 21.2_

- [x] 15. Property-Based Tests (Hypothesis)
  - [x] 15.1 Write property test for OHLCV candle invariants (Property 1)
    - Test file: `tests/properties/test_ohlcv_invariants.py`
    - Generate random valid OHLCV candles using `st.floats(min_value=1.0)` for prices with valid O/H/L/C relationships; run through the validation pipeline; verify `high >= max(open, close)`, `low <= min(open, close)`, `volume >= 0`, all prices `> 0` always hold after processing
    - Minimum 100 iterations (`@settings(max_examples=100)`)
    - Tag: `# Feature: data-service-platform, Property 1: OHLCV Candle Invariants`
    - **Validates: Requirements 4.3, 13.8**
    - _Requirements: 4.3, 13.8_

  - [ ]* 15.2 Write property test for Normaliser round-trip (Property 2)
    - Test file: `tests/properties/test_normaliser_round_trip.py`
    - Generate random provider response JSON structures matching provider schemas; verify `parse(serialise(parse(raw))) == parse(raw)` for all valid inputs
    - Tag: `# Feature: data-service-platform, Property 2: Normaliser Round-Trip`
    - **Validates: Requirements 4.11, 17.8**

  - [ ]* 15.3 Write property test for Deribit instrument name round-trip (Property 3)
    - Test file: `tests/properties/test_deribit_round_trip.py`
    - Generate valid Deribit instrument names using `st.sampled_from(["BTC","ETH","SOL"])`, random valid dates/strikes/C|P; verify parse→serialise produces same `currency`, `strike`, `optionType`, expiry `{DD}{MON}{YY}`
    - Tag: `# Feature: data-service-platform, Property 3: Deribit Instrument Name Round-Trip`
    - **Validates: Requirements 14.7**

  - [ ]* 15.4 Write property test for deduplication hash stability (Property 4)
    - Test file: `tests/properties/test_dedup_hash.py`
    - Generate random `(instrumentId, eventTimeMs, source, ltp, volume)` tuples using `st.text()`, `st.integers()`, `st.floats()`; verify same inputs always produce the same SHA-256 hash
    - Tag: `# Feature: data-service-platform, Property 4: Deduplication Hash Stability`
    - **Validates: Requirements 3.6, 17.4**

  - [ ]* 15.5 Write property test for DataConfidenceScore bounds (Property 5)
    - Test file: `tests/properties/test_confidence_score_bounds.py`
    - Generate all combinations of gate input parameters; verify score always in `[0, 95]` — never negative, never 100
    - Tag: `# Feature: data-service-platform, Property 5: DataConfidenceScore Bounds`
    - **Validates: Requirements 7.1**

  - [ ]* 15.6 Write property test for NSE session phase determinism (Property 6)
    - Test file: `tests/properties/test_session_phase_determinism.py`
    - Generate random IST datetimes including boundary instants using `st.datetimes()` anchored to IST; verify exactly one `SessionPhase` is returned — no instant maps to two phases, no instant maps to zero phases
    - Tag: `# Feature: data-service-platform, Property 6: NSE Session Phase Determinism`
    - **Validates: Requirements 12.2**

  - [ ]* 15.7 Write property test for 3m interval rejection — Indian market (Property 7)
    - Test file: `tests/properties/test_3m_rejection.py`
    - Generate Indian market requests with `interval="3m"` at each pipeline layer (acquisition planner, normaliser, persistence, API handler); verify each layer rejects with HTTP 400 `INTERVAL_NOT_SUPPORTED` or raises `ValueError`
    - Tag: `# Feature: data-service-platform, Property 7: 3m Interval Rejection (Indian Market)`
    - **Validates: Requirements 1.5, 4.2, 10.11, 16.10**

  - [ ]* 15.8 Write property test for DataQualityGate closed-form correctness (Property 8)
    - Test file: `tests/properties/test_quality_gate_closed_form.py`
    - Generate all 32 combinations of 5 boolean gate conditions using `st.booleans()` × 5; verify `signalEngineAllowed` is `true` if and only if all five are `true`
    - Tag: `# Feature: data-service-platform, Property 8: DataQualityGate Closed-Form`
    - **Validates: Requirements 7.2**

  - [ ]* 15.9 Write property test for BLOCKED score blocking signal engine (Property 9)
    - Test file: `tests/properties/test_blocked_score.py`
    - Generate score inputs constrained to produce `DataConfidenceScore < 30`; verify `signalEngineAllowed` is always `false` with no exceptions
    - Tag: `# Feature: data-service-platform, Property 9: BLOCKED Score Blocks Signal Engine`
    - **Validates: Requirements 7.11**

  - [ ]* 15.10 Write property test for cache TTL monotonicity (Property 10)
    - Test file: `tests/properties/test_cache_ttl_monotonicity.py`
    - Simulate cache entry lifecycle with time sequences and TTL configurations; verify TTL is monotonically non-increasing between reads; verify stale-while-revalidate refresh sets new TTL to exactly the configured TTL for the data type
    - Tag: `# Feature: data-service-platform, Property 10: Cache TTL Monotonicity`
    - **Validates: Requirements 9.2, 9.6**

  - [ ]* 15.11 Write property test for provenance observation ID uniqueness (Property 11)
    - Test file: `tests/properties/test_observation_id_uniqueness.py`
    - Generate 1000 observations in a single test run; verify all `dataObservationId` (UUID v4) values are distinct
    - Tag: `# Feature: data-service-platform, Property 11: Provenance Observation ID Uniqueness`
    - **Validates: Requirements 8.1**

  - [ ]* 15.12 Write property test for OI semantic integrity (Property 12)
    - Test file: `tests/properties/test_oi_semantic_integrity.py`
    - Generate provider responses with and without `oi` data; verify `oi` is never set to the value of `tradedValue`; verify absent `oi` → `null` + `oiMissing: true`
    - Tag: `# Feature: data-service-platform, Property 12: OI Semantic Integrity`
    - **Validates: Requirements 3.3, 6.2**

  - [ ]* 15.13 Write property test for look-ahead bias prevention (Property 13)
    - Test file: `tests/properties/test_look_ahead_bias.py`
    - Generate arbitrary backtest request times `T` and sets of records with various `availableAtMs` values using `st.integers()`; verify all returned records satisfy `availableAtMs ≤ T`
    - Tag: `# Feature: data-service-platform, Property 13: Look-Ahead Bias Prevention`
    - **Validates: Requirements 23.4**

  - [ ]* 15.14 Write property test for reconciliation deviation classification (Property 14)
    - Test file: `tests/properties/test_reconciliation_deviation.py`
    - Generate pairs of positive OHLCV values `(A, B)` using `st.floats(min_value=0.01)`; verify: deviation ≤ 0.5% → `CONFIRMED`, between 0.5% and 2.0% → `MINOR_DISCREPANCY`, >2.0% → `MAJOR_DISCREPANCY`; verify classifications are mutually exclusive and exhaustive
    - Tag: `# Feature: data-service-platform, Property 14: Reconciliation Deviation Classification`
    - **Validates: Requirements 10.5, 10.6, 10.7**

- [x] 16. Integration and End-to-End Tests
  - [x] 16.1 Create provider mock fixtures and test infrastructure
    - Write `tests/fixtures/provider_mocks.py` with fixtures for all response scenarios per provider: normal response, empty dataset, partial fields (some null), invalid schema, timeout, HTTP 429 with `Retry-After`, HTTP 429 without `Retry-After`, malformed JSON, schema breaking change
    - Configure pytest-asyncio and Docker-based Redis + PostgreSQL for integration tests
    - _Requirements: 5.4, 5.5, 17.1_

  - [x] 16.2 Write circuit breaker integration tests
    - Test file: `tests/integration/test_circuit_breaker.py`
    - Test all state transitions (CLOSED→OPEN, OPEN→HALF_OPEN, HALF_OPEN→CLOSED, HALF_OPEN→OPEN) under simulated provider failure sequences
    - Verify HTTP 429 does not increment failure counter; verify `MARKET_CLOSED` does not increment counter
    - Verify Redis state persists across simulated process restarts
    - _Requirements: 5.4, 5.5, 5.7, 5.8_

  - [ ]* 16.3 Write cache miss → provider call → cache hit cycle integration tests
    - Test file: `tests/integration/test_cache_cycle.py`
    - Test L1 miss → L2 miss → L3 miss → provider call → L2 write → L1 write → L1 hit on second request
    - Test request coalescing: 5 concurrent requests for same key during in-flight provider call → single provider call
    - Test stale-while-revalidate triggering at 80% TTL consumed
    - _Requirements: 9.1, 9.4, 9.5, 9.6, 9.9_

  - [ ]* 16.4 Write backfill checkpoint resume integration test
    - Test file: `tests/integration/test_backfill_resume.py`
    - Simulate backfill job interrupted after chunk 3 of 10; verify on restart it resumes from chunk 3 checkpoint rather than chunk 0
    - _Requirements: 10.1, 10.2_

  - [ ]* 16.5 Write WebSocket fanout integration tests
    - Test file: `tests/integration/test_websocket_fanout.py`
    - Test 10 concurrent consumer WebSocket connections subscribing to the same symbol; verify all receive the same tick within 500ms
    - Test heartbeat: simulate no response for 3 consecutive heartbeats; verify connection is closed and `subscribedSymbols` decrements
    - Test connection close: verify unsubscribe within 5 seconds
    - _Requirements: 15.5, 15.6, 15.8_

  - [ ]* 16.6 Write SIGTERM graceful shutdown integration test
    - Test file: `tests/integration/test_graceful_shutdown.py`
    - Send SIGTERM while in-flight requests are active; verify in-flight requests complete (up to 30s drain window); verify no new connections accepted after SIGTERM; verify provider WebSocket connections closed
    - _Requirements: 20.5_

  - [ ]* 16.7 Write AlphaForge consumer feature contract tests
    - Test file: `tests/integration/test_consumer_contracts.py`
    - For each of the 14 AlphaForge consumer features in the requirements matrix, write a contract test verifying the Platform endpoint returns the expected payload shape and includes `provenance`, `quality`, and `dataSourceType` in the metadata envelope
    - _Requirements: 21.1_

- [x] 17. Performance and Production Certification
  - [x] 17.1 Write load tests for core API endpoints
    - Test file: `tests/performance/test_load.py`
    - Concurrent request targets: 100, 500, 1000 concurrent requests for each endpoint group: live quote, historical OHLCV, instrument search, options chain, WebSocket connections
    - Measure and assert P50, P95, P99 latency targets from the design
    - _Requirements: 18.2, 18.4_

  - [ ]* 17.2 Write instrument search benchmarks
    - Test file: `tests/performance/test_instrument_search.py`
    - Benchmark search at 3 dataset sizes: 10K, 100K, 500K instruments; measure P50/P95/P99 for `GET /v1/instruments` with representative filter combinations
    - _Requirements: 2.5_

  - [ ]* 17.3 Write historical ingestion benchmarks
    - Test file: `tests/performance/test_ingestion_benchmark.py`
    - Benchmark bulk upsert throughput at 10, 50, 100, and 228 (full F&O universe) symbols simultaneously
    - Measure candles-per-second throughput and peak memory usage
    - _Requirements: 22.5_

  - [x] 17.4 Produce certification documents
    - Write `PERFORMANCE_CERTIFICATION.md` summarising load test results with pass/fail against latency targets
    - Write `DATA_RELIABILITY_CERTIFICATION.md` documenting gap rate, duplicate rate, reconciliation match rate, quality score distributions
    - Write `SECURITY_CERTIFICATION.md` documenting credential stripping verification, auth enforcement, CORS configuration, injection prevention
    - Write `PRODUCTION_READINESS.md` listing all environment variables, runbook for SIGTERM/startup/degraded-mode, circuit-breaker tuning guide
    - _Requirements: 1.4, 19.1, 20.7_

---

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster initial delivery; all non-`*` tasks are required for the production baseline
- The `3m` interval ban for Indian markets is enforced at six independent layers: acquisition planner (`ValueError`), normaliser (reject), persistence (`CHECK` constraint on `candle_bar`), API handler (HTTP 400), historical engine routing, and gap recovery. Any single layer catching it is defence-in-depth; all six must be present
- OI, IV, Greeks, bid/ask null-semantics are the highest-priority correctness invariants; they are tested by both unit tests (Phase 5/6) and property tests (Property 12)
- The `DataConfidenceScore` ceiling of 95 is intentional and must never be relaxed — it reflects inherent market-data uncertainty
- The `ALPHAFORGE_DATA_REQUIREMENTS.md` (Task 14.5) is produced from the requirements specification; the AlphaForge repository at `/Users/manishkumar/Desktop/alpha-forge` must NOT be accessed at runtime
- All Pydantic models use v2 (`BaseModel` with strict field types); no v1 compatibility shims
- All async I/O uses `asyncio` with `httpx` or `aiohttp`; no synchronous blocking calls in async paths
- Polars or PyArrow (not pandas) for any in-process bulk OHLCV transformation
- Redis Lua scripts for atomic rate-limiter and circuit-breaker state operations across replicas
- Property tests require Hypothesis ≥ 6.x; each property runs a minimum of 100 examples

---

## Task Dependency Graph

```json
{
  "waves": [
    {
      "id": 0,
      "tasks": ["1.1", "1.2"]
    },
    {
      "id": 1,
      "tasks": ["1.3", "1.4", "1.5", "1.6"]
    },
    {
      "id": 2,
      "tasks": ["2.1"]
    },
    {
      "id": 3,
      "tasks": ["2.2", "2.4", "2.5"]
    },
    {
      "id": 4,
      "tasks": ["2.3", "2.6"]
    },
    {
      "id": 5,
      "tasks": ["3.1", "4.1"]
    },
    {
      "id": 6,
      "tasks": ["3.2", "4.2", "4.3", "4.4"]
    },
    {
      "id": 7,
      "tasks": ["3.3", "4.5", "4.6", "4.7", "4.8"]
    },
    {
      "id": 8,
      "tasks": ["3.4", "4.9", "5.1"]
    },
    {
      "id": 9,
      "tasks": ["5.2", "5.3", "5.4", "5.5", "5.6", "5.7"]
    },
    {
      "id": 10,
      "tasks": ["5.8", "5.9", "5.10"]
    },
    {
      "id": 11,
      "tasks": ["6.1", "6.2"]
    },
    {
      "id": 12,
      "tasks": ["6.3", "6.4", "8.1"]
    },
    {
      "id": 13,
      "tasks": ["6.5", "7.1", "8.2", "8.3", "8.4"]
    },
    {
      "id": 14,
      "tasks": ["7.2", "7.3", "8.5", "9.1"]
    },
    {
      "id": 15,
      "tasks": ["7.4", "8.6", "8.7", "9.2", "9.3", "9.4"]
    },
    {
      "id": 16,
      "tasks": ["9.5", "9.6", "9.7", "9.8", "9.9", "10.1"]
    },
    {
      "id": 17,
      "tasks": ["10.2", "10.3"]
    },
    {
      "id": 18,
      "tasks": ["10.4", "11.1"]
    },
    {
      "id": 19,
      "tasks": ["11.2", "12.1"]
    },
    {
      "id": 20,
      "tasks": ["11.3", "12.2"]
    },
    {
      "id": 21,
      "tasks": ["12.3", "13.1", "13.2", "13.3", "13.4"]
    },
    {
      "id": 22,
      "tasks": ["13.5", "13.6", "13.7"]
    },
    {
      "id": 23,
      "tasks": ["14.1", "14.2", "14.3", "14.4", "14.5"]
    },
    {
      "id": 24,
      "tasks": ["15.1", "16.1"]
    },
    {
      "id": 25,
      "tasks": ["15.2", "15.3", "15.4", "15.5", "15.6", "15.7", "15.8", "15.9", "15.10", "15.11", "15.12", "15.13", "15.14"]
    },
    {
      "id": 26,
      "tasks": ["16.2", "16.3", "16.4", "16.5", "16.6", "16.7"]
    },
    {
      "id": 27,
      "tasks": ["17.1"]
    },
    {
      "id": 28,
      "tasks": ["17.2", "17.3"]
    },
    {
      "id": 29,
      "tasks": ["17.4"]
    }
  ]
}
```
