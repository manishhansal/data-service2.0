# CHANGELOG — DATA-SERVICE 2.0

All notable changes to DATA-SERVICE 2.0 are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

> **Current status:** CONDITIONALLY_READY for production  
> See [`reports/20_FINAL_PRODUCTION_CERTIFICATION.md`](reports/20_FINAL_PRODUCTION_CERTIFICATION.md) for the authoritative production-readiness assessment.

---

## [2.0.0] — 2026-09-15 (post-audit hardened)

### Fixed (operational — this session)

- **Dockerfile CMD**: Replaced `--log-config /dev/null` with `--no-access-log`. Uvicorn 0.52+ rejects an empty file as a logging config; the new flag achieves the same result (no per-request access log) without crashing on startup. (`Dockerfile`)
- **Dockerfile build**: Added `README.md` to the `COPY pyproject.toml README.md ./` instruction. Hatchling validates the `readme` field in `pyproject.toml` during wheel metadata generation and raised `OSError: Readme file does not exist: README.md` without it. (`Dockerfile`)
- **docker-compose.yml**: Added missing `CONSUMER_API_KEYS`, `JWT_SECRET`, `JWT_EXPIRY_SECONDS`, and `ANGEL_ONE_MPIN` to the `environment` block for all three application services (`api`, `worker`, `scheduler`). Without these, `CONSUMER_API_KEYS` was empty inside the container and every request produced `api_key_rejected` warnings.

### Added (documentation — this session)

- `docs/ARCHITECTURE.md` — new comprehensive architecture reference covering all 17 sections: system overview, repository layout, Docker Compose services, full request lifecycle, all six architecture layers, scheduler/worker, observability stack, provider adapter table, data flow diagrams, configuration reference, and design invariants.
- `CHANGELOG.md` — this file.
- `README.md` — fully rewritten with a complete operational guide: Docker Compose workflow, start/stop commands, log following, health verification, authentication examples, full API endpoint table, architecture overview, market data coverage, null semantics, database migration commands, test runner commands, deployment checklist, and troubleshooting section.
- `.env.example` — rewritten with detailed inline documentation for all 50+ variables, including provider-specific notes (Delta Exchange symbol conventions, Upstox token rotation, Angel One MPIN/TOTP), security guidance, and links to architecture docs.

---

## [2.0.0-rc2] — 2026-09-14 (audit completion)

### Added

- **API Reference** (`docs/API_REFERENCE.md`): Complete reference for all 61 endpoints across 14 functional groups — health, auth, compatibility routes, Indian markets (live + historical + broker analytics), instruments, Binance crypto, Delta Exchange, Deribit options, quality engine, provider health, WebSocket streaming, provenance/lineage, and replay engine. Includes all query parameters, request bodies, response shapes, and error codes. (`1027ad1`)
- **Indian market migration docs** (`docs/ALPHAFORGE_INDIAN_DATA_CONSUMER_CONTRACT.md`, `docs/ALPHAFORGE_INDIAN_DATA_DEPENDENCY_MAP.md`, `docs/INDIAN_DATA_SERVICE_API_GAP_MATRIX.md`): Full AlphaForge Indian data integration analysis, dependency mapping, and gap matrix. (`c919f31`)
- **Yahoo Finance adapter** (`src/providers/adapters/yahoo_finance.py`): NSE index symbol mapping (`.NS` suffix), used as a fallback open-source provider for Indian index data. (`94b2295`)
- **AlphaForge compat routes** (`src/api/compat.py`): `/scraping/*` and `/data/gate` endpoints translating AlphaForge's `ScraplingProvider` calls into data-service backend logic without requiring API key authentication (internal network only). (`b11fa46`)
- **Audit reports**: 17 new certification and RCA reports in `reports/` covering provider runtime, Indian live data, historical data, database audit, AlphaForge integration, security, performance, provenance, WebSocket, failover, and a final production certification. (`2a59aba`, `34a315f`)

### Fixed

- **Jugaad-data adapter** (`src/providers/adapters/jugaad_data.py`): Full rewrite to fix broken response parsing (DS2-RCA-028). (`4ca00a2`)
- **OpenChart adapter** (`src/providers/adapters/openchart.py`): Full rewrite to fix broken response parsing (DS2-RCA-029). (`4ca00a2`)
- **Upstox interval map** (`src/providers/adapters/upstox.py`): Corrected API interval strings — Upstox V2 uses different naming conventions than the canonical platform intervals (DS2-RCA-027). (`8f55f43`)
- **Angel One 1m gap documented**: The 1m historical data limitation for some instruments outside market hours is now documented in the adapter source and acknowledged in reports. (`8fcb9b3`)

---

## [2.0.0-rc1] — 2026-09-13 (post-forensic-audit fixes)

### Fixed (P0 — critical)

- **Consumer auth not enforced** (DS2-RCA-016): `ConsumerAuthDependency` was not applied to any API route. All `/v1/*` data routes now require `X-API-KEY` or `Authorization: Bearer` via a router-level FastAPI dependency in `server.py`. (`77ba837`)
- **Delta Exchange not implemented** (DS2-RCA-001): AlphaForge's default crypto broker had no adapter. Delta Exchange India REST adapter built (`src/providers/adapters/delta_exchange.py`), with normaliser, persistence layer, capability matrix entry, and API endpoints. (`176f1c0`, `b4c7f32`)
- **Broker analytics endpoints missing** (DS2-RCA-018): Angel One PCR, OI buildup, and gainers/losers endpoints were documented but not implemented. Created `src/api/broker_analytics.py`. (`ed48078`)
- **Angel One MPIN auth**: Added `ANGEL_ONE_MPIN` credential support to the Angel One adapter for the TOTP + JWT authentication flow. Updated settings, docker-compose, and `.env.example`. (`1d660bd`)
- **Upstox wiring**: Connected the Upstox adapter to the market engine and historical engine; access token now initialised from `UPSTOX_ACCESS_TOKEN` env var at startup without requiring an OAuth round-trip. (`1d660bd`)

### Fixed (P1 — high)

- **13 of 14 property tests missing**: Only Property 1 existed. Implemented Properties 2–14 (Hypothesis) covering OHLCV invariants, null semantics, dedup hash stability, gap state machine transitions, cache hierarchy, circuit breaker, quality score bounds, backtest PIT filter, provenance immutability, and canonical schema round-trips. (`c775780`)
- **3 settings env-leakage test failures**: `test_settings.py` was reading real env vars from the test runner's environment. Fixed by patching `os.environ` cleanly in each test. (`c775780`)
- **PRODUCTION_CERTIFICATION.md revoked**: Original certification (dated 2026-01-15) predated the actual code by 8 months and was based solely on unit tests without any real provider connections, real DB rows, or real API responses. Status updated to REVOKED; authoritative certification moved to `reports/20_FINAL_PRODUCTION_CERTIFICATION.md`. (`8fcb9b3`)

### Added

- **Angel One MPIN authentication** (`src/providers/adapters/angel_one.py`): Full TOTP + JWT flow using `pyotp` for time-based OTP generation and the 4-digit MPIN for SmartAPI login. Automatic token rotation scheduled at 23:55 IST via the scheduler. (`1d660bd`)
- **Broker analytics API** (`src/api/broker_analytics.py`): `GET /v1/broker-analytics/pcr`, `/oi-buildup`, `/gainers-losers` sourced from Angel One SmartAPI. (`ed48078`)
- **Delta Exchange India REST adapter** (`src/providers/adapters/delta_exchange.py`): Full adapter for Delta India API — OHLCV (ascending sort, ms conversion), tickers, product metadata, OI candles. (`176f1c0`)
- **Delta normaliser + persistence** (`src/providers/delta_normaliser.py`, `delta_persistence.py`): Canonical field mapping and DB write path for Delta candle data. (`176f1c0`)
- **Delta API endpoints** (`src/api/crypto.py`): `GET /v1/delta/products`, `/v1/delta/{symbol}/ticker`, `/v1/delta/{symbol}/ohlcv`. (`b4c7f32`)
- **Replay session endpoints** (`src/api/replay.py`): Create, query, and terminate replay sessions for historical data backtesting. (`8319498`)
- **DataParityContract** (`src/core/schemas/parity.py`, `src/api/contract.py`): Snapshot of current data coverage — providers, intervals, instruments, and quality thresholds — queryable via `GET /v1/contract`. (`2c08712`)
- **Backtest point-in-time filter** (`src/backtest/point_in_time.py`): Prevents look-ahead bias using `availableAtMs` provenance field; `BacktestContext` FastAPI dependency injects the `as_of` timestamp. (`2c08712`)
- **Analytics platform** (`src/api/analytics.py`): Provider health dashboard, platform-level analytics. (`2c08712`)

### Tests added (2026-09-13)

- Unit tests for all API endpoints (Indian market, crypto, quality, security, lineage) — `tests/unit/api/`
- Unit tests for all provider adapters (circuit breaker, gateway, Binance, Deribit) — `tests/unit/providers/`
- Unit tests for all engines (session, calendar, instruments, quality, streaming) — `tests/unit/engines/`
- Unit tests for core modules (settings, shell safety, pipeline, cache, DB, observability) — `tests/unit/core/`
- Unit tests for auth, middleware, monitors, backtest, replay, stores, forensics — `tests/unit/`
- Integration tests for circuit breaker state machine — `tests/integration/`
- Hypothesis property tests (43 total, Properties 1–14) — `tests/property/`
- Performance benchmarks (mocked I/O, CI-safe) + Locust load testing scenarios — `tests/performance/`
- Shared mock fixtures and factory functions — `tests/mocks/`, `tests/fixtures/`

**Total: 4485 tests passing, 0 failing** at the time of the forensic audit.

---

## [2.0.0-alpha] — 2026-09-13 (initial feature build)

### Added (core platform)

- **Project scaffolding** (`d5183c3`): Python 3.11, FastAPI 0.141, Pydantic v2, Uvicorn, Redis, PostgreSQL/TimescaleDB, Alembic, structlog, OpenTelemetry, Prometheus. Multi-stage Dockerfile, Docker Compose with 5 services, pyproject.toml with pinned dependencies, `.gitignore`, and `.env.example`.
- **Settings + observability** (`7796d05`): `src/core/settings.py` — Pydantic `BaseSettings` with `APP_ENV`-based env-file resolution. `src/observability/logging.py` (structlog JSON), `src/observability/tracing.py` (OpenTelemetry). Health endpoints: `GET /v1/health/live`, `/ready`, `/data`.
- **Database layer** (`dc39c9c`): Async SQLAlchemy engine factory, Alembic migration environment, initial schema migration covering `candle_bar`, `data_gap`, `data_provenance`, `instrument`, `fno_universe` tables. TimescaleDB hypertable support via `scripts/promote_timescaledb.sql`.
- **Three-level cache** (`f1f9fd9`): L1 in-process LRU (`src/cache/l1_cache.py`), L2 Redis client pool (`src/cache/redis_client.py`), `CacheManager` with request coalescing and stale-while-revalidate (`src/cache/cache_manager.py`), Redis Streams Event Bus (`src/cache/event_bus.py`).
- **Canonical schemas** (`1fdc732`): Pydantic v2 models for `OHLCVCandle`, `Quote`, `OptionRow`, `SessionPhase`, `ProviderCapability`, `DataProvenance`, `ProvenanceFactory`, `DataParityContract`.
- **14-step validation pipeline** (`f01620d`): `src/core/pipeline.py` orchestrator, `src/core/normaliser.py` with strict null semantics, per-step validators in `src/core/validators/` (schema, timestamp, semantic, dedup, gap detection, freshness, reconciliation, quality).
- **Provider gateway** (`6c72145`): `src/providers/capability_matrix.py` — single source of truth for provider × capability routing. Redis-backed circuit breaker state machine (`src/providers/circuit_breaker.py`). Token-bucket rate limiter (`src/providers/rate_limiter.py`).
- **Indian provider adapters** (`8c55d83`): Angel One SmartAPI, Upstox V2/V3, NSE Scrapling (curl-cffi WAF bypass), Jugaad-data, OpenChart.
- **Crypto provider clients** (`7615d14`): Binance REST (spot + futures), Deribit REST, Delta Exchange (initial).
- **WebSocket stream adapters** (`7ccc072`): Angel One SmartStream, Upstox V3 WebSocket (Protobuf), Binance WebSocket streams.
- **NSE engines** (`7e32e84`): `MarketSessionEngine` (PRE_OPEN / REGULAR / CLOSED state machine), `HolidayCalendar` (NSE holiday + half-day database), `InstrumentMaster` (symbol normalisation), `FnoUniverse` (F&O eligible universe).
- **Indian market API** (`d74f40f`): `GET /v1/india/quotes/{symbol}`, `/v1/india/option-chain`, `/v1/india/market/status`, `/v1/india/historical`, `/v1/india/historical/gaps`.
- **Historical engine + gap recovery** (`68af61e`): Chunked OHLCV backfill, cross-provider reconciliation, gap PENDING→RECOVERING→RECOVERED/EXHAUSTED state machine.
- **Streaming engine** (`a8ac75c`): SHA-256 tick deduplication (24h rolling window), Event Bus fan-out, `DatasetReady` events, `WS /v1/stream/ticks` endpoint.
- **Quality engine** (`c4543af`): `DataConfidenceScore` formula (5 components, [0, 95] cap), `DataQualityGate` (5 conditions), freshness classification (FRESH/AGING/STALE/EXPIRED), quality alerter, NTP clock-skew monitor.
- **Crypto API** (`2a95be1`): Binance OHLCV, ticker, 24hr stats, exchange info, futures overview (mark price, funding, OI, L/S ratio). Deribit options analytics (IV, OI, max pain, PCR, index price).
- **Lineage + forensics** (`d1ac05d`): `ProvenanceRecord` persistence (`src/stores/lineage_store.py`), `TradeForensicsReport` (`src/forensics/trade_forensics.py`), `GET /v1/provenance/{id}`, `GET /v1/lineage/trade/{id}`.
- **Security layer** (`ffdfd01`): `ConsumerAuthDependency` (API key + JWT bearer), `CredentialStripperMiddleware`, CORS allowlist (no wildcard), canonical error envelope (`ErrorCode` enum), shell-injection guards (`src/core/shell_safety.py`).
- **Scheduler** (`src/scheduler.py`): APScheduler with IST timezone — F&O universe refresh at 08:45 IST, Angel One JWT rotation at 23:55 IST, NTP clock-skew sampling every ≤30 seconds.
- **Worker** (`src/worker.py`): Long-running background process for backfill orchestration, gap recovery, reconciliation, provenance retries, and DataIncident archival.

---

## Notes

### Known limitations at time of writing (2026-09-15)

| Issue | Status |
|---|---|
| Angel One 1m historical data unavailable outside market hours for some instruments | Documented; use Upstox as primary 1m provider |
| Upstox OAuth access token requires manual refresh (no automated browser flow) | By design; update `UPSTOX_ACCESS_TOKEN` in env |
| Delta Exchange India does not expose a public global L/S ratio endpoint | Returns empty list; documented in adapter |
| `PRODUCTION_CERTIFICATION.md` in repo root is REVOKED | Use `reports/20_FINAL_PRODUCTION_CERTIFICATION.md` |
| `/docs`, `/redoc`, `/openapi.json` disabled in `ENVIRONMENT=production` | By design (security) |

### RCA index

All known bugs and their fixes are documented in [`reports/18_RCA_AND_FIXES.md`](reports/18_RCA_AND_FIXES.md).

| RCA ID | Issue | Fix commit |
|---|---|---|
| DS2-RCA-001 | Delta Exchange adapter not implemented | `176f1c0`, `b4c7f32` |
| DS2-RCA-016 | Consumer auth not enforced on any route | `77ba837` |
| DS2-RCA-018 | Broker analytics endpoints missing | `ed48078` |
| DS2-RCA-027 | Upstox interval map incorrect API strings | `8f55f43` |
| DS2-RCA-028 | Jugaad-data adapter broken | `4ca00a2` |
| DS2-RCA-029 | OpenChart adapter broken | `4ca00a2` |
| —          | `--log-config /dev/null` crashes Uvicorn 0.52+ | `Dockerfile` (post-audit) |
| —          | `README.md` missing from Docker build context | `Dockerfile` (post-audit) |
| —          | `CONSUMER_API_KEYS` not injected into container | `docker-compose.yml` (post-audit) |
