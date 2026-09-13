# DATA-SERVICE 2.0

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-green)](https://fastapi.tiangolo.com/)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-red)](https://docs.pydantic.dev/)
[![Redis 7](https://img.shields.io/badge/Redis-7-red)](https://redis.io/)
[![PostgreSQL 15](https://img.shields.io/badge/PostgreSQL-15%2B-blue)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Standalone, production-grade Market Data Platform.** Single market-data authority for AlphaForge. Every market data request flows through this service — no consumer calls an external provider directly.

> 📖 **Full documentation** → [`docs/DATA_SERVICE_GUIDE.md`](docs/DATA_SERVICE_GUIDE.md)  
> 🔗 **AlphaForge integration** → [`ALPHAFORGE_DATA_REQUIREMENTS.md`](ALPHAFORGE_DATA_REQUIREMENTS.md)  
> ✅ **Production certification** → [`PRODUCTION_CERTIFICATION.md`](PRODUCTION_CERTIFICATION.md)

---

## Quick Start

```bash
# 1. Start infrastructure (Redis + PostgreSQL)
docker compose --env-file .env.local up -d redis postgres

# 2. Run migrations
APP_ENV=local alembic upgrade head

# 3. Start the API server
APP_ENV=local uvicorn src.server:app --host 0.0.0.0 --port 8200 --reload

# 4. Verify
curl http://localhost:8200/v1/health/live
```

Port: **8200**. REST endpoints: `/v1/`. WebSocket: `ws://localhost:8200/v1/stream/ticks`.  
API docs (development only): http://localhost:8200/docs

---

## Environment Files

The platform uses an `APP_ENV` adapter to automatically select the right config file:

| `APP_ENV` | File | When to use |
|---|---|---|
| `local` | `.env.local` | Developer workstation, hot-reload |
| `production` | `.env.production` | Deployed containers |
| `staging` | `.env.staging` | Staging / UAT |
| *(unset)* | `.env` | Legacy fallback |

```bash
# Local development (loads .env.local)
APP_ENV=local uvicorn src.server:app --reload

# Production (loads .env.production)
APP_ENV=production uvicorn src.server:app

# Override a single variable without editing any file
APP_ENV=local LOG_LEVEL=WARNING uvicorn src.server:app --reload
```

### Env file setup

```bash
# Local dev — edit JWT_SECRET and add provider credentials before running
# The file ships with safe defaults for everything else
cat .env.local   # review the file

# Production — fill ALL REQUIRED fields before deploying
cp .env.example .env.production
# Edit .env.production: DATABASE_URL, POSTGRES_PASSWORD, JWT_SECRET,
# CONSUMER_API_KEYS, CORS_ALLOWED_ORIGINS, provider credentials
```

> ⚠️ `.env.production` is in `.gitignore`. Never commit it.

---

## Architecture

```
External Providers
  (Scrapling/NSE, Angel One SmartAPI, Upstox V2/V3, Jugaad-data,
   OpenChart, Yahoo Finance, Binance REST/WS, Deribit REST)
          │
          ▼
    Provider Gateway ── Capability Matrix + Circuit Breakers + Rate Limiter
          │
          ▼
    Core Engines ── Market Engine · Historical Engine · Streaming Engine
                     Instrument Master · Quality Engine
          │
          ▼
    Validation Pipeline (14 steps)
    Raw → Schema → Normalise → Timestamp → Semantic → Dedup →
    Gap → Freshness → Reconcile → Quality → Output → Cache → Persist → Deliver
          │
          ▼
    Storage
    L1 LRU (in-process, 10K entries) → L2 Redis 7 → L3 PostgreSQL 15
          │
          ▼
    REST /v1/ + WebSocket /v1/stream/ticks
```

**5 Docker Compose services**: `api` (port 8200), `worker`, `scheduler`, `redis`, `postgres`.

---

## Market Data Coverage

| Market | Instruments | Supported intervals |
|---|---|---|
| **Indian (NSE)** | Equities, F&O, Indices | `1m`, `5m`, `10m`, `15m`, `30m`, `1h`, `1d`, `1w`, `1M` |
| **Crypto (Binance)** | BTC/ETH/SOL spot + futures | `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d` |
| **Crypto (Deribit)** | BTC/ETH/SOL options | Various resolutions |

> ⛔ `3m` is **permanently unsupported for Indian market data** at every layer (API → normaliser → DB constraint). `3m` is allowed for Binance crypto.

---

## Authentication

Two methods, both work at all production endpoints:

### API Key (simplest)

```bash
curl -H "X-API-KEY: your-api-key" http://localhost:8200/v1/india/market/status
```

Set keys in `.env.local` / `.env.production` under `CONSUMER_API_KEYS`.

### JWT Bearer

```bash
# Exchange your API key for a short-lived JWT
TOKEN=$(curl -s "http://localhost:8200/v1/auth/token?api_key=your-api-key" | jq -r '.data.accessToken')

# Use the JWT
curl -H "Authorization: Bearer $TOKEN" http://localhost:8200/v1/india/market/status
```

Token lifetime: `JWT_EXPIRY_SECONDS` (default 3600s in production, 86400s locally).

---

## Key API Endpoints

All responses use the canonical envelope: `{"data": {...}, "metadata": {...}}`.  
All errors use: `{"error": {"code": "...", "message": "...", "requestId": "..."}}`.

### Health

```http
GET /v1/health/live       → always 200, never blocks
GET /v1/health/ready      → 200 when Redis+PG ready; 503 otherwise
GET /v1/analytics/health  → 200 healthy, 503 degraded
GET /metrics              → Prometheus scrape target
```

### Indian Market

```http
GET /v1/india/quotes/{symbol}                  → live quote
GET /v1/india/option-chain?underlying=NIFTY    → option chain snapshot
GET /v1/india/market/status                    → session phase + holidays
GET /v1/india/historical?symbol=&interval=1d&from=&to=  → OHLCV (max 10K)
GET /v1/india/historical/gaps                  → gap records
GET /v1/india/historical/reconciliation        → cross-provider stats
```

### Crypto

```http
GET /v1/crypto/{symbol}/ohlcv?interval=1h      → Binance OHLCV klines
GET /v1/crypto/futures/overview                → markPrice, funding, OI, L/S ratio
GET /v1/deribit/{currency}/overview            → Deribit options analytics (BTC|ETH|SOL)
GET /v1/deribit/ticker/{instrument_name}       → Deribit ticker (null fields preserved)
```

### Data Quality

```http
POST /v1/quality/evaluate                      → DataQualityGate evaluation
GET  /v1/quality/score?freshness=FRESH&...     → score from components
```

### Streaming

```http
WS /v1/stream/ticks                            → real-time tick stream
GET /v1/stream/status                          → streaming metrics
```

### Lineage

```http
GET /v1/provenance/{observation_id}            → provenance record
GET /v1/lineage/trade/{trade_id}               → trade forensics
GET /v1/contract                               → DataParityContract
```

---

## Data Quality

Every dataset receives a `DataConfidenceScore` in `[0, 95]` (the 95 cap is permanent — never 100).

| Grade | Score | `signalEngineAllowed` |
|---|---|---|
| HIGH | ≥ 80 | `true` (when gate passes) |
| MEDIUM | 50–79 | Depends on gate |
| LOW | 30–49 | `false` |
| BLOCKED | < 30 | `false` — unconditional |

Datasets with score < 60 are persisted for audit but not delivered.

---

## Null Semantics

**The most important correctness rule**: `null` means absent, `0` means zero. Never interchangeable.

| Field | Rule |
|---|---|
| `oi` (open interest) | `null + oiMissing:true` when absent. Never populated from `tradedValue`. |
| `iv` (implied volatility) | `null + ivMissing:true` when absent. Zero IV is prohibited. |
| `delta`, `gamma`, `theta`, `vega`, `rho` | `null` when absent. Placeholder zeros prohibited. |
| `bid`, `ask` | `null` when not provided. Placeholder zeros prohibited. |
| `volume` | `volume:0, volumeUnavailable:true` when source doesn't supply it. |

---

## Local Development

### Prerequisites

- Python 3.11+
- Docker Desktop (Compose V2)
- `pip` or `uv`

### Setup

```bash
# Install dependencies
pip install -e ".[dev]"

# Start infrastructure
docker compose --env-file .env.local up -d redis postgres

# Migrate database
APP_ENV=local alembic upgrade head

# Start API with hot-reload
APP_ENV=local uvicorn src.server:app --host 0.0.0.0 --port 8200 --reload

# Browse docs
open http://localhost:8200/docs
```

### Start all services with Docker Compose

```bash
docker compose --env-file .env.local up -d
docker compose exec api alembic upgrade head
```

### Code quality

```bash
ruff check src/ tests/          # lint
ruff format src/ tests/         # format
mypy src/                        # type-check
```

---

## Running Tests

```bash
# Unit tests (no infrastructure needed)
APP_ENV=local pytest tests/unit/ -q

# Integration tests (requires Redis + PostgreSQL)
docker compose --env-file .env.local up -d redis postgres
APP_ENV=local pytest -m integration -q

# Property-based tests (Hypothesis, 100+ examples each)
APP_ENV=local pytest tests/property/ -q -m property

# Performance benchmarks (mocked I/O, CI-safe)
APP_ENV=local pytest tests/performance/ -m performance -v -s

# All tests with coverage
APP_ENV=local pytest tests/unit/ --cov=src --cov-report=term-missing -q
```

Pytest markers: `integration` (needs Redis+PG), `performance` (slow), `property` (Hypothesis).

---

## Deployment

```bash
# 1. Fill in all REQUIRED fields in .env.production
#    (DATABASE_URL, POSTGRES_PASSWORD, JWT_SECRET, CONSUMER_API_KEYS, CORS_ALLOWED_ORIGINS)
vim .env.production

# 2. Deploy
APP_ENV=production docker compose --env-file .env.production up -d

# 3. Migrate
docker compose exec api alembic upgrade head

# 4. Verify
curl http://your-server:8200/v1/health/ready
```

### Pre-deploy checklist

- [ ] `alembic upgrade head` runs successfully
- [ ] `GET /v1/health/ready` returns 200
- [ ] `pytest tests/unit/ -q` passes
- [ ] `CORS_ALLOWED_ORIGINS` is set (no wildcard)
- [ ] `JWT_SECRET` is a strong random string (not the dev placeholder)
- [ ] `.env.production` is NOT committed to version control

---

## Database Migrations

```bash
APP_ENV=local alembic upgrade head          # apply all migrations
APP_ENV=local alembic current               # check current state
APP_ENV=local alembic history --verbose     # view history
APP_ENV=local alembic revision --autogenerate -m "my change"  # create migration
APP_ENV=local alembic downgrade -1          # roll back one step
```

### Optional: TimescaleDB hypertable

```bash
# Only if TimescaleDB extension is available
psql $DATABASE_URL -c "SELECT create_hypertable('candle_bar', 'time', if_not_exists => TRUE);"
```

---

## Observability

- **Prometheus**: `GET /metrics` — scrape at `data-service-api:8200`
- **Logs**: JSON-structured via structlog. `LOG_LEVEL=DEBUG` for verbose output.
- **Tracing**: OpenTelemetry via `OTEL_EXPORTER=noop|jaeger|otlp`
- **Dashboard**: `GET /v1/health/data` for live platform state
- **Alerts**: `GET /v1/analytics/health` (200 healthy, 503 degraded)

---

## Docker Compose Reference

```bash
docker compose --env-file .env.local up -d          # start all
docker compose --env-file .env.local down            # stop (data preserved)
docker compose --env-file .env.local down -v         # stop + remove volumes ⚠️
docker compose logs -f api                           # API logs
docker compose logs -f worker                        # worker logs
docker compose build api && docker compose up -d api # rebuild API
docker compose up -d --scale api=3                  # scale to 3 replicas
```

---

## Further Reading

| Document | Contents |
|---|---|
| [`docs/DATA_SERVICE_GUIDE.md`](docs/DATA_SERVICE_GUIDE.md) | Full technical deep-dive: all components, configuration, API reference, troubleshooting |
| [`ALPHAFORGE_DATA_REQUIREMENTS.md`](ALPHAFORGE_DATA_REQUIREMENTS.md) | AlphaForge integration contract: endpoints, null semantics, migration obligations |
| [`PRODUCTION_CERTIFICATION.md`](PRODUCTION_CERTIFICATION.md) | Requirements coverage, security checklist, deployment instructions |
| [`.env.example`](.env.example) | Annotated reference for all environment variables |
| [`.env.local`](.env.local) | Local development defaults (safe to commit with placeholders) |
| [`.kiro/specs/data-service-platform/requirements.md`](.kiro/specs/data-service-platform/requirements.md) | Full specification (23 requirements) |
