# Design Document — DATA-SERVICE 2.0

## Overview

DATA-SERVICE 2.0 is a standalone, production-grade Market Data Platform written in Python 3.11+ with FastAPI. It is the single market-data authority for AlphaForge and all future consumers. Every external provider call, normalisation step, quality check, and delivery path flows through this service. No consumer may call an external data provider directly.

The platform exposes two primary surfaces:
- A versioned REST API under `/v1/` for request/response data access
- A WebSocket endpoint (`WS /v1/stream/ticks`) for real-time streaming

It covers two market verticals:
- **Indian Markets** — NSE equities, F&O, indices; sourced from Scrapling/NSE, Angel One SmartAPI, Upstox V2/V3, Jugaad-data, OpenChart, Yahoo Finance (fallback only)
- **Crypto Markets** — BTC/ETH/SOL spot and derivatives; sourced from Binance REST/WebSocket and Deribit REST

> **3m Interval Rule**: The `3m` interval is **permanently unsupported** for Indian market data at every processing layer — acquisition planner, normaliser, persistence, and API. Binance natively supports `3m` for crypto, and this exception is documented and explicitly allowed only in the crypto path.

**Canonical Timeframes (Indian Markets)**: `1m`, `5m`, `10m`, `15m`, `30m`, `1h`, `1d`, `1w`, `1M`

**Crypto Timeframes**: `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d` (Binance native)

---

## Architecture

### High-Level Component Diagram

```mermaid
graph TB
    subgraph External["External Providers"]
        NSE["Scrapling/NSE\n(WAF bypass)"]
        AO["Angel One\nSmartAPI + SmartStream"]
        UPT["Upstox V2/V3\n(OAuth + Protobuf WS)"]
        JG["Jugaad-data\n(F&O EOD)"]
        OC["OpenChart\n(OHLCV)"]
        YF["Yahoo Finance\n(fallback only)"]
        BIN["Binance\nREST + WebSocket"]
        DER["Deribit\nREST"]
    end

    subgraph Gateway["Provider Gateway"]
        CM["Capability Matrix"]
        CB["Circuit Breakers\n(per provider × capability)"]
        RL["Token-Bucket\nRate Limiter"]
        AM["Auth Manager\n(JWT / OAuth / credential-free)"]
    end

    subgraph Engines["Core Engines"]
        ME["Market Engine\n(Indian live data)"]
        HE["Historical Engine\n(OHLCV + backfill)"]
        SE["Streaming Engine\n(WS + Event Bus)"]
        IM["Instrument Master\n(F&O universe)"]
        QE["Quality Engine\n(scoring + gate)"]
    end

    subgraph Pipeline["Validation Pipeline (14 steps)"]
        VP["Raw → Schema → Normalise →\nTimestamp → Semantic → Dedup →\nGap → Freshness → Reconcile →\nQuality → Output → Cache →\nPersist → Deliver"]
    end

    subgraph Storage["Storage"]
        L1["L1 LRU Cache\n(in-process, 10K entries)"]
        L2["L2 Redis 7+\n(mds: namespace)"]
        L3["L3 PostgreSQL 15+\n(TimescaleDB)"]
        EB["Event Bus\n(Redis Streams)"]
    end

    subgraph API["API Layer (FastAPI + Uvicorn)"]
        REST["REST /v1/"]
        WS["WS /v1/stream/ticks"]
    end

    subgraph Observability["Observability"]
        PROM["Prometheus /metrics"]
        OTEL["OpenTelemetry\n(spans + traces)"]
        SL["structlog\n(structured JSON)"]
    end

    External --> Gateway
    Gateway --> Engines
    Engines --> Pipeline
    Pipeline --> Storage
    Storage --> API
    Engines --> EB
    EB --> WS
    API --> Observability
```

### Deployment Topology

```mermaid
graph LR
    subgraph Container["Docker Compose"]
        API_SVC["api\nFastAPI + Uvicorn\nport 8200"]
        WORKER["worker\nBackfill + Gap Recovery\n+ Scheduler tasks"]
        SCHED["scheduler\nCron: 08:45 IST refresh,\n23:55 IST JWT rotation"]
        REDIS["redis:7\nL2 Cache + Event Bus\n+ Checkpoints"]
        PG["postgres:15\n+ TimescaleDB\nOHLCV + Provenance\n+ Instruments"]
    end

    API_SVC --> REDIS
    API_SVC --> PG
    WORKER --> REDIS
    WORKER --> PG
    SCHED --> REDIS
    SCHED --> PG
    API_SVC -- "publishes ticks" --> REDIS
    WORKER -- "backfill chunks" --> REDIS
```

### Startup Sequence

1. Load settings from environment variables (no hardcoded values)
2. Connect Redis — if unavailable, enter degraded mode
3. Connect PostgreSQL — if unavailable, enter degraded mode
4. Initialise Instrument Master from database snapshot
5. Start circuit breakers for all configured providers
6. Start WebSocket connections (Angel One SmartStream, Upstox Protobuf, Binance)
7. Start Streaming Engine tick publisher
8. Register SIGTERM handler for graceful shutdown (30s drain window)
9. Begin accepting requests on port 8200

---

## Technology Stack

| Concern | Choice | Notes |
|---|---|---|
| HTTP framework | FastAPI + Uvicorn | Async, OpenAPI-native |
| Schema / validation | Pydantic v2 | All canonical types; strict field typing |
| Async HTTP (outbound) | `httpx` (async) or `aiohttp` | Per-provider HTTP client pools |
| NSE WAF bypass | `curl_cffi` | Chrome TLS fingerprint impersonation; `Scrapling` for JS-rendered pages |
| Redis client | `redis[hiredis]` async | L2 cache, Event Bus (Redis Streams), distributed locks, checkpoints |
| PostgreSQL driver | `asyncpg` | Async; used via SQLAlchemy 2.x async ORM or direct for bulk upserts |
| Time-series | TimescaleDB (optional extension) | Hypertable on `candle_bar`; plain PG-compatible DDL |
| Bulk analytics | Polars or PyArrow | In-process OHLCV transforms; not pandas |
| Structured logging | `structlog` | JSON output in production; `correlation_id` on every entry |
| Distributed tracing | OpenTelemetry SDK | Jaeger / OTLP / no-op exporter, config-driven |
| Scheduling | APScheduler or `asyncio` tasks | 08:45 IST F&O refresh, 23:55 IST JWT rotation |
| Secrets | Environment variables + secrets store | Never committed to disk or logs |
| Crypto WS | `websockets` or `aiohttp` | Binance WS with exponential backoff |
| Indian broker WS | Angel One SmartStream SDK, Upstox Protobuf WS | Sole owners — not shared with consumers |
| Testing framework | `pytest` + `pytest-asyncio` | `hypothesis` for PBT |
| Container | Docker multi-stage build | Final image based on `python:3.11-slim` |

---

## Components and Interfaces

### Provider Gateway

The Provider_Gateway is the single egress point for all outbound provider calls. No other component may call a provider directly.

#### Capability Matrix Schema

```python
class ProviderCapability(BaseModel):
    provider: ProviderId
    dataType: DataType          # LIVE_QUOTE | HISTORICAL_OHLCV | OPTION_CHAIN | INSTRUMENT_MASTER
    instrumentClass: str        # EQ | FO | IDX | CRYPTO_SPOT | CRYPTO_FUTURES | CRYPTO_OPTIONS
    supported: bool
    liveSupported: bool
    historySupported: bool
    maxChunkDays: int           # provider-specific chunk limit
    requestsPerSecond: float    # token-bucket refill rate
    intervalSupport: list[str]  # which intervals are supported
    sourceType: SourceType      # BROKER_AUTHENTICATED | OPEN_SOURCE_NSE_DERIVED | CREDENTIAL_FREE
    priority: int               # lower = higher priority
```

**Provider chunk limits** (enforced in acquisition planner):

| Provider | Interval | Max Chunk Days |
|---|---|---|
| Angel One SmartAPI | `1m` | 30 |
| Angel One SmartAPI | `5m`, `15m` | 90 |
| Upstox V3 | `1m` | 7 |
| Upstox V3 | `5m`, `15m` | 30 |
| Upstox V3 | `1d` | 365 |
| OpenChart | any | 365 |
| Jugaad-data | `1d` (F&O EOD) | 3650 |

**Routing rules** (Capability_Matrix authority):
- Angel One SmartAPI: primary for multi-day intraday equity history (`1m`–`1h`)
- Upstox V3: primary for index intraday history; intervals not natively supported by Angel One
- OpenChart: open-source supplement for all Canonical_Timeframes; reconciliation fallback
- Jugaad-data: primary for F&O EOD history with OI
- Yahoo Finance: equity EOD fallback only; `SECONDARY_FALLBACK` provenance; max quality grade `B`

#### Circuit Breaker State Machine

```mermaid
stateDiagram-v2
    [*] --> CLOSED
    CLOSED --> OPEN : failure_count >= threshold\nwithin rolling window
    OPEN --> HALF_OPEN : recovery_window elapsed
    HALF_OPEN --> CLOSED : probe request succeeds
    HALF_OPEN --> OPEN : probe request fails
```

- **CLOSED** — all requests pass through; failures are counted
- **OPEN** — all requests rejected immediately with `PROVIDER_UNAVAILABLE`; no HTTP call made
- **HALF_OPEN** — exactly one probe request is dispatched; success → CLOSED; failure → OPEN
- Failure threshold: configurable 1–100 failures in rolling window (default: 50% of last N requests, minimum 5 requests)
- Recovery window: configurable 1–3600 seconds (default: 60s)
- **HTTP 429 handling**: do not increment failure counter; honour `Retry-After` header or apply 60s default backoff
- **`MARKET_CLOSED` semantics**: do not count as failure; do not increment circuit breaker
- **`UNSUPPORTED_CAPABILITY` semantics**: do not count as failure

Circuit breaker state is stored in Redis so all replicas share state:
- Key pattern: `mds:cb:{provider}:{capability}` → `{state, opened_at_ms, failure_count}`

#### Token-Bucket Rate Limiter

Each provider × capability pair has a token-bucket governed by `requestsPerSecond` from the Capability_Matrix:

```
capacity = requestsPerSecond * burst_multiplier (default 2x)
refill_rate = requestsPerSecond tokens/second
```

- Queue depth limit: configurable 1–10,000 (default 100); overflow returns `PROVIDER_QUEUE_FULL`
- Implemented with Redis atomic operations (`EVAL` Lua scripts) for cross-replica consistency
- Angel One: 3 req/s (NSE rate-limit proxy); Upstox: 10 req/s; Binance REST: 20 req/s; Deribit: 5 req/s

#### Provider Switch Logging

When a primary provider fails and a fallback is selected:
```json
{
  "event": "provider_switch",
  "fromProvider": "angel_one",
  "toProvider": "openchart",
  "reason": "circuit_breaker_open",
  "dataset": "HISTORICAL_OHLCV",
  "instrumentId": "NSE:RELIANCE:EQ",
  "timestamp": "2026-01-15T09:15:00.000Z"
}
```

---

### Instrument Master

The Instrument_Master is the canonical registry for all tradeable instruments.

#### Canonical Instrument Schema

```python
class Instrument(BaseModel):
    # Internal identity
    instrumentId: str               # platform canonical ID (UUID or deterministic hash)
    tradingSymbol: str              # e.g. "NIFTY25JANFUT"
    displaySymbol: str              # human-readable, e.g. "NIFTY Jan 2025 Fut"
    isin: Optional[str]

    # Exchange classification
    exchange: ExchangeEnum          # NSE | NFO | BSE | BFO
    segment: SegmentEnum            # EQ | FO | CD | COM
    instrumentType: InstrumentType  # EQ | FUTIDX | FUTSTK | OPTIDX | OPTSTK | ETF | IDX
    underlying: Optional[str]       # e.g. "NIFTY" for derivatives

    # Derivative-specific
    expiry: Optional[date]          # ISO-8601 UTC date
    strike: Optional[float]         # option strike price
    optionType: Optional[str]       # "CE" | "PE" | null

    # Contract specs
    lotSize: int
    tickSize: float

    # Lifecycle
    activeFrom: date                # point-in-time tracking
    activeTo: Optional[date]        # null = currently active

    # Provider token mappings (not exposed in consumer API by default)
    angelToken: Optional[str]
    angelSymbol: Optional[str]
    upstoxKey: Optional[str]
    upstoxSymbol: Optional[str]
```

Token mappings are stripped from consumer responses unless `?include=providerTokens` is passed.

#### F&O Universe Snapshot

```python
class FnoUniverseSnapshot(BaseModel):
    snapshotVersion: int            # monotonically increasing
    checksum: str                   # SHA-256 of sorted instrument IDs
    generatedAt: str                # UTC ISO-8601
    effectiveFrom: date
    effectiveTo: Optional[date]
    fnoEquityCount: int
    fnoIndexCount: int
    constituentCount: int           # total
    status: str                     # ACTIVE | SUPERSEDED
```

**Refresh lifecycle**:
1. At 08:45 IST on every trading day, the scheduler triggers a universe refresh
2. Fetch F&O eligible list from NSE upstream source
3. Compute SHA-256 checksum of sorted instrument IDs
4. If checksum matches current snapshot → no write, log idempotent skip
5. If checksum differs → write new snapshot, emit `lifecycleEvent` records for ADDED/REMOVED/SUSPENDED instruments
6. On expiry day by 09:15 IST: set `activeTo` on expired contracts; do not delete
7. If upstream unavailable: retain last successful snapshot; emit structured warning

---

### Market Engine (Indian Live Data)

#### NSE Session Phase State Machine

```mermaid
stateDiagram-v2
    [*] --> CLOSED
    CLOSED --> PRE_OPEN : 09:00 IST (trading day)
    PRE_OPEN --> PRE_OPEN_CALL_AUCTION : 09:08 IST
    PRE_OPEN_CALL_AUCTION --> REGULAR : 09:15 IST
    REGULAR --> POST_MARKET : 15:30 IST (or 13:00 IST on half-day)
    POST_MARKET --> CLOSED : 16:00 IST (or 13:30 IST on half-day)
    CLOSED --> MUHURAT : Muhurat session start (Diwali only)
    MUHURAT --> CLOSED : Muhurat session end
```

Phase classification rules (IST, each boundary inclusive at start, exclusive at end):
- `PRE_OPEN`: 09:00–09:08
- `PRE_OPEN_CALL_AUCTION`: 09:08–09:15
- `REGULAR`: 09:15–15:30 (or 09:15–13:00 on half-days)
- `POST_MARKET`: 15:30–16:00 (or 13:00–13:30 on half-days)
- `CLOSED`: all other instants, full NSE holidays, weekends

The session engine uses `zoneinfo.ZoneInfo("Asia/Kolkata")` for IST. IST interpretation occurs only inside `MarketSessionEngine`. All other timestamps are UTC.

Holiday calendar: fetched from NSE official holiday API before the first trading session of each new calendar year; cached in Redis + in-memory. If unavailable, the most recent successful calendar is retained; `calendarStatus` in `/v1/india/market/status` reflects the last successful refresh date.

#### Live Quote Fields

Every live quote carries:
`instrumentId`, `symbol`, `exchange`, `ltp`, `open`, `high`, `low`, `prevClose`, `change`, `changePct`, `volume`, `oi` (F&O only — null for equities, never populated from tradedValue), `tradedValue`, `totalBuyQty`, `totalSellQty`, `upperCircuit`, `lowerCircuit`, `weekHigh52`, `weekLow52`, `lastTradeTime`, `bid` (where available), `ask` (where available), plus full `provenance`.

**OI semantic enforcement**: `oi` represents open interest in contracts. It is never populated from `tradedValue`. If the provider does not supply `oi`, the field is `null` (`oiMissing: true`). This invariant is enforced in the Normaliser and is a hard validation rule.

#### Option Chain Pipeline

1. Acquire option chain snapshot from NSE (via Scrapling) or broker API
2. Validate each contract row for completeness
3. If `spotAge > 60s` → set `chainQuality: "DEGRADED"`, include `spotAgeMs`
4. Compute analytics: `pcrOi`, `pcrVolume`, `maxCeOiStrike`, `maxPeOiStrike`, `totalCeOi`, `totalPeOi`, `atmIv`, `maxPain`
5. Tag each computed field with `MetricTag` (`OBSERVED` | `DERIVED` | `MODELLED`)
6. Per-contract completeness flags: `oiMissing`, `oiChangeMissing`, `volumeMissing`, `ivMissing`
7. `iv: null` when not provided — zero is never a substitute
8. Publish to Event Bus as `OptionChainUpdated` event
9. Write to L2 cache with 15s TTL

---

### Historical Engine

#### Resumable Checkpointed Backfill

```
Checkpoint key: mds:backfill:checkpoint:{symbol}:{exchange}:{interval}
Value: UTC timestamp of last successfully persisted candle (no TTL — persistent)
```

On interruption (process termination, unhandled exception, or provider timeout > 30s), the backfill job resumes from the last checkpoint rather than restarting from scratch.

Chunk acquisition flow:
```
for date_range in chunk_date_ranges(from_ts, to_ts, max_chunk_days):
    candles = await gateway.fetch(provider, symbol, interval, date_range)
    validated = validation_pipeline.run(candles)
    await db.bulk_upsert(validated)
    await redis.set(checkpoint_key, validated[-1].time)
```

#### OHLCV Candle Invariants

Each candle is validated before persistence:
- `high >= max(open, close)`
- `low <= min(open, close)`
- `volume >= 0`
- all price values `> 0`

Failures generate `DataIncident` with `instrumentId`, `intervalStr`, `timestamp`, `failedInvariant`, `rejectedValues`, `detectedAt`.

If source does not supply volume: `volumeUnavailable: true`, `volume = 0` (distinguished from genuine zero-volume bar).

#### Cross-Provider Reconciliation

When two providers return OHLCV data for the same `(instrumentId, exchange, interval, timestamp)` tuple:

| Deviation formula | Status |
|---|---|
| `|A − B| / max(|A|, |B|) × 100 ≤ 0.5%` for all OHLCV fields | `CONFIRMED` |
| Any field between `0.5%` and `2.0%` | `MINOR_DISCREPANCY` |
| Any field exceeds `2.0%` | `MAJOR_DISCREPANCY` |

The overall tuple status is determined by the worst-case field deviation. `MAJOR_DISCREPANCY` generates a `DataIncident`.

#### Gap Detection and Recovery

Gap record schema:
```python
class DataGap(BaseModel):
    gapId: UUID
    instrumentId: str
    exchange: str
    intervalStr: str
    gapStart: int           # UTC epoch ms
    gapEnd: int             # UTC epoch ms
    durationSec: int
    recoveryStatus: GapStatus   # PENDING | RECOVERING | RECOVERED | EXHAUSTED
    recoveryAttempts: int
    expectedProvider: str
    recoveryProvider: Optional[str]
```

Recovery state machine:
- `PENDING` → schedule gap recovery using Capability_Matrix fallback provider
- `RECOVERING` → up to N attempts (default 5, valid range 1–10)
- `RECOVERED` → gap filled
- `EXHAUSTED` → max retries reached; retained for manual review; no further auto-retry; alert emitted

---

### Streaming Engine

#### WebSocket Connection Management

**Angel One SmartStream**: Sole owner is the Streaming Engine. Reconnect with exponential backoff: base 1s, doubles each attempt, max 60s, max 10 attempts. On each reconnect attempt, publish `{"type": "reconnect", "attempt": N}` to Event Bus. After exhaustion, publish `{"type": "connection_failed", "provider": "angel_one"}`.

**Upstox V3 Protobuf**: Same reconnect policy as above. Protobuf decoding happens inside the Streaming Engine; decoded fields are normalised to canonical schema before publishing.

**Binance WebSocket**: Exponential backoff, base 1s, max 30s (as specified). Heartbeat pings every 30 seconds. On `error` or `closed` state, log `symbol`, `reason`, `reconnectAttempt`; do not expose raw WebSocket error to consumers.

Ticks are published to the Event Bus at ≤200ms p99 from `receivedAtMs`.

#### Tick Publish Format

```json
{
  "tickId": "uuid-v4",
  "instrumentId": "NSE:NIFTY:IDX",
  "symbol": "NIFTY",
  "exchange": "NSE",
  "eventTimeMs": 1705300000123,
  "receivedAtMs": 1705300000234,
  "ltp": 22150.50,
  "change": 45.25,
  "changePct": 0.20,
  "volume": 1234567,
  "oi": null,
  "tradedValue": 5678901234,
  "source": "angel_one",
  "quality": 0.92,
  "isDuplicate": false
}
```

**Deduplication ID**: `SHA-256(instrumentId:eventTimeMs:source:ltp:volume)[:32]`

Duplicate ticks are published with `isDuplicate: true` rather than dropped (audit trail preservation).

#### Consumer WebSocket Fanout

`WS /v1/stream/ticks` accepts control messages:
```json
{"action": "subscribe", "symbols": ["NIFTY", "BANKNIFTY"]}
{"action": "unsubscribe", "symbols": ["NIFTY"]}
```

- Heartbeat messages every 10 seconds
- If no heartbeat response for 3 consecutive heartbeats within 30s window → close connection, unsubscribe all streams
- On connection close → unsubscribe within 5 seconds; decrement `subscribedSymbols` count
- Max 500 concurrent symbol tick streams; exceed → return error

---

### Validation Pipeline

Every dataset must traverse all 14 steps in order. A dataset that fails at any step does not advance.

| Step | Description |
|---|---|
| 1 | Raw response receipt from provider |
| 2 | JSON schema validation → reject + `DataIncident` on failure |
| 3 | Normaliser → canonical Pydantic schema |
| 4 | Timestamp normalisation to UTC epoch ms (IST for Indian, UTC for crypto) |
| 5 | Semantic validation (OI≠tradedValue, IV not zero-substituted, Greeks not zero-substituted, bid/ask not zero-substituted) |
| 6 | Duplicate detection via deterministic hash; flag `isDuplicate: true`, do not persist |
| 7 | Gap detection vs expected candle sequence; emit `DataGapEvent` |
| 8 | Freshness classification per instrument tier and session phase |
| 9 | Cross-provider reconciliation (CONFIRMED / MINOR / MAJOR) |
| 10 | Quality_Engine scoring → `DataConfidenceScore` + `DataQualityGate` |
| 11 | Canonical dataset output construction |
| 12 | Cache population (L2 and L1) |
| 13 | Persistence (L3 PostgreSQL) |
| 14 | API / Event_Bus delivery |

**Step skip policy**: No step may be skipped without an explicit `OVERRIDE_REASON` audit entry (1–500 characters) and a high-severity alert within 5 seconds.

**POOR_QUALITY rule**: Datasets with `DataConfidenceScore < 60` are flagged `POOR_QUALITY`. They are persisted with the flag for audit but are NOT delivered via API or Event Bus.

**Round-trip parser property** (enforced by test suite):
```
parse(serialise(parse(raw))) == parse(raw)
```
This must hold for every parser: JSON, Protobuf (Upstox), bhavcopy CSV, NSE charting response.

---

### Normaliser

The Normaliser maps provider-specific raw responses to canonical Pydantic schemas.

#### Null Semantics (non-negotiable)

| Field | Rule |
|---|---|
| `oi` | `null` + `oiMissing: true` when provider does not supply it. Never populated from `tradedValue`. |
| `tradedValue` | Never interchangeable with `oi`. Represents total traded value in INR. |
| `iv` | `null` when not provided. Zero IV is not a substitute. |
| `delta`, `gamma`, `theta`, `vega`, `rho` | `null` when not provided. Placeholder zeros are prohibited. |
| `bid`, `ask` | `null` when not actually provided. Placeholder zeros are prohibited. |
| `volume` | When source does not supply volume: `volumeUnavailable: true`, `volume = 0`. |

#### MetricTag per Field

Every analytics field is annotated:
- `OBSERVED` — directly from exchange feed
- `DERIVED` — computed from observed values (e.g., `changePct`, `pcrOi`)
- `MODELLED` — from a pricing model (e.g., `iv`, `delta` from a model)
- Default when undetermined: `OBSERVED`

#### normalisationVersion

`normalisationVersion` is a semver string (e.g., `"2.0.0"`) attached to every provenance record. Enables re-normalisation of stored datasets when the schema version changes.

#### Partial Response Handling

If a provider response passes initial schema validation but contains mixed valid/invalid fields:
- Valid fields are processed normally
- Invalid fields are set to `null` with corresponding missing flags
- Processing continues rather than dropping the entire response

If a response fails schema validation entirely → log structured error (`provider`, `endpoint`, `rawResponseHash`, `validationErrors`, `receivedAt`) → drop response.

#### Deribit Instrument Name Parser

Deribit instrument names follow the pattern: `{CURRENCY}-{DD}{MON}{YY}-{STRIKE}-{C|P}`

Parsing rules:
- `baseCurrency`: currency prefix (BTC, ETH, SOL)
- `expiryTs`: UTC epoch ms at 08:00 UTC on the expiry date
- `strike`: numeric strike price
- `optionType`: `C` → `CE`, `P` → `PE`
- Instruments that fail parsing are silently dropped with a logged warning

Round-trip validation: parsing then re-serialising must produce a string with the same `currency`, `strike`, `optionType` (as `C`/`P`), and expiry formatted as `{DD}{MON}{YY}` (day zero-padded, month uppercase 3-letter).

---

### Quality Engine

#### DataConfidenceScore Formula

```
score = freshness_score    (0–35, weight 35%)
      + completeness_score (0–25, weight 25%)
      + provider_score     (0–20, weight 20%)
      + timestamp_score    (0–10, weight 10%)
      + agreement_score    (0–10, weight 10%)
```

Maximum is capped at **95** — never 100, reflecting inherent market-data uncertainty.

Freshness scoring:
- `FRESH` → 35
- `AGING` → 25
- `STALE` → 10
- `EXPIRED` → 0
- `UNKNOWN` → 5

If sequence integrity is broken, apply 20% penalty: `score = int(score * 0.8)`.

`DataConfidenceScore < 30` → grade `BLOCKED` → `signalEngineAllowed: false` with no exceptions.

#### DataQualityGate

```python
class DataQualityGate(BaseModel):
    dataFresh: bool
    dataComplete: bool
    dataTimestampValid: bool
    dataProviderHealthy: bool
    dataSemanticallyValid: bool
    signalEngineAllowed: bool   # true iff all five are true
    confidenceScore: int        # 0–95
    quality: DataQuality        # VALID|DEGRADED|PARTIAL|STALE|INVALID|UNKNOWN
    blockReasons: list[str]     # populated when signalEngineAllowed is false
    gates: dict[str, bool]      # individual gate values
    circuitBreakers: dict       # circuit breaker states at evaluation time
```

`signalEngineAllowed = true` iff all five boolean conditions are `true`.

#### Freshness Thresholds (REGULAR session)

| Tier | FRESH threshold |
|---|---|
| Index (NIFTY, BANKNIFTY, etc.) | ≤ 10 seconds |
| F&O liquid instruments | ≤ 10 seconds |
| F&O normal instruments | ≤ 15 seconds |
| Equity instruments | ≤ 30 seconds |

**Extended freshness windows (non-REGULAR sessions)**:

| Tier | FRESH threshold |
|---|---|
| Index | ≤ 60 seconds |
| F&O liquid | ≤ 60 seconds |
| F&O normal | ≤ 90 seconds |
| Equity | ≤ 120 seconds |

#### Quality Classification

| Classification | Condition |
|---|---|
| `VALID` | score ≥ 80, fresh, complete |
| `DEGRADED` | score 50–79 |
| `PARTIAL` | completeness 40–99%, at least one required field missing |
| `STALE` | data outside freshness window |
| `INVALID` | fundamental integrity failure |
| `UNKNOWN` | cannot be determined |

#### Strategy-Specific Overrides

Each strategy declares `maxQuoteAgeMs`, `minConfidenceScore` (minimum 30), `requiresOI`, `requiresOptionChain`, `requiresConfirmedCandle`. Strategy overrides apply in addition to the global gate but cannot relax any global gate condition.

#### Option Chain Quality Checks

Reject records with:
- Crossed markets (`bid > ask`)
- Negative IV
- Negative OI
- Invalid Greeks (`|delta| > 1`, `gamma < 0`)

Each violation generates a `DataIncident` record.

---

### Multi-Level Cache

#### Cache Hierarchy

```
L1 (in-process LRU, 10K entries, ≤1ms p99)
  ↓ miss
L2 (Redis 7+, ≤5ms p99, mds: namespace)
  ↓ miss
L3 (PostgreSQL 15+ read path)
  ↓ miss
Provider call
```

#### L2 Key Namespace

Pattern: `mds:{dataType}:{provider}:{exchange}:{symbol}:{interval}:{from}:{to}`

When a field is not applicable, substitute `_`.

Examples:
- Live quote: `mds:quote:angel_one:NSE:NIFTY:_:_:_`
- Historical candles: `mds:candle:upstox:NSE:RELIANCE:1m:1705300000000:1705386400000`
- Option chain: `mds:optchain:scrapling:NSE:NIFTY:_:_:_`
- Instrument master: `mds:instrument_master:_:NSE:_:_:_:_`

#### TTL Table (L2 Redis)

| Data type | TTL |
|---|---|
| Single live quote | 3 seconds |
| Batch live quotes | 3 seconds |
| Intraday candles | 30 seconds |
| Daily+ candles | 4 hours |
| Option chain snapshot | 15 seconds |
| Instrument master | 12 hours |
| Provider health | 5 seconds |

#### Cache Behaviours

**Stale-while-revalidate**: When a cache entry's remaining TTL is within 20% of its configured TTL, return cached data immediately and initiate a single background refresh. Do not start a second refresh if one is already in progress.

**Request coalescing**: When multiple concurrent requests arrive for the same cache key while a provider call is already in-flight, deduplicate to a single provider call and deliver the result to all waiting requests. If the provider call fails, return error to all waiting requests.

**On provider call success**: Write result to L2 Redis and L1 in-process cache before returning to consumer.

**L1 eviction**: LRU — evict least-recently-used entry when capacity (10,000) is reached.

**L2 unavailability**: Fall back to direct provider calls; log `cache_l2_unavailable` warning; do not return error to consumer.

**Cache-hit metadata**: When served from L1 or L2, set `dataSourceType: "CACHED"` and preserve original live provider in `provenance.sourceChain[0]`.

---

## Data Models

### Core Pydantic Schemas

```python
# Enumerations
class InstrumentType(str, Enum):
    EQ = "EQ"
    FUTIDX = "FUTIDX"
    FUTSTK = "FUTSTK"
    OPTIDX = "OPTIDX"
    OPTSTK = "OPTSTK"
    ETF = "ETF"
    IDX = "IDX"

class SessionPhase(str, Enum):
    PRE_OPEN = "PRE_OPEN"
    PRE_OPEN_CALL_AUCTION = "PRE_OPEN_CALL_AUCTION"
    REGULAR = "REGULAR"
    POST_MARKET = "POST_MARKET"
    CLOSED = "CLOSED"
    MUHURAT = "MUHURAT"

class DataSourceType(str, Enum):
    LIVE = "LIVE"
    HISTORICAL = "HISTORICAL"
    DERIVED = "DERIVED"
    CACHED = "CACHED"

class MetricTag(str, Enum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    MODELLED = "MODELLED"

class GapStatus(str, Enum):
    PENDING = "PENDING"
    RECOVERING = "RECOVERING"
    RECOVERED = "RECOVERED"
    EXHAUSTED = "EXHAUSTED"

class IncidentSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

# Core data structures
class DataProvenance(BaseModel):
    dataObservationId: str               # UUID v4
    source: DataSource
    sourceVersion: str
    eventTimeMs: Optional[int]           # exchange event time, UTC ms
    receivedAtMs: int                    # when platform received it
    availableAtMs: int                   # when consumers may use it
    normalisationVersion: str            # semver, e.g. "2.0.0"
    validationApplied: bool
    isFallback: bool
    fallbackReason: Optional[str]
    sourceChain: list[str] = []          # provider chain for cached responses

class OHLCVCandle(BaseModel):
    time: int                            # UTC epoch seconds (candle open time)
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: Optional[int] = None
    volumeUnavailable: bool = False
    provenance: DataProvenance

class LiveQuote(BaseModel):
    instrumentId: str
    symbol: str
    exchange: str
    ltp: float
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    prevClose: Optional[float]
    change: Optional[float]
    changePct: Optional[float]
    volume: Optional[int]
    oi: Optional[int]                    # F&O only; null for equities
    tradedValue: Optional[float]         # INR; distinct from OI
    totalBuyQty: Optional[int]
    totalSellQty: Optional[int]
    upperCircuit: Optional[float]
    lowerCircuit: Optional[float]
    weekHigh52: Optional[float]
    weekLow52: Optional[float]
    lastTradeTime: Optional[str]         # UTC ISO-8601
    bid: Optional[float]                 # null when not provided
    ask: Optional[float]                 # null when not provided
    marketStatus: str
    provenance: DataProvenance

class DataIncident(BaseModel):
    incidentId: str                      # UUID v4
    incidentType: str                    # OHLC_INVARIANT | SEMANTIC_INTEGRITY | GAP | etc.
    instrumentId: str
    provider: str
    timestamp: str                       # UTC ISO-8601
    severity: IncidentSeverity
    details: dict                        # incident-specific fields

class DataGapEvent(BaseModel):
    gapStartMs: int
    gapEndMs: int
    expectedCount: int
    actualCount: int
    severity: str                        # LOW(1 missing) | MEDIUM(2-5) | HIGH(>5)
```

### PostgreSQL Schema

#### `candle_bar` (TimescaleDB hypertable)

```sql
CREATE TABLE candle_bar (
    id              BIGSERIAL,
    instrument_id   VARCHAR(64)     NOT NULL,
    exchange        VARCHAR(8)      NOT NULL,
    interval_str    VARCHAR(4)      NOT NULL,   -- '1m','5m','10m','15m','30m','1h','1d','1w','1M'
    time            TIMESTAMPTZ     NOT NULL,   -- candle open time (UTC)
    open            NUMERIC(18,6)   NOT NULL,
    high            NUMERIC(18,6)   NOT NULL,
    low             NUMERIC(18,6)   NOT NULL,
    close           NUMERIC(18,6)   NOT NULL,
    volume          BIGINT          NOT NULL DEFAULT 0,
    oi              BIGINT,                     -- null for non-F&O
    volume_unavailable BOOLEAN      NOT NULL DEFAULT FALSE,
    provider        VARCHAR(32)     NOT NULL,
    source_type     VARCHAR(32)     NOT NULL,   -- BROKER_AUTHENTICATED | OPEN_SOURCE_NSE_DERIVED
    source_timestamp TIMESTAMPTZ,
    received_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    normalisation_version VARCHAR(16) NOT NULL DEFAULT '2.0.0',
    dataset_version BIGINT          NOT NULL,
    session_date    DATE            NOT NULL,   -- IST trading day
    reconciliation_status VARCHAR(32),
    poor_quality    BOOLEAN         NOT NULL DEFAULT FALSE,
    data_observation_id UUID,
    PRIMARY KEY (id, time)
);

-- TimescaleDB hypertable (single DDL, no data migration needed for plain PG)
-- SELECT create_hypertable('candle_bar', 'time', chunk_time_interval => INTERVAL '1 day');

-- Key indexes
CREATE UNIQUE INDEX candle_bar_uq
    ON candle_bar (instrument_id, exchange, interval_str, time);
CREATE INDEX candle_bar_symbol_interval_time
    ON candle_bar (instrument_id, interval_str, time DESC);

-- 3m interval guard (defense-in-depth)
ALTER TABLE candle_bar ADD CONSTRAINT no_3m_interval
    CHECK (interval_str <> '3m');
```

#### `instrument_master`

```sql
CREATE TABLE instrument_master (
    instrument_id   VARCHAR(64)     PRIMARY KEY,
    trading_symbol  VARCHAR(64)     NOT NULL,
    display_symbol  VARCHAR(128),
    isin            VARCHAR(12),
    exchange        VARCHAR(8)      NOT NULL,
    segment         VARCHAR(8)      NOT NULL,
    instrument_type VARCHAR(16)     NOT NULL,
    underlying      VARCHAR(64),
    expiry          DATE,
    strike          NUMERIC(18,2),
    option_type     VARCHAR(4),                 -- 'CE' | 'PE' | NULL
    lot_size        INT             NOT NULL DEFAULT 1,
    tick_size       NUMERIC(10,4)   NOT NULL DEFAULT 0.05,
    active_from     DATE            NOT NULL,
    active_to       DATE,                       -- null = currently active
    -- Provider token mappings
    angel_token     VARCHAR(32),
    angel_symbol    VARCHAR(64),
    upstox_key      VARCHAR(64),
    upstox_symbol   VARCHAR(64),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX im_trading_symbol ON instrument_master (trading_symbol);
CREATE INDEX im_active_instruments ON instrument_master (exchange, instrument_type)
    WHERE active_to IS NULL;
CREATE INDEX im_underlying_expiry ON instrument_master (underlying, expiry)
    WHERE expiry IS NOT NULL;
```

#### `fno_universe_snapshot`

```sql
CREATE TABLE fno_universe_snapshot (
    id                  BIGSERIAL       PRIMARY KEY,
    snapshot_version    INT             NOT NULL,
    checksum            VARCHAR(64)     NOT NULL UNIQUE,  -- SHA-256
    generated_at        TIMESTAMPTZ     NOT NULL,
    effective_from      DATE            NOT NULL,
    effective_to        DATE,
    fno_equity_count    INT             NOT NULL,
    fno_index_count     INT             NOT NULL,
    constituent_count   INT             NOT NULL,
    status              VARCHAR(16)     NOT NULL DEFAULT 'ACTIVE'  -- ACTIVE | SUPERSEDED
);
```

#### `data_gap`

```sql
CREATE TABLE data_gap (
    gap_id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id       VARCHAR(64)     NOT NULL,
    exchange            VARCHAR(8)      NOT NULL,
    interval_str        VARCHAR(4)      NOT NULL,
    gap_start           TIMESTAMPTZ     NOT NULL,
    gap_end             TIMESTAMPTZ     NOT NULL,
    duration_sec        INT             NOT NULL,
    recovery_status     VARCHAR(16)     NOT NULL DEFAULT 'PENDING',
    recovery_attempts   INT             NOT NULL DEFAULT 0,
    expected_provider   VARCHAR(32)     NOT NULL,
    recovery_provider   VARCHAR(32),
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX data_gap_pending ON data_gap (instrument_id, interval_str)
    WHERE recovery_status = 'PENDING';
```

#### `data_incident`

```sql
CREATE TABLE data_incident (
    incident_id     UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_type   VARCHAR(64)     NOT NULL,
    instrument_id   VARCHAR(64)     NOT NULL,
    provider        VARCHAR(32)     NOT NULL,
    timestamp       TIMESTAMPTZ     NOT NULL,
    severity        VARCHAR(16)     NOT NULL,
    details         JSONB           NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX di_instrument_ts ON data_incident (instrument_id, timestamp DESC);
CREATE INDEX di_severity ON data_incident (severity, timestamp DESC);
```

#### `data_provenance`

```sql
CREATE TABLE data_provenance (
    observation_id      UUID            PRIMARY KEY,
    dataset_key         VARCHAR(256)    NOT NULL,
    instrument_id       VARCHAR(64)     NOT NULL,
    exchange            VARCHAR(8),
    interval_str        VARCHAR(4),
    session_date        DATE,
    provider            VARCHAR(32)     NOT NULL,
    source_type         VARCHAR(32)     NOT NULL,
    authenticated       BOOLEAN         NOT NULL DEFAULT FALSE,
    fetched_at          TIMESTAMPTZ     NOT NULL,
    source_timestamp    TIMESTAMPTZ,
    data_as_of          TIMESTAMPTZ,
    response_hash       VARCHAR(64),
    from_ts             TIMESTAMPTZ,
    to_ts               TIMESTAMPTZ,
    dataset_version     BIGINT,
    normalisation_version VARCHAR(16)   NOT NULL DEFAULT '2.0.0',
    data_trust_status   VARCHAR(32)     NOT NULL DEFAULT 'TRUSTED',
    row_count           INT,
    is_fallback         BOOLEAN         NOT NULL DEFAULT FALSE,
    fallback_reason     TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX dp_instrument_received ON data_provenance (instrument_id, fetched_at DESC);
```

#### `provider_health`

```sql
CREATE TABLE provider_health (
    id              BIGSERIAL       PRIMARY KEY,
    provider        VARCHAR(32)     NOT NULL,
    capability      VARCHAR(32)     NOT NULL,
    status          VARCHAR(16)     NOT NULL,   -- UP | DOWN | DEGRADED | UNKNOWN
    circuit_state   VARCHAR(16)     NOT NULL,   -- CLOSED | OPEN | HALF_OPEN
    availability    NUMERIC(5,4)    NOT NULL DEFAULT 1.0,
    latency_p50_ms  INT,
    latency_p99_ms  INT,
    error_rate      NUMERIC(5,4)    NOT NULL DEFAULT 0.0,
    last_success_at TIMESTAMPTZ,
    last_failure_reason TEXT,
    semantic_integrity BOOLEAN      NOT NULL DEFAULT TRUE,
    measured_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX ph_provider_cap ON provider_health (provider, capability, measured_at DESC);
```

#### Bulk Upsert Pattern (candle_bar)

```sql
INSERT INTO candle_bar (instrument_id, exchange, interval_str, time, open, high, low, close, volume, oi, provider, source_type, dataset_version, session_date, normalisation_version)
VALUES (...)
ON CONFLICT (instrument_id, exchange, interval_str, time)
DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    oi = EXCLUDED.oi,
    provider = EXCLUDED.provider,
    dataset_version = EXCLUDED.dataset_version,
    normalisation_version = EXCLUDED.normalisation_version;
```

---

## Data Provenance and Lineage Tracking

### Lineage Store

The lineage store maintains two tiers:
1. **In-memory LRU** — most recent 100,000 observation records (evicted in insertion order)
2. **PostgreSQL** — all records persisted indefinitely until explicitly deleted by an authorised admin operation

Provenance is persisted within 1,000ms of successful data acquisition. On persistence failure: retry up to 3 times at 500ms intervals, then log persistence failure and discard record.

### DataProvenance Schema (Runtime)

```python
class DataProvenance(BaseModel):
    dataObservationId: str          # UUID v4; assigned at ingestion
    source: DataSource
    sourceVersion: str
    eventTimeMs: Optional[int]      # exchange event time, UTC ms
    receivedAtMs: int
    availableAtMs: int
    normalisationVersion: str       # semver
    validationApplied: bool
    isFallback: bool
    fallbackReason: Optional[str]
    sourceChain: list[str] = []
```

### Trade Forensics

When a paper trade or signal is generated, the following provenance fields are recorded in the trade record:
`dataObservationId`, `quoteAgeAtEntryMs`, `dataConfidenceAtEntry`, `dataQualityAtEntry`, `dataProviderAtEntry`, `dataIsFallback`, `observationEventTime`

`GET /v1/lineage/forensics/{tradeId}` returns a joined response within 1,000ms comprising:
- Trade record
- Signal record
- Provenance record
- Lineage entry

If any constituent record is missing, return HTTP 404 identifying which record is absent.

---

## API Design

### Canonical Success Envelope

```json
{
  "data": { },
  "metadata": {
    "requestedAt": "2026-01-15T09:15:00.000Z",
    "dataAsOf": "2026-01-15T09:14:59.750Z",
    "dataSourceType": "LIVE",
    "provider": "angel_one",
    "provenance": { },
    "marketStatus": "REGULAR",
    "quality": {
      "score": 87,
      "grade": "VALID"
    }
  }
}
```

### Canonical Error Envelope

```json
{
  "error": {
    "code": "INTERVAL_NOT_SUPPORTED",
    "message": "interval 3m is permanently unsupported",
    "provider": null,
    "retryAfterMs": null,
    "requestId": "req-uuid-v4"
  }
}
```

No stack traces, no credentials, no internal file paths in error responses.

### HTTP Status Mapping

| Condition | Status |
|---|---|
| Success | 200 |
| Unsupported interval, missing param, malformed date | 400 |
| Unauthenticated | 401 |
| Authenticated but unauthorised | 403 |
| Resource not found | 404 |
| Method not allowed (e.g., modifying provenance) | 405 |
| Rate limit exceeded | 429 |
| Provider failures exhaust all fallbacks | 502 |
| Platform unavailable (Redis/DB not ready) | 503 |

### Cache-Control Headers

| Response type | Cache-Control header |
|---|---|
| Live quote | `public, s-maxage=3, stale-while-revalidate=5` |
| Intraday candles | `public, s-maxage=30, stale-while-revalidate=60` |
| Daily candles | `public, s-maxage=300` |
| Option chain | `public, s-maxage=15, stale-while-revalidate=20` |
| Error response | `no-store` |

### Complete Endpoint Listing

**Platform / Health**
```
GET  /v1/health/live           → {status, version, uptimeMs, timestamp} — always HTTP 200
GET  /v1/health/ready          → HTTP 200 when Redis+PG respond in <2s; else HTTP 503
GET  /v1/health/data           → session state, freshness stats, gap counts, circuit states, clock skew
GET  /metrics                  → Prometheus metrics
GET  /v1/monitoring/stats      → rolling 3600s P50/P99/successRate/requestCount per endpoint
```

**Instruments**
```
GET  /v1/instruments                        → list with filters: exchange, instrumentType, underlying, segment, expiry
GET  /v1/instruments/{instrumentId}         → single instrument lookup
GET  /v1/instruments/fno-universe           → current F&O universe snapshot
GET  /v1/instruments/fno-universe/history   → past snapshots (paginated, max 100/page)
```

**Indian Market — Live**
```
GET  /v1/india/quotes/{symbol}              → live quote
GET  /v1/india/quotes                       → batch quotes (query: symbols=A,B,C)
GET  /v1/india/option-chain                 → option chain (params: underlying, expiry)
GET  /v1/india/market/status               → session phase, next change, holidays, calendarStatus
```

**Indian Market — Historical**
```
GET  /v1/india/historical                   → OHLCV (params: symbol, exchange, interval, from, to; max 10K records)
GET  /v1/india/historical/status           → timeframe coverage, gap summary, provider activity
GET  /v1/india/historical/gaps             → gap records (params: symbol, interval, status, limit)
GET  /v1/india/historical/reconciliation   → reconciliation statistics
```

**Broker Analytics (Angel One SmartAPI-specific)**
```
GET  /v1/india/broker-analytics/pcr                → put/call ratio
GET  /v1/india/broker-analytics/oi-buildup         → OI buildup (long/short buildup, covering, unwinding)
GET  /v1/india/broker-analytics/gainers-losers     → top OI/price gainers & losers
```

**Crypto — Binance**
```
GET  /v1/crypto/klines/{symbol}                    → OHLCV klines (params: interval, from, to)
GET  /v1/crypto/futures/overview                   → mark price, funding, OI, L/S ratio for tracked symbols
GET  /v1/crypto/futures/funding-history/{symbol}   → funding rate history
GET  /v1/crypto/futures/oi-history/{symbol}        → open interest history (params: period)
GET  /v1/crypto/futures/long-short/{symbol}        → long/short ratio
```

**Crypto — Deribit**
```
GET  /v1/crypto/options/{currency}/overview        → OptionsOverview (BTC|ETH|SOL)
GET  /v1/crypto/options/{currency}/book            → book summary
```

**Quality**
```
POST /v1/quality/gate                              → evaluate gate (body: symbol, quoteAgeMs, ...)
GET  /v1/quality/gate/{symbol}                     → current gate state per symbol
```

**Streaming**
```
WS   /v1/stream/ticks                              → WebSocket: subscribe/unsubscribe, heartbeat
GET  /v1/stream/status                             → subscribedSymbols, ticksPublished, brokerConnections
```

**Providers**
```
GET  /v1/providers/health                          → per-provider, per-capability health
```

**Lineage**
```
GET  /v1/lineage/{observationId}                   → lineage record (within 500ms)
GET  /v1/lineage/instrument/{instrumentId}         → most recent N records (N: 1-1000)
GET  /v1/lineage/forensics/{tradeId}               → joined trade+signal+provenance+lineage
```

**Data Parity**
```
GET  /v1/parity/contract                           → DataParityContract object
```

---

## Event Bus

### Redis Streams Topology

| Stream key | Purpose | Retention |
|---|---|---|
| `mds:events:ticks` | Live tick events | 24 hours or until acknowledged |
| `mds:events:candles` | Candle-closed events | 24 hours |
| `mds:events:option-chain` | Option chain snapshot events | 24 hours |
| `mds:events:dataset-ready` | Backfill / gap recovery complete | 24 hours |
| `mds:events:incidents` | DataIncident events | 7 days |
| `mds:events:quality` | Quality state changes | 24 hours |

### Event Types

```python
class EventType(str, Enum):
    QUOTE_UPDATED = "QuoteUpdated"
    CANDLE_CLOSED = "CandleClosed"
    OPTION_CHAIN_UPDATED = "OptionChainUpdated"
    DATASET_READY = "DatasetReady"
    DATA_INCIDENT = "DataIncident"
    QUALITY_DEGRADED = "QualityDegraded"
    QUALITY_RESTORED = "QualityRestored"
    CIRCUIT_OPEN = "CircuitOpen"
    PROVIDER_SWITCH = "ProviderSwitch"
    CONNECTION_FAILED = "ConnectionFailed"
```

**`DatasetReady` event payload**:
```json
{
  "dataType": "HISTORICAL_OHLCV",
  "instrumentId": "NSE:RELIANCE:EQ",
  "exchange": "NSE",
  "intervalStr": "1m",
  "fromTs": 1705300000000,
  "toTs": 1705386400000,
  "rowCount": 375,
  "provider": "angel_one"
}
```

Delivery semantics: at-least-once. Consumers use `isDuplicate` field and sequence numbers for deduplication.

---

## Observability

### Structured Log Fields (structlog)

Every log entry includes at minimum:
```json
{
  "timestamp": "2026-01-15T09:15:00.000Z",
  "level": "INFO",
  "service": "data-service",
  "component": "market_engine",
  "event": "quote_published",
  "instrumentId": "NSE:NIFTY:IDX",
  "provider": "angel_one",
  "durationMs": 12,
  "requestId": "req-uuid"
}
```

Log entries are written within 100ms of the triggering event. Maximum 10KB per entry.

### Prometheus Metrics

| Metric name | Type | Description |
|---|---|---|
| `mds_request_duration_seconds{endpoint, method, status}` | Histogram | Request latency P50/P95/P99 |
| `mds_provider_call_duration_seconds{provider, capability}` | Histogram | Provider call latency |
| `mds_cache_hits_total{level}` | Counter | Cache hits per level (l1/l2/l3) |
| `mds_cache_misses_total{level}` | Counter | Cache misses |
| `mds_circuit_breaker_state{provider, capability}` | Gauge | 0=CLOSED, 1=OPEN, 2=HALF_OPEN |
| `mds_tick_publish_rate` | Gauge | Ticks published per second |
| `mds_gap_count{severity}` | Gauge | Open gaps by severity |
| `mds_quality_score{instrument_id}` | Gauge | Current DataConfidenceScore |
| `mds_quality_score_distribution` | Histogram | Score distribution |
| `mds_duplicate_rate` | Gauge | Duplicate tick rate |

`GET /metrics` responds within 500ms.

### OpenTelemetry Spans

Every provider call, cache lookup, and pipeline step emits a span with:
- `traceId`, `spanId`
- `mds.provider`
- `mds.instrumentId`
- `mds.durationMs`
- `mds.step` (pipeline step name)
- `mds.cacheHit` (bool)

Spans exported within 5 seconds of completion. Exporter configurable via `OTEL_EXPORTER` env var: `jaeger` | `otlp` | `noop`.

### DataIncident Schema

```python
class DataIncident(BaseModel):
    incidentId: str                   # UUID v4
    incidentType: IncidentType        # CIRCUIT_BREAKER_OPEN | OHLC_INVARIANT |
                                      # SEMANTIC_INTEGRITY | GAP_DETECTED |
                                      # GAP_RECOVERY_EXHAUSTED | MAJOR_DISCREPANCY |
                                      # PERSISTENCE_FAILURE | SCHEMA_VALIDATION
    instrumentId: str
    provider: str
    timestamp: str                    # UTC ISO-8601
    severity: IncidentSeverity        # LOW | MEDIUM | HIGH | CRITICAL
    details: dict                     # incident-specific: failedInvariant, measuredValue, etc.
```

### Quality Alerting

- When `DataConfidenceScore` drops below 50 → emit `quality_degraded` event at WARN level with `instrumentId`, `score`, `previousScore`, `blockReasons`
- When score returns to ≥ 50 → emit `quality_restored` with `instrumentId`, `score`, `previousScore`

### Clock Skew

NTP offset sampled every ≤ 30 seconds. When skew exceeds 500ms → emit `clock_skew_warning` with `measuredSkewMs`, `timestamp`; set `clockDegraded: true` in `/v1/health/data`. When returns to ≤ 500ms → set `clockDegraded: false`.

---

## Security

### Secrets Management

- All provider API keys, TOTP seeds, OAuth tokens, WebSocket credentials stored in secrets store (e.g. HashiCorp Vault, AWS Secrets Manager, or environment-injected at deploy time)
- Plaintext credentials never written to disk, application logs, or version-controlled files
- `.env` files containing secrets never committed to version control

### Credential Stripping

Before serialising any API response, strip all fields whose names contain (case-insensitive): `"key"`, `"token"`, `"secret"`, `"password"`, `"credential"`. This applies regardless of originating handler or data source.

### Authentication

- Consumers authenticate via API key or JWT bearer token in `Authorization` header
- Invalid, malformed, or expired credentials → HTTP 401 without disclosing token details or internal state
- Consumer rate limiting: configurable per consumer, maximum 10,000 requests/minute; exceeded → HTTP 429 with `Retry-After` header

### Angel One JWT Rotation

- Rotation at 23:55 IST daily
- On failure: retry up to 3 times at 60-second intervals
- All retries exhausted → raise alert

### Upstox OAuth 401 Handling

- When Upstox returns HTTP 401: immediately attempt OAuth token refresh; retry original request once with new token
- If refresh also fails: mark Upstox provider as unavailable; return error indicating provider authentication failure

### CORS

- Wildcard CORS (`*`) prohibited on production endpoints
- Explicitly allowlisted consumer origins only; configurable via environment variable

### Shell Command Safety

When constructing shell commands or HTTP requests containing provider-supplied values, use parameterised construction only — no string interpolation that could enable injection.

---

## Deployment

### Docker Multi-Stage Build

```dockerfile
# Stage 1: build dependencies
FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: runtime
FROM python:3.11-slim AS runtime
WORKDIR /app
COPY --from=builder /install /usr/local
COPY src/ ./src/
COPY alembic/ ./alembic/
ENV DATA_SERVICE_PORT=8200
EXPOSE 8200
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8200", \
     "--workers", "4", "--loop", "uvloop"]
```

### docker-compose Services

```yaml
services:
  api:
    build: .
    ports: ["8200:8200"]
    depends_on: [redis, postgres]
    environment:
      - REDIS_URL=redis://redis:6379/0
      - DATABASE_URL=postgresql+asyncpg://user:pass@postgres:5432/mds
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8200/v1/health/live"]
      interval: 10s
      timeout: 3s
      retries: 3

  worker:
    build: .
    command: python -m src.worker
    depends_on: [redis, postgres]

  scheduler:
    build: .
    command: python -m src.scheduler
    depends_on: [redis, postgres]
    # Runs: 08:45 IST F&O universe refresh, 23:55 IST Angel One JWT rotation

  redis:
    image: redis:7-alpine
    command: redis-server --save 60 1 --loglevel warning
    volumes: [redis_data:/data]

  postgres:
    image: timescale/timescaledb:latest-pg15
    environment:
      POSTGRES_DB: mds
      POSTGRES_USER: mds_user
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: [pg_data:/var/lib/postgresql/data]
```

### Graceful Shutdown

On `SIGTERM`:
1. Stop accepting new connections
2. Drain all in-flight requests started before the signal (30-second window)
3. Close all provider WebSocket connections (Angel One SmartStream, Upstox Protobuf, Binance)
4. Flush pending cache writes to Redis
5. Flush pending Event Bus publishes
6. If drain period exceeds 30 seconds: terminate remaining requests
7. Exit with code 0

### Configuration via Environment Variables

All tunable parameters are environment variable-driven (no hardcoded values):

| Variable | Default | Description |
|---|---|---|
| `DATA_SERVICE_PORT` | `8200` | HTTP port |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `DATABASE_URL` | _(required)_ | PostgreSQL async connection string |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Failures before opening circuit |
| `CIRCUIT_BREAKER_RECOVERY_WINDOW_SEC` | `60` | Recovery window seconds |
| `CACHE_L1_MAX_ENTRIES` | `10000` | L1 LRU entry limit |
| `PROVIDER_QUEUE_MAX_DEPTH` | `100` | Max queue depth per provider |
| `GAP_RECOVERY_MAX_ATTEMPTS` | `5` | Max gap recovery retries (1–10) |
| `BACKFILL_CHUNK_ANGEL_1M_DAYS` | `30` | Angel One 1m chunk size |
| `BACKFILL_CHUNK_UPSTOX_1M_DAYS` | `7` | Upstox 1m chunk size |
| `CORS_ALLOWED_ORIGINS` | _(required in prod)_ | Comma-separated allowlist |
| `OTEL_EXPORTER` | `noop` | OpenTelemetry exporter |
| `LOG_LEVEL` | `INFO` | Logging level |
| `SECRETS_BACKEND` | `env` | Secrets backend (env \| vault \| aws_secrets) |

---

## Data Parity Contract

The `DataParityContract` asserts that live, paper, replay, and backtest modes all route through the same normalisation and validation pipeline.

```python
class DataParityContract(BaseModel):
    contractVersion: str
    liveDataPath: str       # description of live data pipeline path
    paperDataPath: str      # description of paper trading data path
    replayDataPath: str     # description of replay pipeline path
    backtestDataPath: str   # description of backtest pipeline path
    parityVerified: bool    # false until explicitly verified
    lastVerifiedAt: Optional[str]   # UTC ISO-8601; null if never verified
```

### Look-Ahead Bias Prevention (Backtest)

When serving backtest data, point-in-time correctness is enforced:
- Data served for time `T` must NOT include any record whose `availableAtMs > T`
- Records missing `availableAtMs` or with unparseable values are rejected; logged with record identifier and rejection reason

### Replay Mode

Replay serves historical data at a configurable playback speed (`playbackMultiplier: float > 0`, where `1.0` = real-time, `> 1.0` accelerates). Response envelope is identical to live responses: `dataSourceType: "HISTORICAL"`, `provenance`, quality fields all present.

### Parity Verification

When `DataParityContract` is verified:
- If all modes share the same pipeline → set `parityVerified: true`, record `lastVerifiedAt`
- If any mode diverges → set `parityVerified: false`, do not update `lastVerifiedAt`
- Divergence from parity contract is a critical finding that blocks release

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: OHLCV Candle Invariants

*For any* candle stored or served by the Historical Engine, `high >= max(open, close)`, `low <= min(open, close)`, `volume >= 0`, and all price values `> 0`.

**Validates: Requirements 4.3, 13.8**

### Property 2: Normaliser Round-Trip

*For any* valid raw provider response (JSON, Protobuf, bhavcopy CSV, NSE charting), `parse(serialise(parse(raw))) == parse(raw)`; the canonical representation is stable across serialise/parse cycles.

**Validates: Requirements 4.11, 17.8**

### Property 3: Deribit Instrument Name Round-Trip

*For any* valid Deribit instrument name of the form `{CURRENCY}-{DD}{MON}{YY}-{STRIKE}-{C|P}`, parsing it into canonical fields and re-serialising produces a string with the same `currency`, `strike`, `optionType` (as `C`/`P`), and expiry formatted as `{DD}{MON}{YY}`.

**Validates: Requirements 14.7**

### Property 4: Deduplication Hash Stability

*For any* combination of `(instrumentId, eventTimeMs, source, ltp, volume)`, the deduplication hash `SHA-256(instrumentId:eventTimeMs:source:ltp:volume)[:32]` is always identical — same inputs always produce the same hash.

**Validates: Requirements 3.6, 17.4**

### Property 5: DataConfidenceScore Bounds

*For any* combination of input parameters to the quality engine, the computed `DataConfidenceScore` is always in `[0, 95]` — never negative, never 100, never above 95.

**Validates: Requirements 7.1**

### Property 6: NSE Session Phase Determinism

*For any* IST clock instant, the session phase classifier assigns exactly one phase from `{PRE_OPEN, PRE_OPEN_CALL_AUCTION, REGULAR, POST_MARKET, CLOSED, MUHURAT}` — no instant maps to two phases, and no instant maps to zero phases.

**Validates: Requirements 12.2**

### Property 7: 3m Interval Rejection (Indian Market)

*For any* request to any Indian market endpoint or internal processing function that includes `interval=3m`, the system rejects it with `HTTP 400 / INTERVAL_NOT_SUPPORTED` or raises a `ValueError` — no 3m candle is ever acquired, stored, or returned for Indian market data.

**Validates: Requirements 1.5, 4.2, 10.11, 16.10**

### Property 8: DataQualityGate Closed-Form Correctness

*For any* set of five gate conditions, `signalEngineAllowed` is `true` if and only if all five conditions (`dataFresh ∧ dataComplete ∧ dataTimestampValid ∧ dataProviderHealthy ∧ dataSemanticallyValid`) are `true`.

**Validates: Requirements 7.2**

### Property 9: BLOCKED Score Blocks Signal Engine

*For any* dataset with `DataConfidenceScore < 30`, `signalEngineAllowed` is always `false` — no exception exists.

**Validates: Requirements 7.11**

### Property 10: Cache TTL Monotonicity

*For any* cache entry, the remaining TTL is monotonically non-increasing between reads (time only removes from TTL, never adds, unless a refresh occurs). When a stale-while-revalidate refresh updates an entry, the new TTL is exactly the configured TTL for that data type.

**Validates: Requirements 9.2, 9.6**

### Property 11: Provenance Observation ID Uniqueness

*For any* two market data observations ingested by the platform, their `dataObservationId` (UUID v4) values are distinct.

**Validates: Requirements 8.1**

### Property 12: OI Semantic Integrity

*For any* instrument record flowing through the Normaliser, the `oi` field is never set to the value of `tradedValue`; if the provider does not supply `oi`, then `oi` is `null` and `oiMissing` is `true`.

**Validates: Requirements 3.3, 6.2**

### Property 13: Look-Ahead Bias Prevention

*For any* backtest request for time `T`, no record in the response has `availableAtMs > T`; all returned records satisfy `availableAtMs ≤ T`.

**Validates: Requirements 23.4**

### Property 14: Reconciliation Deviation Classification

*For any* pair of provider values `(A, B)` for the same OHLCV field, `|A − B| / max(|A|, |B|) × 100 ≤ 0.5` implies `CONFIRMED`, `> 0.5` and `≤ 2.0` implies `MINOR_DISCREPANCY`, and `> 2.0` implies `MAJOR_DISCREPANCY`. The classifications are mutually exclusive and exhaustive over all valid positive numeric values.

**Validates: Requirements 10.5, 10.6, 10.7**

---

## Error Handling

### Hierarchy of Error Classes

| Class | Example | Handling |
|---|---|---|
| Provider failure | HTTP 5xx from provider | Increment circuit breaker; try next provider from Capability_Matrix |
| Rate limit (429) | Binance 429 | Do not increment circuit breaker; respect `Retry-After` or 60s default |
| Schema validation | Missing required field | Reject dataset; log `DataIncident`; do not advance pipeline |
| Semantic integrity | `oi = tradedValue` substitution | Reject field; set to `null`; set missing flag; continue processing |
| OHLC invariant | `high < close` | Reject candle; log `DataIncident` |
| Authentication failure | Angel One JWT expired | Rotate token; retry once; mark provider unavailable if refresh fails |
| Cache unavailability | Redis unreachable | Fall back to direct provider call; log `cache_l2_unavailable`; continue serving |
| Persistence failure | PostgreSQL timeout | Retry 3× at 500ms intervals; log persistence failure; discard if all fail |
| Gap recovery exhaustion | All fallback providers fail | Mark gap `EXHAUSTED`; retain for manual review; emit alert |
| WebSocket disconnect | Binance WS drops | Reconnect with exponential backoff; cease publishing for affected symbols during outage |

### Interval Validation

The `3m` interval receives special handling:
- Returns `HTTP 400` with `{"code": "INTERVAL_NOT_SUPPORTED", "message": "interval 3m is permanently unsupported"}` for any Indian market endpoint
- Blocked at acquisition planner, normaliser, persistence, and API handler layers
- Any existing `3m` rows in the database are ignored by all read paths
- New acquisitions raise `ValueError` at the acquisition planner before any I/O occurs

### No Fabrication Rule

The platform never fabricates data fields not actually provided by the upstream source. Absent fields are `null` with appropriate missing flags. This is the highest-priority correctness invariant.

---

## Testing Strategy

### Unit Tests

Unit tests cover specific behaviour, edge cases, and integration points between components. They focus on:
- OHLCV candle invariant enforcement (each invariant separately)
- Session phase boundary conditions (each boundary time ±1 second)
- Null semantics enforcement (OI from tradedValue substitution — must reject)
- Error envelope shape (all error paths return canonical error envelope)
- Freshness threshold edge cases (right at threshold boundary for each tier)
- Circuit breaker state transitions (each transition)
- Cache TTL expiry and stale-while-revalidate trigger
- Credential stripping (fields containing `key`, `token`, `secret`, etc.)
- `3m` interval rejection at each pipeline layer

### Property-Based Tests (Hypothesis)

The property-based testing library for this project is **[Hypothesis](https://hypothesis.readthedocs.io/)** for Python.

Each property test runs a minimum of **100 iterations**. Each test is tagged with a comment:
```python
# Feature: data-service-platform, Property N: <property_text>
```

| Property | Test description | Generators |
|---|---|---|
| Property 1 (OHLC Invariants) | Generate random valid OHLCV candles; verify all invariants hold after pipeline | `st.floats(min_value=1.0)` for prices, random O/H/L/C with valid relationships |
| Property 2 (Normaliser Round-Trip) | Generate random provider response JSON; verify parse→serialise→parse is stable | JSON structures matching provider schemas |
| Property 3 (Deribit Round-Trip) | Generate valid Deribit instrument names; verify parse→serialise round-trip | BTC/ETH/SOL, random valid dates, strikes, C/P |
| Property 4 (Dedup Hash Stability) | Generate random (instrumentId, eventTimeMs, source, ltp, volume) tuples; verify same inputs = same hash | `st.text()`, `st.integers()`, `st.floats()` |
| Property 5 (Score Bounds) | Generate all combinations of gate inputs; verify score always in [0, 95] | `st.booleans()`, `st.floats(min_value=0, max_value=1)` |
| Property 6 (Session Phase Determinism) | Generate random IST instants including boundaries; verify exactly one phase per instant | `st.datetimes()` anchored to IST timezone |
| Property 7 (3m Rejection) | Generate Indian market requests with `interval="3m"` at each pipeline layer; verify rejection | Fixed interval, random other params |
| Property 8 (Gate Closed-Form) | Generate all 32 combinations of 5 boolean gate conditions; verify `signalEngineAllowed` iff all true | `st.booleans()` × 5 |
| Property 9 (BLOCKED) | Generate score inputs producing score < 30; verify `signalEngineAllowed` is always false | Score inputs constrained to yield < 30 |
| Property 10 (Cache TTL) | Simulate cache entry lifecycle; verify TTL is monotonically non-increasing between reads | Time sequences, TTL configurations |
| Property 11 (UUID Uniqueness) | Generate 1000 observations in a single test run; verify all UUIDs distinct | UUID v4 generation |
| Property 12 (OI Integrity) | Generate provider responses with and without OI data; verify substitution never occurs | Provider response generators with/without `oi` field |
| Property 13 (Look-Ahead Bias) | Generate arbitrary backtest request times T and sets of records; verify all returned have `availableAtMs ≤ T` | `st.integers()` for timestamps |
| Property 14 (Reconciliation) | Generate pairs of OHLCV values (A, B); verify deviation classification is correct and exhaustive | `st.floats(min_value=0.01)` pairs |

### Integration Tests

Integration tests use real Redis and PostgreSQL (via Docker in CI). They cover:
- End-to-end pipeline from mock provider response to L3 database persistence
- Circuit breaker state transitions under simulated provider failures
- Cache miss → provider call → cache write → cache hit cycle
- Backfill checkpoint resume after simulated interruption
- WebSocket fanout to multiple concurrent consumer connections
- SIGTERM graceful shutdown (verify in-flight requests complete)

### Test Coverage Targets

- Unit tests: ≥ 90% statement coverage for `core/`, `engines/`, `providers/`
- Property tests: 14 properties × ≥ 100 iterations each
- Integration tests: all critical happy paths and failure modes

