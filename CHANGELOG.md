# CHANGELOG — DATA-SERVICE 2.0

All notable changes to DATA-SERVICE 2.0 are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

> **Current status:** CONDITIONALLY_READY for production  
> See [`PRODUCTION_CERTIFICATION_STATUS.md`](PRODUCTION_CERTIFICATION_STATUS.md) for the authoritative runtime certification.

---

## [2.2.0] — 2026-09-22 (5-year backfill campaign + continuous OHLCV catch-up worker)

### Added (data pipeline)

- **`fo_universe` DB table** — ORM model `src/db/models/fo_universe.py`, migration `20260920_000000_add_fo_universe_table.py`. Master F&O instrument registry with columns: `instrument_id`, `symbol`, `company_name`, `isin`, `exchange`, `instrument_class`, `sector`, `is_index`, `backfill_priority`, `fo_listed_date`, `fo_delisted_date`, `is_fo_active`, `spot_listed_date`, `spot_delisted_date`. Seeded with 314 rows (293 active, 21 retired) via `scripts/seed_fo_universe.py`.
- **`continuous_futures` DB table** — migration `20260919_000000_add_continuous_futures_table.py`. Stores front-month-rolled continuous series; seeded with 131,265 rows (305 underlyings, Sep 2021 → Sep 2026) via `scripts/load_continuous_futures.py`.
- **5-year backfill script** — `scripts/backfill_india_5y.py`: reads instrument universe from `fo_universe` DB table with FO→IDX→EQ priority order, falls back to static `india_instruments.py`. Supports `ALL_SPOT` / `FO_UNIVERSE` / `EQ_IDX` class filters. Gradual rate-limiting via chunk-delay + instrument-delay. Completed: 123M+ equity candle rows.
- **NSE F&O bhavcopy loader** — `scripts/load_fo_bhavcopy_5y.py`: daily loader for `futures_candle` + `options_candle` (1d). Dual-path: pre-2024 via `pybhav` library, 2024+ via direct `BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip` URL. Loaded 246,986 futures rows and 242,255 options rows (Sep 2021 → live).
- **F&O options chain snapshot loader** — `scripts/load_options_chain_snapshots.py`: loads per-strike CE/PE snapshots.
- **Seed F&O universe script** — `scripts/seed_fo_universe.py`: 647-line seeder that populates `fo_universe` with company names, ISINs, sectors, listing dates, and backfill priority.
- **Continuous OHLCV catch-up worker** — `src/worker_tasks/ohlcv_catchup.py` (new 484-line module). Background task that automatically fetches all missing candles from the last Redis checkpoint to today, for every instrument × interval pair. Two pass types: EOD pass (`1d`, `1w`, `1M`) runs once daily at 17:00 IST; intraday pass runs every 4 hours. Fires immediately on startup to close any gap accumulated since the last run. Stateless scheduler job pattern — sessions created and disposed per cycle.
- **301 Upstox `NSE_EQ|ISIN` keys** — `_UPSTOX_INSTRUMENT_KEYS` map in `historical_engine.py` expanded from 51 → 301 symbols. Added all 229 NSE F&O universe stocks with verified ISINs plus all NSE index display name aliases.
- **Angel One → Upstox automatic fallback** — `_fetch_candles()` in `historical_engine.py`: when `angel_one_token_unknown` fires and the symbol has a registered Upstox ISIN key, the engine silently re-routes to the Upstox path. Logs `angel_one_token_unknown_upstox_fallback` info event.
- **Full NSE F&O universe instrument list** — `scripts/india_instruments.py` expanded: `FO_UNIVERSE_EQUITIES` (231 EQ + 2 IDX), `NFO_FUTURES` (full index + stock universe), `ALL_SPOT` (all spot backfill targets), `ALL_INSTRUMENTS` (union of all). `ALL_EQ_IDX` alias retained for backward compatibility with 1-year backfill script.
- **Upstox `NSE_FO|{token}` keys** — `scripts/load_fno_instrument_master.py` updated: `_build_instrument_record()` derives `upstox_key` as `NSE_FO|{angel_token}`. UPSERT uses `COALESCE(EXCLUDED, existing)` to preserve existing keys on conflict. Seeded 35,940 Angel One + 35,940 Upstox mappings (70,629 total provider mapping rows).
- **Makefile targets** — extensive new targets: `load-fno`, `load-fno-dry`, `backfill-5y`, `seed-fo-universe`, `catchup-status`, `worker-logs`, `data-report`, and all existing infra targets. Auto-detection of env file: `.env.local` → `.env.production` → `.env`.

### Fixed (worker + OHLCV catch-up)

- **NSE_INDEX intraday blocked (UDAPI100011)** — Upstox does not serve `1m–1h` candles for `NSE_INDEX` instruments. `_resolve_provider()` in `historical_engine.py` now routes IDX instruments to Upstox only for EOD intervals (`1d`/`1w`/`1M`); intraday routes to Angel One instead. `ohlcv_catchup._run_pass()` skips intraday intervals for `instrument_class=IDX` entirely, preventing wasted 400 retry cycles.
- **MIDCPNIFTY not available on Upstox** — Nifty Midcap Select index returns `UDAPI100011` for all intervals including `1d`. Comment added in `_UPSTOX_INSTRUMENT_KEYS` flagging the limitation. Intraday traffic routes to Angel One.
- **Upstox 90-day single-call window exceeded** — when the Angel One fallback fires, Upstox received the full 90-day window as a single call, exceeding its ~30-day limit for minute-level data. Now chunked correctly in `historical_engine.py`.

### Changed

- **`src/worker.py`** — idle stub replaced by real OHLCV catch-up worker with 5 services: `OHLCVCatchUpWorker` for intraday, `OHLCVEODWorker` for EOD, startup immediate pass, configurable intervals via env vars.
- **`src/scheduler.py`** — added `catchup_eod_job` (daily at 17:00 IST) and `catchup_intraday_job` (every `CATCHUP_INTRADAY_INTERVAL` hours, default 4h).
- **`docker-compose.yml`** — added `CATCHUP_*` env vars to `api` and `worker` service env blocks.
- **`.env.example`** — documents all `CATCHUP_*` configuration variables: `CATCHUP_EOD_INTERVAL_HOURS`, `CATCHUP_INTRADAY_INTERVAL`, `CATCHUP_MAX_INSTRUMENTS`, `CATCHUP_CHUNK_DELAY_S`, `CATCHUP_INSTRUMENT_DELAY_S`.
- **`Dockerfile`** — added `COPY scripts/` so all Makefile targets work inside containers.
- **`scripts/backfill_india_1y.py`** — imports `ALL_EQ_IDX` alias instead of `ALL_INSTRUMENTS` to keep 1-year scope to IDX+EQ only (no F&O bleed).

### DB state after 2.2.0

| Table | Rows | Change from 2.1.0 |
|---|---|---|
| `equity_candle` | ~123M+ | +117M (5y intraday backfill, 298 instruments) |
| `futures_candle` (bhavcopy 1d) | 246,986 | +246,986 (new, 5y NSE bhavcopy) |
| `futures_candle` (broker intraday) | ~55,000 | +55,000 (near-month only) |
| `options_candle` (bhavcopy 1d) | 242,255 | +242,255 (new, 5y NSE bhavcopy) |
| `continuous_futures` | 131,265 | +131,265 (new, 5y, 305 underlyings) |
| `fo_universe` | 314 | +314 (new master registry) |

### Tests

4,821+ unit tests passing (0 failures, 6 external warnings). ML_DATA_CERTIFICATION.md updated to v3.0 with full column schemas, sample data, and empirically confirmed availability dates from live DB queries.

---

## [2.1.0] — 2026-09-17 (data pipeline gap fixes + Upstox V3 migration)

### Fixed (critical data pipeline gaps)

- **market_quote persistence:** `persist_market_quote()` added to `market_engine.py` — fire-and-forget async upsert writes every live quote to `market_quote` with `depth_json` (JSONB), `source_type`, `change`, `change_pct`, `avg_traded_price`. DB migration adds these columns and the unique constraint needed for upsert.
- **option_greeks_snapshot persistence:** `_persist_option_greeks()` added to `api/india.py` — writes Greeks (iv, delta, gamma, theta, vega, oi, volume, ltp) to DB after every REST Greeks API call.
- **option_chain_snapshot + option_chain_contract persistence:** `_persist_option_chain()` added to `dual_provider_engine.py` — writes chain snapshots and per-strike CE/PE contract rows including Greeks and OI.
- **bulk_upsert_candles:** `source_timestamp` and `underlying_id` now written to all three candle tables. SQL uses `COALESCE(EXCLUDED, existing)` to preserve existing timestamps on conflict.
- **Historical API per-candle response:** `provider` and `sourceType` now included in every candle returned by `GET /v1/india/historical`. SQL updated to SELECT `source_type`.
- **Upstox normalizer gaps:** `normalize_full_quote` now captures `totalBuyQty`, `totalSellQty`, `weekHigh52`, `weekLow52`, `avgTradedPrice` from V2/V3 response.
- **MarketEngine token bug (BUG-001):** `_fetch_live_quote_stub` now resolves numeric Angel One token via `InstrumentMasterService.resolve_provider_tokens()`. Zero HTTP 400s confirmed post-fix.
- **PCR analytics schema (BUG-002):** `fetch_pcr()` now handles Angel One list response — returns `{"data": [list], "provider": ..., "fetchedAt": ...}`.

### Changed (Upstox V3 migration)

- **`fetch_full_quote` migrated to V3:** `GET /v3/market-quote/quotes` replaces deprecated `GET /v2/market-quote/quotes` (Upstox V3 launched April 2025; V3 adds CAS fields).
- **Upstox interval restriction lifted:** `_UPSTOX_V2_SUPPORTED_INTERVALS` (5 intervals: 1m/30m/1d/1w/1M) replaced with `_UPSTOX_V3_SUPPORTED_INTERVALS` (all 9 canonical intervals). V3 has no plan-based interval restrictions.
- **Historical engine routing:** IDX and EQ routing now recognises `upstox_analytics_key` as a valid credential (not just `upstox_access_token`).

### Added (new Upstox APIs)

- **Market Information APIs (launched May 2026):** `fetch_oi_data`, `fetch_pcr_data`, `fetch_max_pain`, `fetch_change_oi`, `fetch_fii_data`, `fetch_dii_data` — all calling V2 market endpoints.
- **Smartlist APIs (launched May 2026):** `fetch_smartlist_futures`, `fetch_smartlist_options`, `fetch_smartlist_mtf`.
- **DB migration `20260917_000000`:** Adds `depth_json` (JSONB), `source_type`, `change`, `change_pct`, `avg_traded_price` to `market_quote`; adds `oi`, `volume`, `ltp`, `prev_close`, `ltq`, `instrument_key` to `option_greeks_snapshot`; adds unique constraint `mq_instrument_exchange_ts_provider_uq` to enable market_quote upserts.
- **`set_db_engine()` injected into `MarketEngine` and `DualProviderEngine`** from server lifespan for persistence.
- **`DualProviderEngine` pre-constructed in `server.py`** with db_engine injection for option chain persistence.

### DB state after 2.1.0

| Table | Rows | New rows since 2.0.0 |
|-------|------|---------------------|
| equity_candle | 5,460,561 | +34,842 (intraday backfill) |
| market_quote | 3 | +3 (new live data) |
| option_greeks_snapshot | 10 | +10 (new live data) |
| option_chain_snapshot | 53 | +1 (real data added) |
| option_chain_contract | 10 | +10 (real data added) |

### Tests

4,413 unit tests passing (0 failures). +16 tests added for new Upstox V3 endpoints and market_quote/greeks persistence.

---

## [2.0.0] — 2026-09-15 (post-audit hardened)

### Added (schema redesign — v2)

- **Alembic migration `b1c2d3e4f5a6`** — 16 new tables across 6 schema layers, replacing `candle_bar` as the production write target for all Indian market data:
  - **Layer 1 (Instrument Identity)**: `instrument_provider_mapping` (provider token normalisation), `instrument_identity_history` (symbol/token change audit trail). `instrument_master` enhanced with `instrument_class` (EQ|IDX|FUT|OPT|CRYPTO|ETF) and `name` columns.
  - **Layer 2 (Canonical Candles — TimescaleDB hypertables, 7-day chunks)**: `equity_candle` (NSE/BSE equities + indices), `futures_candle` (F&O futures + OI), `options_candle` (F&O options + OI, CE/PE only). All three enforce a `3m` interval CHECK constraint and full OHLC validity constraints.
  - **Layer 3 (Live Data — TimescaleDB hypertables, 1-day chunks)**: `market_tick` (WebSocket ticks), `market_quote` (polled snapshots).
  - **Layer 4 (Option Chain)**: `option_chain_snapshot` (UUID PK, chain header), `option_chain_contract` (per-strike, Greeks nullable by design), `option_greeks_snapshot` (TimescaleDB hypertable, 1-day chunks).
  - **Layer 5 (Calendar + Sessions)**: `exchange_calendar` (NSE/BSE trading day registry, unique per `(exchange, segment, date)`), `market_session` (actual session records), `fno_universe_membership` (point-in-time F&O eligibility with `effective_from`/`effective_to`).
  - **Layer 6 (Operations)**: `ingestion_job` (UUID PK, self-referencing `parent_job_id`), `ingestion_checkpoint` (resumable job state), `candle_bar_quarantine` (unclassifiable migration rows).
  - `candle_bar` marked as deprecated archive (COMMENT DDL). Not dropped. Scheduled for removal 2026-10-15.
  - TimescaleDB promotion embedded in migration via `create_hypertable(..., if_not_exists => TRUE)` — no manual DDL step needed.

- **NSE data migration** (`scripts/migrate_candle_bar.py`): 5,425,719 NSE rows from `candle_bar` → `equity_candle`. Delta = 0 (verified).

- **F&O reference data** loaded via new scripts:
  - `scripts/load_fno_instrument_master.py`: 34,460 F&O contracts into `instrument_master` + 68,915 rows into `instrument_provider_mapping` (Angel One tokens, sourced from SmartAPI scrip master).
  - `scripts/load_fno_universe.py`: 238 active `fno_universe_membership` rows (effective_from 2020-01-01).
  - `scripts/populate_exchange_calendar.py`: 3,654 `exchange_calendar` rows (NSE/EQ + NFO/FO, 2024–2028).

- **`TradingCalendarService`** (`src/engines/trading_calendar_service.py`): DB-backed authoritative trading day resolver. Uses `exchange_calendar` as primary source; falls back to in-memory `HolidayCalendar`. All F&O backfill jobs must use this service. Guarantees: weekend ≠ holiday, NOT_PUBLISHED ≠ trading day, no look-ahead bias.

### Changed (API cutover — v2)

- **`src/api/india.py`** — `GET /v1/india/historical` now reads from `equity_candle` (NSE/BSE) or `futures_candle` (NFO/BFO). `candle_bar` is no longer read by any endpoint. Batch quotes endpoint URL corrected to `/v1/india/quotes/batch` (was `/v1/india/quotes?symbols=...`); maximum raised to 200 symbols (was 50).
- **`src/engines/historical_engine.py`** — `bulk_upsert_candles()` routes EQ/IDX → `equity_candle`, FO/FUT → `futures_candle`, OPT → `options_candle` via `_canonical_table_for()`. Never writes to `candle_bar`. F&O+1d routes to Angel One (not jugaad_data).
- **`src/db/timescale.py`** — `promote_hypertable()` changed from runtime DDL (was promoting `candle_bar`) to verification-only: detects TimescaleDB version and logs hypertable inventory. No DDL executed at startup.
- **`src/providers/adapters/scrapling_nse.py`** — `fetch_live_quote()` for index symbols (NIFTY, BANKNIFTY, FINNIFTY, etc.) routes to `allIndices` endpoint (139+ live index prices, no Akamai JS cookies required). `fetch_all_indices()` method added. NSE `quote-equity` still attempted for equity symbols (403 WAF known issue).

### Fixed (operational — this session)

- **TOTP multi-worker conflict** (`src/providers/adapters/angel_one.py` + `src/server.py`): `AngelOneAdapter` accepts `redis_client` parameter. Worker 1 does TOTP login and stores JWT at `mds:angel_one:jwt:{client_id}` (TTL 6 h) using a distributed lock. Workers 2–4 load JWT from Redis, skipping TOTP entirely. Zero HTTP 403 at startup with 4 Uvicorn workers.
- **`src/server.py` startup**: pre-creates `HistoricalEngine` with injected shared adapter instances and DB engine. `InstrumentMasterService` loaded from DB at startup. `MarketEngine` wired with real `AngelOneAdapter`.

### Fixed (test suite — 2026-09-15)

- **`tests/unit/db/test_timescale.py`**: 5 tests updated to match new `promote_hypertable` contract — no DDL executed, no `commit()` call, `_PROMOTE_HYPERTABLE_SQL` removed. Tests now verify `_LIST_HYPERTABLES_SQL` inventory query instead.
- **`tests/unit/providers/adapters/test_scrapling_nse.py`**: `test_successful_quote_source_type_is_open_source_nse_derived` updated to mock `fetch_all_indices()` for NIFTY (index fast path), not `_get` directly.
- **`tests/test_pre_fno_backfill_certification.py` + `tests/test_v2_schema_migration.py`**: `db_engine` fixture now parses `.env.local` directly to resolve `DATABASE_URL`, bypassing `get_settings()` lru_cache and the `Settings.model_config.env_file` class-level lock that baked in the wrong `.env` path when unit tests ran first. Fixture teardown switched to `engine.sync_engine.dispose()` to fix pre-existing `pytest-asyncio` 1.4.0 teardown error on Python 3.14.
- **`tests/unit/engines/test_quality_gate.py`**: `test_future_timestamp_fails` computed `_NOW_MS` at module import — caused flaky failure when full suite ran > 2 minutes. Fixed to compute `time.time()` at test execution.

**Total test count: 4,662 passing, 0 failing, 0 errors** (was 139 failing before this session).

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
