# DATA-SERVICE 2.0

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-green)](https://fastapi.tiangolo.com/)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-red)](https://docs.pydantic.dev/)
[![Redis 7](https://img.shields.io/badge/Redis-7-red)](https://redis.io/)
[![TimescaleDB](https://img.shields.io/badge/TimescaleDB-PG15-blue)](https://www.timescale.com/)
[![License: Proprietary](https://img.shields.io/badge/License-Proprietary-lightgrey.svg)]()

**Standalone, production-grade Market Data Platform.**  
Single market-data authority for AlphaForge. Every market data request flows through this service — no consumer calls an external provider directly.

| Document | Purpose |
|---|---|
| [`docs/DATA_SERVICE_GUIDE.md`](docs/DATA_SERVICE_GUIDE.md) | Full technical deep-dive: all components, configuration, troubleshooting |
| [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) | Complete API reference — 61 endpoints across 14 functional groups |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System design, module map, data flow diagrams |
| [`ALPHAFORGE_DATA_REQUIREMENTS.md`](ALPHAFORGE_DATA_REQUIREMENTS.md) | AlphaForge integration contract and null semantics |
| [`reports/20_FINAL_PRODUCTION_CERTIFICATION.md`](reports/20_FINAL_PRODUCTION_CERTIFICATION.md) | Authoritative production-readiness status |
| [`.env.example`](.env.example) | Annotated reference for every environment variable |

---

## Table of Contents

1. [Quick Start — Docker Compose (recommended)](#1-quick-start--docker-compose-recommended)
2. [Quick Start — Local (no Docker)](#2-quick-start--local-no-docker)
3. [Environment Files](#3-environment-files)
4. [Start / Stop Services](#4-start--stop-services)
5. [View Logs](#5-view-logs)
6. [Verify the Service](#6-verify-the-service)
7. [Authentication](#7-authentication)
8. [Key API Endpoints](#8-key-api-endpoints)
9. [Architecture Overview](#9-architecture-overview)
10. [Market Data Coverage](#10-market-data-coverage)
11. [Data Quality and Null Semantics](#11-data-quality-and-null-semantics)
12. [Database Migrations](#12-database-migrations)
13. [Running Tests](#13-running-tests)
14. [Code Quality](#14-code-quality)
15. [Deployment](#15-deployment)
16. [Observability](#16-observability)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Quick Start — Docker Compose (recommended)

> **Prerequisites**: Docker Desktop with Compose V2, credentials in `.env.local`.

```bash
# Step 1 — Start infrastructure + build and start the API
docker compose --env-file .env.local up -d --build api

# Step 2 — Run database migrations (first time, or after schema changes)
docker compose --env-file .env.local exec api alembic upgrade head

# Step 3 — Verify the API is healthy
curl http://localhost:8200/v1/health/live

# Step 4 — Follow live logs
docker compose --env-file .env.local logs -f api
```

The API is ready when you see `data_service_ready` in the logs and the health endpoint returns HTTP 200.

---

## 2. Quick Start — Local (no Docker)

> **Prerequisites**: Python 3.11+, Docker (for Redis + PostgreSQL only).

```bash
# Install dependencies (editable install includes dev extras)
pip install -e ".[dev]"

# Start only the infrastructure containers
docker compose --env-file .env.local up -d redis postgres

# Apply database migrations
APP_ENV=local alembic upgrade head

# Start the API server with hot-reload
APP_ENV=local uvicorn src.server:app --host 0.0.0.0 --port 8200 --reload

# Open interactive API docs (development only)
open http://localhost:8200/docs
```

---

## 3. Environment Files

The platform uses an `APP_ENV` variable to automatically select the right `.env` file at startup.

| `APP_ENV` | File loaded | Use case |
|---|---|---|
| `local` | `.env.local` | Developer workstation, hot-reload |
| `production` | `.env.production` | Deployed containers |
| `staging` | `.env.staging` | Staging / UAT |
| *(unset)* | `.env` | Legacy fallback |

The env file is resolved by `src/core/settings.py:_resolve_env_file()` at import time — changing `APP_ENV` after the process starts has no effect.

### First-time setup

```bash
# Local development — ships with safe defaults, just add your provider credentials
# CONSUMER_API_KEYS and JWT_SECRET are already set for local dev
cat .env.local   # review before running

# Production — copy the template and fill in every REQUIRED field
cp .env.example .env.production
# Edit .env.production:
#   DATABASE_URL, POSTGRES_PASSWORD, JWT_SECRET (min 32 chars),
#   CONSUMER_API_KEYS, CORS_ALLOWED_ORIGINS, provider credentials
```

> `.env.production` is in `.gitignore`. Never commit it.

### Important: Docker Compose uses `--env-file`, not `APP_ENV`

When running via Docker Compose, the env file is passed directly with `--env-file`. The container reads all values as OS environment variables, so `APP_ENV` inside the container does **not** need to be set — the compose file already injects every variable explicitly.

```bash
# Always use --env-file when running any docker compose command
docker compose --env-file .env.local <subcommand>
```

---

## 4. Start / Stop Services

### Start all services (first time or after a rebuild)

```bash
# Build image + start api (postgres and redis start automatically via depends_on)
docker compose --env-file .env.local up -d --build api
```

### Start individual services

```bash
# Infrastructure only (Redis + PostgreSQL)
docker compose --env-file .env.local up -d redis postgres

# API only (assumes redis + postgres already running)
docker compose --env-file .env.local up -d api

# Worker (background backfill / gap-recovery tasks)
docker compose --env-file .env.local up -d worker

# Scheduler (F&O universe refresh at 08:45 IST, JWT rotation at 23:55 IST)
docker compose --env-file .env.local up -d scheduler

# All services at once
docker compose --env-file .env.local up -d
```

### Rebuild after code changes

```bash
# Rebuild only the API image and restart the container
docker compose --env-file .env.local up -d --build api

# Rebuild everything
docker compose --env-file .env.local build
docker compose --env-file .env.local up -d
```

### Stop services

```bash
# Stop all containers (data volumes preserved)
docker compose --env-file .env.local down

# Stop and remove ALL volumes (wipes database + redis — destructive!)
docker compose --env-file .env.local down -v

# Stop a single service
docker compose --env-file .env.local stop api
```

### Container status

```bash
docker compose --env-file .env.local ps
```

Expected output when all services are up:

```
NAME                     STATUS          PORTS
data-service-api         Up (healthy)    0.0.0.0:8200->8200/tcp
data-service-worker      Up              
data-service-scheduler   Up              
data-service-redis       Up (healthy)    6379/tcp
data-service-postgres    Up (healthy)    0.0.0.0:5444->5432/tcp
```

---

## 5. View Logs

### Follow live logs (most common)

```bash
# API service — most relevant for debugging
docker compose --env-file .env.local logs -f api

# Show last 100 lines then follow (useful on first start)
docker compose --env-file .env.local logs -f --tail=100 api

# All services at once
docker compose --env-file .env.local logs -f

# Worker or scheduler
docker compose --env-file .env.local logs -f worker
docker compose --env-file .env.local logs -f scheduler
```

### Startup sequence to look for

A successful startup logs these events in order:

```
data_service_starting      version=2.0.0
redis_connected            url=redis://redis:6379/0
postgres_connected
angel_one_adapter_authenticated   provider=angel_one      ← if credentials set
upstox_adapter_ready              provider=upstox         ← if credentials set
market_engine_ready        real_provider=true
data_service_ready         version=2.0.0
```

If `angel_one_credentials_not_configured` or `upstox_credentials_not_configured` appear, the service still runs but those data sources return degraded/null responses.

### Useful log filters

```bash
# Show only warnings and errors
docker compose --env-file .env.local logs api | grep -E '"log_level":"(warning|error|critical)"'

# Show only startup events
docker compose --env-file .env.local logs api | grep -E "starting|connected|ready"

# Show auth rejections
docker compose --env-file .env.local logs api | grep "api_key_rejected"
```

---

## 6. Verify the Service

### Health endpoints

```bash
# Liveness — always returns 200, never blocks on dependencies
curl http://localhost:8200/v1/health/live

# Readiness — returns 200 when Redis + PostgreSQL are reachable; 503 otherwise
curl http://localhost:8200/v1/health/ready

# Operational snapshot — session state, gap counts, circuit-breaker states, clock skew
curl -H "X-API-KEY: dev-key-local-1" http://localhost:8200/v1/health/data
```

### Docker health check status

```bash
# Quick check — outputs: starting | healthy | unhealthy
docker inspect --format="{{.State.Health.Status}}" data-service-api

# Full health check history (last 5 attempts with output)
docker inspect --format='{{json .State.Health}}' data-service-api | python3 -m json.tool

# Poll every 2 seconds until healthy
watch -n 2 'docker inspect --format="{{.State.Health.Status}}" data-service-api'
```

### Verify authentication works

```bash
# API key auth — should return 200 with market status JSON
curl -s -H "X-API-KEY: dev-key-local-1" \
  http://localhost:8200/v1/india/market/status | python3 -m json.tool

# Exchange API key for a JWT
TOKEN=$(curl -s "http://localhost:8200/v1/auth/token?api_key=dev-key-local-1" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['accessToken'])")

# Use JWT bearer
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8200/v1/india/market/status | python3 -m json.tool
```

### Verify key endpoints

```bash
# NSE session status
curl -s -H "X-API-KEY: dev-key-local-1" http://localhost:8200/v1/india/market/status

# F&O instrument list (first 5)
curl -s -H "X-API-KEY: dev-key-local-1" \
  "http://localhost:8200/v1/instruments?market=NSE&limit=5" | python3 -m json.tool

# Binance BTC/USDT last 5 hourly candles
curl -s -H "X-API-KEY: dev-key-local-1" \
  "http://localhost:8200/v1/crypto/BTCUSDT/ohlcv?interval=1h&limit=5" | python3 -m json.tool

# Prometheus metrics
curl -s http://localhost:8200/metrics | head -30

# Interactive docs (development only)
open http://localhost:8200/docs
```

### Verify database

```bash
# Connect to PostgreSQL directly
docker compose --env-file .env.local exec postgres \
  psql -U mds_user -d mds -c "\dt"

# Check migrations are applied
docker compose --env-file .env.local exec api alembic current
```

---

## 7. Authentication

All `/v1/*` data endpoints require authentication. Two methods are accepted interchangeably.

### Method A — API Key (simplest)

Add the `X-API-KEY` header with any key from `CONSUMER_API_KEYS`:

```bash
curl -H "X-API-KEY: dev-key-local-1" http://localhost:8200/v1/india/market/status
```

Configure keys in your env file:
```bash
# .env.local
CONSUMER_API_KEYS=dev-key-local-1,dev-key-local-2
```

### Method B — JWT Bearer

Exchange an API key for a short-lived signed JWT, then use it as a Bearer token:

```bash
# Step 1 — Get a token (no auth required for this endpoint)
TOKEN=$(curl -s "http://localhost:8200/v1/auth/token?api_key=dev-key-local-1" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['accessToken'])")

# Step 2 — Use the token
curl -H "Authorization: Bearer $TOKEN" http://localhost:8200/v1/india/market/status
```

Token expiry: `JWT_EXPIRY_SECONDS` — defaults to `86400s` (24h) locally, `3600s` (1h) in production.

### Unauthenticated endpoints (no key needed)

```
GET  /v1/health/live
GET  /v1/health/ready
GET  /metrics
GET  /v1/auth/token
GET  /scraping/*        (AlphaForge compat — internal network only)
POST /data/gate         (AlphaForge compat — internal network only)
```

---

## 8. Key API Endpoints

All responses use the canonical envelope:
- Success: `{"data": {...}, "metadata": {...}}`
- Error: `{"error": {"code": "...", "message": "...", "requestId": "..."}}`

Full reference: [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md)

### Health & Observability

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/health/live` | No | Liveness probe — always 200 |
| GET | `/v1/health/ready` | No | Readiness probe — 503 if deps down |
| GET | `/v1/health/data` | Yes | Operational snapshot |
| GET | `/metrics` | No | Prometheus scrape target |

### Authentication

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/auth/token?api_key=` | No | Exchange API key → JWT |

### Indian Markets

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/india/market/status` | Yes | NSE session phase + holidays |
| GET | `/v1/india/quotes/{symbol}` | Yes | Live quote |
| GET | `/v1/india/option-chain` | Yes | Option chain snapshot |
| GET | `/v1/india/historical` | Yes | OHLCV candles (max 10K rows) |
| GET | `/v1/india/historical/gaps` | Yes | Detected candle gaps |
| GET | `/v1/india/historical/reconciliation` | Yes | Cross-provider reconciliation stats |

Query params for `/v1/india/historical`:
```
symbol=RELIANCE&exchange=NSE&interval=1d&from=2026-01-01&to=2026-09-01
```
Supported intervals: `1m` `5m` `10m` `15m` `30m` `1h` `1d` `1w` `1M` — **`3m` is permanently blocked**.

### Broker Analytics

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/broker-analytics/pcr` | Yes | Put/Call ratio |
| GET | `/v1/broker-analytics/oi-buildup` | Yes | OI buildup summary |
| GET | `/v1/broker-analytics/gainers-losers` | Yes | Top gainers/losers |

### Instruments

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/instruments` | Yes | Instrument master (filterable) |
| GET | `/v1/instruments/{symbol}` | Yes | Single instrument detail |
| GET | `/v1/instruments/fno-universe` | Yes | Current F&O universe |

### Crypto — Binance

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/crypto/{symbol}/ohlcv` | Yes | OHLCV klines |
| GET | `/v1/crypto/{symbol}/ticker` | Yes | Current price |
| GET | `/v1/crypto/{symbol}/stats` | Yes | 24-hour rolling stats |
| GET | `/v1/crypto/exchange-info` | Yes | Exchange info |
| GET | `/v1/crypto/futures/overview` | Yes | Mark price, funding, OI, L/S ratio |

### Crypto — Delta Exchange (India)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/delta/products` | Yes | All Delta Exchange products |
| GET | `/v1/delta/{symbol}/ticker` | Yes | Delta ticker |
| GET | `/v1/delta/{symbol}/ohlcv` | Yes | Delta OHLCV candles |

### Deribit Options

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/deribit/{currency}/overview` | Yes | Options analytics (IV, OI, PCR, max pain) |
| GET | `/v1/deribit/{currency}/instruments` | Yes | Active instruments |
| GET | `/v1/deribit/{currency}/index` | Yes | Index price |
| GET | `/v1/deribit/ticker/{instrument_name}` | Yes | Single instrument ticker |
| GET | `/v1/deribit/{instrument_name}/ohlcv` | Yes | Options OHLCV |

### Data Quality

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/v1/quality/evaluate` | Yes | Full DataQualityGate evaluation |
| GET | `/v1/quality/score` | Yes | Score from individual components |

### WebSocket Streaming

| Method | Path | Auth | Description |
|---|---|---|---|
| WS | `/v1/stream/ticks` | Yes | Real-time tick fan-out |
| GET | `/v1/stream/status` | Yes | Streaming metrics |

### Provenance & Lineage

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/v1/provenance/{observation_id}` | Yes | Per-observation lineage record |
| GET | `/v1/lineage/trade/{trade_id}` | Yes | Trade forensics report |
| GET | `/v1/contract` | Yes | DataParityContract snapshot |

### Replay Engine

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/v1/replay/session` | Yes | Create replay session |
| GET | `/v1/replay/session/{session_id}` | Yes | Session status |
| DELETE | `/v1/replay/session/{session_id}` | Yes | Terminate session |

---

## 9. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  External Data Providers                                        │
│  Angel One SmartAPI · Upstox V3 · NSE Scrapling · Yahoo Finance │
│  Jugaad-data · OpenChart · Binance REST/WS · Delta Exchange     │
│  Deribit REST                                                    │
└───────────────────────┬─────────────────────────────────────────┘
                        │
                        ▼
          ┌─────────────────────────┐
          │   Provider Gateway      │
          │  Capability Matrix      │
          │  Circuit Breakers (5f)  │
          │  Token-Bucket Rate Limiter│
          └───────────┬─────────────┘
                      │
                      ▼
          ┌───────────────────────────────────┐
          │  Core Engines                     │
          │  MarketEngine   (live Indian data) │
          │  HistoricalEngine (OHLCV backfill) │
          │  GapRecovery    (state machine)    │
          │  StreamingEngine (tick fan-out)    │
          │  QualityEngine  (DCS + gate)       │
          │  InstrumentMaster + F&O Universe   │
          │  HolidayCalendar + SessionStateMachine│
          └───────────┬───────────────────────┘
                      │
                      ▼
          ┌─────────────────────────────────┐
          │   14-Step Validation Pipeline   │
          │  Raw → Schema → Normalise →     │
          │  Timestamp → Semantic → Dedup → │
          │  Gap → Freshness → Reconcile →  │
          │  Quality → Output → Cache →     │
          │  Persist → Deliver              │
          └───────────┬─────────────────────┘
                      │
                      ▼
          ┌──────────────────────────────┐
          │   Three-Level Cache          │
          │  L1: In-process LRU (10K)   │
          │  L2: Redis 7 (512 MB)       │
          │  L3: PostgreSQL 15          │
          │      + TimescaleDB          │
          └───────────┬──────────────────┘
                      │
                      ▼
          ┌──────────────────────────────┐
          │   REST API + WebSocket       │
          │  FastAPI / Uvicorn (4 wkr)   │
          │  Port 8200                   │
          │  /v1/* — authenticated       │
          │  /v1/stream/ticks — WS       │
          └──────────────────────────────┘
```

**5 Docker Compose services**: `api` (8200), `worker`, `scheduler`, `redis`, `postgres` (5444 on host).

Full architecture reference: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

---

## 10. Market Data Coverage

| Market | Source(s) | Instruments | Intervals |
|---|---|---|---|
| **NSE Live** | Angel One SmartAPI | Equities, F&O, Indices | Tick / real-time |
| **NSE Historical** | Upstox V3, Angel One | Equities, F&O, Indices | `1m` `5m` `10m` `15m` `30m` `1h` `1d` `1w` `1M` |
| **NSE Open-source** | Yahoo Finance, Jugaad-data, OpenChart, NSE Scrapling | Equities, Indices | `1d` and select intraday |
| **Crypto Spot** | Binance REST + WS | BTC, ETH, SOL and others | `1m`–`1d` (incl. `3m`) |
| **Crypto Futures** | Binance Futures REST | Perpetuals | Mark price, funding, OI, L/S |
| **Crypto Options** | Deribit REST | BTC, ETH, SOL options | IV, OI, max pain, PCR |
| **Crypto Spot India** | Delta Exchange India REST | All Delta products | OHLCV, ticker |

> ⛔ `3m` is **permanently unsupported for Indian market data** at every layer — API, normaliser, DB constraint, and engine. Binance/Delta crypto explicitly allows `3m`.

---

## 11. Data Quality and Null Semantics

Every dataset receives a `DataConfidenceScore` (DCS) in `[0, 95]` — never 100.

| Grade | Score | Deliverable |
|---|---|---|
| HIGH | ≥ 80 | Delivered + signal engine allowed |
| MEDIUM | 50–79 | Delivered (gate dependent) |
| LOW | 30–49 | Delivered but signal engine blocked |
| BLOCKED | < 30 | Persisted for audit, **not delivered** |

**Null semantics** — the most important correctness rule: `null` = absent, `0` = zero. Never interchangeable.

| Field | Rule |
|---|---|
| `oi` (open interest) | `null` + `oiMissing: true` when unavailable. Never derived from `tradedValue`. |
| `iv` (implied volatility) | `null` + `ivMissing: true` when unavailable. Zero IV is prohibited. |
| `delta`, `gamma`, `theta`, `vega`, `rho` | `null` when absent. Placeholder zeros prohibited. |
| `bid`, `ask` | `null` when not provided by source. |
| `volume` | `volume: 0, volumeUnavailable: true` when source does not supply it. |

---

## 12. Database Migrations

```bash
# Apply all pending migrations (run after first start and after upgrades)
docker compose --env-file .env.local exec api alembic upgrade head

# Check current migration state
docker compose --env-file .env.local exec api alembic current

# View full migration history
docker compose --env-file .env.local exec api alembic history --verbose

# Create a new migration from model changes
docker compose --env-file .env.local exec api \
  alembic revision --autogenerate -m "describe your change"

# Roll back one migration
docker compose --env-file .env.local exec api alembic downgrade -1
```

When running locally (not in Docker):

```bash
APP_ENV=local alembic upgrade head
APP_ENV=local alembic current
APP_ENV=local alembic revision --autogenerate -m "my change"
APP_ENV=local alembic downgrade -1
```

### Optional: TimescaleDB hypertable

If TimescaleDB is available in your PostgreSQL instance, convert the OHLCV table to a hypertable for better time-series query performance:

```bash
docker compose --env-file .env.local exec postgres \
  psql -U mds_user -d mds \
  -c "SELECT create_hypertable('candle_bar', 'time', if_not_exists => TRUE);"
```

---

## 13. Running Tests

### Unit tests (no infrastructure required)

```bash
APP_ENV=local pytest tests/unit/ -q
```

### Integration tests (requires Redis + PostgreSQL)

```bash
# Ensure infrastructure is running
docker compose --env-file .env.local up -d redis postgres

APP_ENV=local pytest -m integration -q
```

### Property-based tests (Hypothesis)

```bash
APP_ENV=local pytest tests/property/ -q -m property
```

### Performance benchmarks (mocked I/O — CI-safe)

```bash
APP_ENV=local pytest tests/performance/ -m performance -v -s
```

### Full test suite with coverage

```bash
APP_ENV=local pytest tests/unit/ \
  --cov=src \
  --cov-report=term-missing \
  --cov-report=html:htmlcov \
  -q
```

### Test markers

| Marker | Meaning |
|---|---|
| `integration` | Requires live Redis + PostgreSQL |
| `performance` | Slow benchmark tests |
| `property` | Hypothesis property-based tests |

---

## 14. Code Quality

```bash
# Lint (checks style, imports, and type issues)
ruff check src/ tests/

# Auto-fix lint issues
ruff check --fix src/ tests/

# Format code
ruff format src/ tests/

# Type-check
mypy src/
```

All three tools are pre-configured in `pyproject.toml`.

---

## 15. Deployment

### Production Docker Compose

```bash
# 1. Fill ALL required fields in .env.production
#    Required: DATABASE_URL, POSTGRES_PASSWORD, JWT_SECRET (≥32 chars),
#              CONSUMER_API_KEYS, CORS_ALLOWED_ORIGINS, provider credentials
vim .env.production

# 2. Start all services
docker compose --env-file .env.production up -d

# 3. Run migrations
docker compose --env-file .env.production exec api alembic upgrade head

# 4. Verify
curl http://your-server:8200/v1/health/ready
curl http://your-server:8200/v1/health/live
```

### Pre-deploy checklist

- [ ] `alembic upgrade head` succeeds
- [ ] `GET /v1/health/ready` returns 200
- [ ] `pytest tests/unit/ -q` passes (0 failures)
- [ ] `CORS_ALLOWED_ORIGINS` is set (no wildcard `*`)
- [ ] `JWT_SECRET` is a strong random string — generate with `openssl rand -hex 32`
- [ ] `CONSUMER_API_KEYS` contains at least one real key
- [ ] `POSTGRES_PASSWORD` is a strong password (not the dev placeholder)
- [ ] `.env.production` is **not** committed to version control
- [ ] Provider credentials are set (Angel One, Upstox) or accepted as degraded

### Scaling

```bash
# Scale the API to 3 replicas (requires a load balancer in front)
docker compose --env-file .env.production up -d --scale api=3
```

---

## 16. Observability

| Signal | Endpoint / Method | Details |
|---|---|---|
| Prometheus metrics | `GET /metrics` | Scrape at `data-service-api:8200/metrics` |
| Structured logs | stdout / `docker logs` | JSON via structlog; `LOG_LEVEL=DEBUG` for verbose |
| OpenTelemetry traces | OTLP exporter | Configured via `OTEL_EXPORTER=noop\|jaeger\|otlp` |
| Health dashboard | `GET /v1/health/data` | Live platform state (session, gaps, DCS, circuit breakers) |
| Provider health | `GET /v1/analytics/health` | 200 = healthy; 503 = degraded |

---

## 17. Troubleshooting

### Container keeps restarting

```bash
# See the last crash reason
docker compose --env-file .env.local logs --tail=50 api
docker inspect --format='{{json .State}}' data-service-api | python3 -m json.tool
```

### `api_key_rejected` warnings in logs

The API key you're sending is not in `CONSUMER_API_KEYS` as seen by the running container. Most common cause: `CONSUMER_API_KEYS` is missing from the docker-compose `environment` block (fixed in current `docker-compose.yml`). Confirm the value is injected:

```bash
docker compose --env-file .env.local exec api \
  python3 -c "from src.core.settings import get_settings; s=get_settings(); print(s.consumer_api_keys)"
```

### `RuntimeError: /dev/null is an empty file`

Uvicorn 0.52+ rejects `--log-config /dev/null`. The Dockerfile CMD was updated to use `--no-access-log` instead. Rebuild the image:

```bash
docker compose --env-file .env.local up -d --build api
```

### `OSError: Readme file does not exist: README.md`

Hatchling requires `README.md` to be present in the Docker build context alongside `pyproject.toml`. The Dockerfile was updated to `COPY pyproject.toml README.md ./`. Rebuild:

```bash
docker compose --env-file .env.local up -d --build api
```

### CONSUMER_API_KEYS not taking effect

The settings are loaded once at startup and cached (`@lru_cache`). After changing env variables, restart the container:

```bash
docker compose --env-file .env.local restart api
```

### Angel One auth fails at startup

```
angel_one_adapter_auth_failed
```

Check credentials in `.env.local`: `ANGEL_ONE_API_KEY`, `ANGEL_ONE_CLIENT_ID`, `ANGEL_ONE_TOTP_SECRET`, `ANGEL_ONE_MPIN`. The service runs in degraded mode — live quotes and option chains return null values but all other functionality works.

### Upstox returns errors

`UPSTOX_ACCESS_TOKEN` expires daily. Refresh it via the Upstox developer console and update `.env.local`. The `scheduler` service rotates the Angel One JWT automatically at 23:55 IST but Upstox OAuth requires a manual browser flow for initial token issuance.

### Database connection refused

```bash
# Check postgres is healthy
docker compose --env-file .env.local ps postgres
docker compose --env-file .env.local logs postgres | tail -20

# Test connection manually
docker compose --env-file .env.local exec postgres \
  pg_isready -U mds_user -d mds
```

### Redis connection refused

```bash
docker compose --env-file .env.local exec redis redis-cli ping
# Expected: PONG
```

---

## Further Reading

| Document | Contents |
|---|---|
| [`docs/DATA_SERVICE_GUIDE.md`](docs/DATA_SERVICE_GUIDE.md) | Complete technical reference: all 25 sections covering every subsystem |
| [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) | Full API reference — 61 endpoints, all params, response shapes |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System design, module map, data flow, provider routing |
| [`docs/ALPHAFORGE_INDIAN_DATA_CONSUMER_CONTRACT.md`](docs/ALPHAFORGE_INDIAN_DATA_CONSUMER_CONTRACT.md) | AlphaForge integration contract for Indian data |
| [`ALPHAFORGE_DATA_REQUIREMENTS.md`](ALPHAFORGE_DATA_REQUIREMENTS.md) | Full AlphaForge integration contract and null-semantics obligations |
| [`reports/20_FINAL_PRODUCTION_CERTIFICATION.md`](reports/20_FINAL_PRODUCTION_CERTIFICATION.md) | Authoritative production-readiness certification |
| [`reports/18_RCA_AND_FIXES.md`](reports/18_RCA_AND_FIXES.md) | Root-cause analysis for all known issues and applied fixes |
| [`.env.example`](.env.example) | Annotated reference for all 50+ environment variables |
| [`.kiro/specs/data-service-platform/requirements.md`](.kiro/specs/data-service-platform/requirements.md) | Full 23-requirement specification |
