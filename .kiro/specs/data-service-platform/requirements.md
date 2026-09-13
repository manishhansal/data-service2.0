# Requirements Document

## Introduction

DATA-SERVICE 2.0 is a standalone, independently deployable, production-grade Market Data Platform that serves as the **single market-data authority** for AlphaForge and all future consumers. It centralises all external market-data acquisition, normalisation, validation, caching, persistence, and streaming behind a versioned REST/WebSocket API. No consumer may call an external data provider directly; every request for market data must flow through this service.

The platform supports two market verticals:

- **Indian Markets** — NSE equities, F&O (equity and index derivatives), NSE indices, and associated reference data, sourced from Scrapling/NSE, Angel One SmartAPI, Upstox, Jugaad-data, OpenChart, and Yahoo Finance (controlled fallback only).
- **Crypto Markets** — spot, perpetual futures, and dated futures for BTC/ETH/SOL, sourced from Binance REST/WebSocket and Deribit REST.

Correctness takes priority over availability for all trading-critical datasets. The platform must never fabricate data fields not actually provided by the upstream source.

This document was written after thorough inspection of the AlphaForge reference repository at `/Users/manishkumar/Desktop/alpha-forge`, from which actual provider usage, data contracts, quality rules, freshness thresholds, and API shapes were extracted.

---

## Glossary

- **Platform**: DATA-SERVICE 2.0 — the system being built.
- **Consumer**: Any client that calls the Platform API. AlphaForge is the primary consumer.
- **Provider**: An external upstream market-data source (Angel One SmartAPI, Upstox, Jugaad-data, OpenChart, Scrapling/NSE, Binance, Deribit, Yahoo Finance).
- **Provider_Gateway**: The Platform component that manages all outbound provider connections, authentication, rate limiting, and circuit breaking.
- **Capability_Matrix**: The authoritative, dataset-scoped mapping of which Provider can serve which data type for which instrument class and interval.
- **Instrument_Master**: The canonical registry of all tradeable instruments, their exchange tokens, expiry, lot size, tick size, and provider-specific identifiers.
- **Market_Engine**: The component responsible for live/real-time Indian market data acquisition, distribution, and session management.
- **Historical_Engine**: The component responsible for historical OHLCV acquisition, backfill, gap detection, and gap recovery.
- **Streaming_Engine**: The component responsible for WebSocket/streaming connections, tick publishing via the Event Bus, and live crypto data.
- **Normaliser**: The component that maps provider-specific raw responses to canonical schema types.
- **Validation_Pipeline**: The ordered sequence of checks each dataset traverses: schema validation → normalisation → timestamp normalisation → semantic validation → duplicate detection → gap detection → freshness validation → cross-provider reconciliation → quality scoring.
- **Quality_Engine**: The component that computes the DataConfidenceScore (0–95) and DataQualityGate for each dataset.
- **Data_Provenance**: The immutable record of where a datum came from, when it was observed, received, and made available, with what normalisaton version.
- **Cache**: The three-level caching subsystem — L1 (in-process LRU), L2 (Redis), L3 (PostgreSQL/TimescaleDB read path).
- **Event_Bus**: The internal message bus (Redis Streams or NATS) over which the Platform publishes ticks and dataset-ready events to consumers.
- **Canonical_Timeframes**: The nine supported intervals: `1m`, `5m`, `10m`, `15m`, `30m`, `1h`, `1d`, `1w`, `1M`. The `3m` interval is permanently unsupported at every layer.
- **Instrument_Type**: One of: `EQ` (equity), `FUTIDX` (index future), `FUTSTK` (stock future), `OPTIDX` (index option), `OPTSTK` (stock option), `ETF`, `IDX` (index reference).
- **Session_Phase**: One of: `PRE_OPEN`, `PRE_OPEN_CALL_AUCTION`, `REGULAR`, `POST_MARKET`, `CLOSED`, `MUHURAT`.
- **DataQualityGate**: A five-condition signal-safety contract: `dataFresh ∧ dataComplete ∧ dataTimestampValid ∧ dataProviderHealthy ∧ dataSemanticallyValid`.
- **DataConfidenceScore**: An integer in `[0, 95]` computed from freshness (35%), completeness (25%), provider health (20%), timestamp validity (10%), and cross-source agreement (10%).
- **DataSourceType**: One of: `LIVE`, `HISTORICAL`, `DERIVED`, `CACHED`. Every dataset response must declare its source type explicitly.
- **OI**: Open interest — the number of outstanding derivative contracts. Semantically distinct from `tradedValue` and must never be mapped from it.
- **ALPHAFORGE_DATA_REQUIREMENTS**: The companion document (`ALPHAFORGE_DATA_REQUIREMENTS.md`) enumerating every AlphaForge feature's data dependencies extracted from the reference repository.

---

## Requirements

### Requirement 1: Single Market-Data Authority

**User Story:** As an AlphaForge developer, I want all market data to flow through DATA-SERVICE 2.0 so that no consumer bypasses validation, provenance tracking, or quality checks by calling providers directly.

#### Acceptance Criteria

1. THE Platform SHALL expose all market data exclusively through REST API endpoints prefixed with `/v1/` and WebSocket endpoints; no market data endpoint SHALL exist outside the `/v1/` path prefix.
2. THE Platform SHALL reject, at build and CI time, any code within its repository that imports or calls an external provider SDK directly from outside the Provider_Gateway component; a CI pipeline violation SHALL cause the pipeline to exit with a non-zero status code and SHALL report each violation with the file path and line number.
3. WHEN a Consumer requests market data, THE Platform SHALL return a response envelope that includes `dataSourceType` (one of `LIVE`, `HISTORICAL`, `DERIVED`, `CACHED`), `provenance` (object identifying the provider and the UTC ISO-8601 ingestion timestamp), `quality` (a numeric score in the range 0.00–1.00), and `freshness` (a non-negative integer representing the age of the data in milliseconds) alongside the data payload.
4. THE Platform SHALL produce a machine-readable import boundary contract artifact; WHEN a consumer codebase references this artifact in a static analysis tool, any import or call to an external provider API from outside the Provider_Gateway component SHALL cause the static analysis tool to report a failure.
5. IF a Consumer requests the `3m` interval, THEN THE Platform SHALL return HTTP 400 with error code `INTERVAL_NOT_SUPPORTED` and the message `"interval 3m is permanently unsupported"`.
6. IF a Consumer requests an interval value that is not one of the Canonical_Timeframes and is not `3m`, THEN THE Platform SHALL return HTTP 400 with error code `INTERVAL_NOT_SUPPORTED` and a message identifying the unsupported interval value.

---

### Requirement 2: Indian Market Instrument Coverage

**User Story:** As a signal engineer, I want the Platform to cover all NSE equity, F&O, index, and derivative instruments so that every strategy can access the instruments it needs.

#### Acceptance Criteria

1. THE Instrument_Master SHALL maintain canonical records for NSE equities (series EQ, BE), NSE indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, INDIAVIX, and all active NSE indices), all equities designated as F&O-eligible by NSE as of the snapshot's `effectiveFrom` date, index futures, index options, stock futures, and stock options.
2. THE Instrument_Master SHALL store the following fields per instrument: `instrumentId` (canonical internal ID), `tradingSymbol`, `displaySymbol`, `exchange` (NSE/NFO/BSE/BFO), `segment` (EQ/FO/CD/COM), `instrumentType`, `underlying`, `expiry` (ISO-8601 UTC date), `strike`, `optionType` (CE/PE or null), `lotSize`, `tickSize`, `isin`, and provider-specific token mappings for each connected Provider.
3. THE Instrument_Master SHALL track `activeFrom` and `activeTo` validity dates per instrument to support point-in-time historical accuracy.
4. WHEN an instrument is added, removed, or modified per an NSE official circular or F&O inclusion/exclusion notification, THE Instrument_Master SHALL record the lifecycle event with a `snapshotVersion`, `checksum`, and `effectiveFrom` timestamp within 24 hours of the notification's published timestamp.
5. THE Platform SHALL expose `GET /v1/instruments` that accepts filters for `exchange`, `instrumentType`, `underlying`, `segment`, and `expiry` and returns matching Instrument records; IF no instruments match the filters, THEN THE Platform SHALL return an empty list with HTTP 200.
6. THE Platform SHALL expose `GET /v1/instruments/fno-universe` that returns the current F&O-eligible universe with `universeVersion`, `effectiveFrom`, `effectiveTo`, `fnoEquityCount`, `fnoIndexCount`, and a cryptographic `checksum`.
7. THE Instrument_Master SHALL resolve provider-specific tokens from a canonical `instrumentId` without including the provider-specific token in the consumer-facing API response by default.
8. WHERE a Consumer includes `?include=providerTokens` in a request to an instrument lookup endpoint, THE Instrument_Master SHALL include provider-specific token fields in the response.
9. IF the `GET /v1/instruments/fno-universe` endpoint is called and no F&O universe snapshot has been successfully loaded, THEN THE Platform SHALL return HTTP 503 with error code `FNO_UNIVERSE_UNAVAILABLE` and a message indicating the snapshot has not been initialised.

---

### Requirement 3: Indian Market Live Data

**User Story:** As a scalping strategy, I want low-latency live quotes, ticks, and option chain snapshots for Indian instruments so that I can generate intraday signals with fresh data.

#### Acceptance Criteria

1. WHEN a provider quote update event is received during the NSE `REGULAR` session, THE Market_Engine SHALL publish the normalised live quote for the corresponding instrument within 500 milliseconds of the event timestamp.
2. THE Market_Engine SHALL provide the following fields per live quote: `instrumentId`, `symbol`, `exchange`, `ltp`, `open`, `high`, `low`, `prevClose`, `change`, `changePct`, `volume`, `oi` (F&O only, null for equities), `tradedValue`, `totalBuyQty`, `totalSellQty`, `upperCircuit`, `lowerCircuit`, `weekHigh52`, `weekLow52`, `lastTradeTime`, `bid` (where available), `ask` (where available), and full `provenance`.
3. THE Market_Engine SHALL never populate the `oi` field with `tradedValue` data; `oi` represents open interest in contracts and `tradedValue` represents total traded value in INR; these fields are semantically distinct and THE Market_Engine SHALL return `null` rather than fabricate a value.
4. IF the NSE market session is `CLOSED` and a live quote is requested, THEN THE Market_Engine SHALL return the last available quote with `marketStatus: "CLOSED"` and SHALL NOT classify this as a provider failure or increment circuit-breaker failure counts.
5. THE Streaming_Engine SHALL publish live ticks to the Event_Bus on channel pattern `mds:ticks:{symbol}` with fields: `tickId`, `instrumentId`, `symbol`, `exchange`, `eventTimeMs` (UTC milliseconds), `receivedAtMs`, `ltp`, `change`, `changePct`, `volume`, `oi` (F&O only), `tradedValue`, `source`, `quality`, and `isDuplicate`; the publish latency from `receivedAtMs` to Event_Bus delivery SHALL be ≤ 200ms at p99.
6. THE Streaming_Engine SHALL deduplicate ticks using a deterministic deduplication ID computed from `instrumentId:eventTimeMs:source:ltp:volume`; duplicate ticks SHALL be published with `isDuplicate: true` rather than silently dropped, to preserve auditability.
7. WHEN an NSE option chain snapshot is available from the provider, THE Market_Engine SHALL acquire and publish it within 5 seconds; per-contract fields SHALL include: `strike`, `optionType`, `ltp`, `bid`, `ask`, `oi`, `oiChange`, `volume`, `tradedValue`, `iv` (null when unavailable — zero IV is never a substitute), `delta`, `gamma`, `theta`, `vega`, `rho`, and completeness metadata (`oiMissing`, `oiChangeMissing`, `volumeMissing`, `ivMissing` flags).
8. WHEN the underlying spot price used in an option chain snapshot is older than 60 seconds, THE Market_Engine SHALL set `chainQuality: "DEGRADED"` and SHALL include `spotAgeMs` in the response metadata.
9. THE Market_Engine SHALL expose `GET /v1/india/option-chain` accepting `underlying` and `expiry` parameters; IF the market is closed, THEN THE endpoint SHALL return `rows: []` with `marketStatus: "CLOSED"` and SHALL NOT return HTTP 5xx; IF no contracts are listed for the given underlying and expiry, THEN THE endpoint SHALL return `rows: []` with `marketStatus: "NO_DATA"`.
10. WHEN an option chain snapshot is acquired, THE Market_Engine SHALL compute option chain analytics: `pcrOi`, `pcrVolume`, `maxCeOiStrike`, `maxPeOiStrike`, `totalCeOi`, `totalPeOi`, `atmIv`, and `maxPain`, and SHALL tag each computed field with a `MetricTag` (`OBSERVED`, `DERIVED`, or `MODELLED`) to declare its provenance class.

---

### Requirement 4: Indian Market Historical OHLCV

**User Story:** As a backtesting engine, I want deep, validated, gap-free historical OHLCV data for all Canonical_Timeframes so that strategy simulations use the same data path as live trading.

#### Acceptance Criteria

1. THE Historical_Engine SHALL acquire and store OHLCV candle data for NSE equities, indices, and F&O instruments for the following Canonical_Timeframes: `1m`, `5m`, `10m`, `15m`, `30m`, `1h`, `1d`, `1w`, `1M`; minimum history depth SHALL be 60 calendar days for `1m`, 180 calendar days for `5m`–`30m`, 365 calendar days for `1h`, and 10 years for `1d`–`1M`.
2. IF a request includes `interval=3m`, THEN THE Historical_Engine SHALL return HTTP 400 with error code `INTERVAL_NOT_SUPPORTED`; the `3m` interval SHALL be blocked at every processing layer including the acquisition planner, normaliser, persistence layer, and API handler.
3. THE Historical_Engine SHALL validate each OHLCV candle against invariants before persistence: `high >= max(open, close)`, `low <= min(open, close)`, `volume >= 0`, and all price values `> 0`; candles failing these invariants SHALL be rejected and SHALL generate a DataIncident record containing `instrumentId`, `intervalStr`, `timestamp`, `failedInvariant`, `rejectedValues`, and `detectedAt`.
4. IF the source does not supply a volume value for a candle, THEN THE Historical_Engine SHALL set `volumeUnavailable: true` and store the numeric `volume` field as `0` to distinguish it from a genuine zero-volume bar.
5. FOR ALL historical candles, THE Historical_Engine SHALL record `provenance` including: `provider`, `sourceType` (`BROKER_AUTHENTICATED` or `OPEN_SOURCE_NSE_DERIVED`), `sourceTimestamp`, `receivedAt`, `datasetVersion` (monotonically increasing integer), `normalisationVersion` (monotonically increasing integer), and `sessionDate` (IST trading day as `YYYY-MM-DD`).
6. THE Historical_Engine SHALL enforce dataset-based provider routing per the Capability_Matrix: Angel One SmartAPI is the primary source for multi-day intraday equity history (`1m`–`1h`); Upstox V3 is the primary source for index intraday history and for intervals not natively supported by Angel One; OpenChart is the open-source supplement for all Canonical_Timeframes; Jugaad-data is the primary source for F&O EOD history with OI.
7. THE Historical_Engine SHALL detect gaps in acquired candle sequences and SHALL persist gap records with: `instrumentId`, `exchange`, `intervalStr`, `gapStart`, `gapEnd`, `durationSec`, `recoveryStatus`, `recoveryAttempts`, and `expectedProvider`.
8. WHEN a gap is detected, THE Historical_Engine SHALL schedule an automated gap-recovery attempt using the Capability_Matrix to select the appropriate fallback provider; THE Historical_Engine SHALL attempt recovery up to a maximum of 5 times (configurable between 1 and 10) before marking the gap as `EXHAUSTED`; gaps with status `EXHAUSTED` SHALL be retained for manual review and SHALL NOT be automatically retried further.
9. THE Historical_Engine SHALL expose `GET /v1/india/historical` accepting `symbol`, `exchange`, `interval`, `from` (ISO-8601), and `to` (ISO-8601) parameters and SHALL return the response envelope with `data: OHLCVCandle[]` (maximum 10,000 records per response with `metadata.truncated: true` when the limit is reached), `metadata.provider`, `metadata.provenance`, `metadata.quality`, `metadata.gaps`, and `metadata.dataAsOf`.
10. THE Historical_Engine SHALL support `GET /v1/india/historical/status` returning timeframe coverage statistics, gap summary, provider activity, reconciliation status, and the list of supported timeframes.
11. WHEN the Normaliser processes a provider response, THE Normaliser SHALL produce the same canonical output when parsing a provider response, serialising it, and parsing the serialised form again; this round-trip property SHALL be verified by the test suite for each provider adapter.

---

### Requirement 5: Provider Gateway and Capability Routing

**User Story:** As an operator, I want all external provider calls to go through a single gateway that enforces capability-based routing, rate limiting, authentication, and circuit breaking so that provider failures are isolated and the system degrades gracefully.

#### Acceptance Criteria

1. THE Provider_Gateway SHALL maintain the Capability_Matrix as a single source of truth declaring, per provider and per interval: `supported` (bool), `history` (bool), `live` (bool), `maxChunkDays` (int), and `requestsPerSecond` (float).
2. WHEN per-provider rate limit capacity is available, THE Provider_Gateway SHALL queue incoming requests using a token-bucket algorithm and dispatch them as capacity permits.
3. IF the per-provider request queue depth exceeds the configured maximum (default 100, valid range 1–10,000), THEN THE Provider_Gateway SHALL reject the request immediately with error code `PROVIDER_QUEUE_FULL`.
4. THE Provider_Gateway SHALL implement circuit breakers per provider per capability (live quotes, historical, option chain, WebSocket); a circuit opens after a configurable failure threshold (valid range 1–100 failures) and transitions to `HALF_OPEN` after a configurable recovery window (valid range 1–3600 seconds); in `HALF_OPEN` state, exactly one probe request is dispatched — success transitions the circuit to `CLOSED`, failure re-opens it.
5. WHEN a provider returns HTTP 429 (rate limit) and the response includes a `Retry-After` header, THE Provider_Gateway SHALL wait for the indicated duration before retrying and SHALL NOT increment the circuit-breaker failure counter.
6. IF a provider returns HTTP 429 and no `Retry-After` header is present, THEN THE Provider_Gateway SHALL apply a default backoff of 60 seconds and SHALL NOT increment the circuit-breaker failure counter.
7. WHEN a provider returns `MARKET_CLOSED` semantics (empty data during closed hours), THE Provider_Gateway SHALL NOT classify this as a provider failure and SHALL NOT increment circuit-breaker failure counters.
8. WHEN a provider returns `UNSUPPORTED_CAPABILITY` semantics, THE Provider_Gateway SHALL NOT classify this as a provider failure and SHALL NOT increment circuit-breaker failure counters.
9. THE Provider_Gateway SHALL support the following providers with authenticated and credential-free variants: Angel One SmartAPI (TOTP + JWT, live + historical), Upstox V2/V3 (OAuth token, live + historical), Scrapling/NSE (credential-free, live + instrument master), Jugaad-data (credential-free, historical F&O EOD), OpenChart (credential-free, historical OHLCV), Yahoo Finance (credential-free, controlled fallback only).
10. WHEN a provider switch from primary to fallback occurs, THE Provider_Gateway SHALL log the event with fields: `fromProvider`, `toProvider`, `reason`, `dataset`, `instrumentId`, and `timestamp`.
11. THE Provider_Gateway SHALL expose `GET /v1/providers/health` returning per-provider, per-capability health: `status` (UP/DOWN/DEGRADED/UNKNOWN), `circuitState` (CLOSED/OPEN/HALF_OPEN), `availability` (float 0.0–1.0), `latencyP50Ms` (int, milliseconds), `latencyP99Ms` (int, milliseconds), `errorRate` (float 0.0–1.0), `lastSuccessAt`, `lastFailureReason`, and `semanticIntegrity` (bool, true when the provider's last response passed all semantic validation checks).
12. WHERE Yahoo Finance is configured as a fallback, THE Provider_Gateway SHALL restrict Yahoo Finance to equity EOD historical data only and SHALL enforce a `SECONDARY_FALLBACK` provenance tag and a maximum quality grade of `B` on all Yahoo-sourced data.

---

### Requirement 6: Data Normalisation and Semantic Integrity

**User Story:** As a data consumer, I want every dataset to use a canonical schema with guaranteed semantic correctness so that I never receive fabricated or misrepresented fields.

#### Acceptance Criteria

1. THE Normaliser SHALL map all provider-specific raw responses to canonical Pydantic schema types; no provider-specific type SHALL be exposed in any consumer-facing API response.
2. THE Normaliser SHALL enforce semantic integrity: IF the provider does not supply an `oi` (open interest in contracts) value, THEN THE Normaliser SHALL set `oi: null` and `oiMissing: true` rather than substituting a fabricated value; the `oi` field SHALL never be populated from a provider's `tradedValue` field.
3. THE Normaliser SHALL enforce that `volume` represents traded quantity (number of shares or contracts) and `tradedValue` represents total traded value in INR; these fields SHALL never be used interchangeably.
4. THE Normaliser SHALL enforce that option `iv` (implied volatility) is `null` when not provided by the source; zero IV is not a valid substitute for a missing IV value.
5. THE Normaliser SHALL enforce that option Greeks (`delta`, `gamma`, `theta`, `vega`, `rho`) are `null` when not provided by the source; placeholder zeros are prohibited.
6. THE Normaliser SHALL enforce that all `bid` and `ask` prices are `null` when not actually provided by the source; placeholder zeros are prohibited.
7. THE Normaliser SHALL normalise all internal timestamps to UTC epoch milliseconds for storage and processing.
8. THE Normaliser SHALL format all API response timestamps as UTC ISO-8601 strings with `Z` suffix; IST interpretation SHALL only occur inside the Market_Engine's session-management component.
9. FOR ALL canonicalised datasets, THE Normaliser SHALL attach a `normalisationVersion` string in semver format (e.g., `"1.0.0"`) to the provenance record so that datasets can be re-normalised if the schema changes.
10. THE Normaliser SHALL tag each field in the canonical dataset with a `MetricTag`: `OBSERVED` (directly from exchange feed), `DERIVED` (computed from observed values), or `MODELLED` (from a pricing model); WHERE a field's MetricTag cannot be determined, THE Normaliser SHALL assign `OBSERVED` as the default.
11. IF a provider response passes initial schema validation but contains a mix of valid and invalid fields, THEN THE Normaliser SHALL process the valid fields, set invalid fields to `null` with their corresponding missing flags, and continue processing rather than dropping the entire response.
12. WHEN a provider response fails schema validation, THE Normaliser SHALL log a structured error with `provider`, `endpoint`, `rawResponseHash`, `validationErrors`, and `receivedAt`; the response SHALL be dropped rather than passed to consumers.

---

### Requirement 7: Data Quality Engine

**User Story:** As a signal engine, I want a data quality gate that blocks signal generation when data is stale, incomplete, or semantically invalid, so that no trade decision is made on bad data.

#### Acceptance Criteria

1. THE Quality_Engine SHALL compute a `DataConfidenceScore` in `[0, 95]` for each dataset using five weighted components: freshness (35%), completeness (25%), provider health (20%), timestamp validity (10%), and cross-source agreement (10%); the score SHALL never reach 100 to reflect inherent market-data uncertainty.
2. THE Quality_Engine SHALL evaluate the `DataQualityGate` with five boolean conditions: `dataFresh`, `dataComplete`, `dataTimestampValid`, `dataProviderHealthy`, `dataSemanticallyValid`; `signalEngineAllowed` SHALL be `true` if and only if all five conditions are `true`.
3. THE Quality_Engine SHALL classify data freshness per instrument tier during market hours: index instruments with `eventTimeMs` within 10 seconds are `FRESH`; F&O liquid instruments within 10 seconds are `FRESH`; F&O normal instruments within 15 seconds are `FRESH`; equity instruments within 30 seconds are `FRESH`.
4. IF the current market session phase is not `REGULAR`, THEN THE Quality_Engine SHALL apply extended freshness windows: index instruments within 60 seconds are `FRESH`; F&O liquid instruments within 60 seconds are `FRESH`; F&O normal instruments within 90 seconds are `FRESH`; equity instruments within 120 seconds are `FRESH`.
5. THE Quality_Engine SHALL classify dataset quality as `VALID` (score ≥ 80 and fresh and complete), `DEGRADED` (score 50–79), `PARTIAL` (`completenessPercent` in [40, 99] with at least one required field missing), `STALE` (data outside freshness window), `INVALID` (fundamental integrity failure), or `UNKNOWN` (cannot be determined).
6. THE Quality_Engine SHALL expose `POST /v1/quality/gate` accepting `symbol`, `quoteAgeMs`, `completenessPercent`, `timestampValid`, `crossSourceAgreement`, `sequenceIntegrity`, and optional strategy-specific overrides; IF any required field is missing or out of range, THEN THE Platform SHALL return HTTP 400 with a message identifying the invalid field; the response SHALL include `signalEngineAllowed`, `confidenceScore`, `quality`, `gates` (each condition with boolean value), `blockReasons`, and `circuitBreakers`.
7. THE Quality_Engine SHALL expose `GET /v1/quality/gate/{symbol}` for dashboard monitoring of current gate state per symbol.
8. THE Quality_Engine SHALL support strategy-specific data requirements: each strategy SHALL declare `maxQuoteAgeMs`, `minConfidenceScore` (minimum value 30, to prevent relaxing the global BLOCKED threshold), `requiresOI`, `requiresOptionChain`, and `requiresConfirmedCandle`; THE Quality_Engine SHALL apply these strategy-specific thresholds in addition to the global gate but SHALL NOT allow strategy overrides to relax any global gate condition.
9. WHEN `signalEngineAllowed` is `false`, THE Quality_Engine SHALL include at least one `blockReason` string explaining which gate condition failed and the measured value that caused the failure.
10. WHEN option chain data is evaluated, THE Quality_Engine SHALL detect and reject records with crossed markets (`bid > ask`), negative IV, negative OI, or invalid Greeks (`|delta| > 1`, `gamma < 0`); detected violations SHALL generate a `DataIncident` record containing `symbol`, `violationType`, `measuredValue`, and `detectionTimestamp`.
11. THE Quality_Engine SHALL ensure that a dataset with `DataConfidenceScore < 30` (grade `BLOCKED`) SHALL set `signalEngineAllowed: false` with no exceptions.

---

### Requirement 8: Data Provenance and Lineage Tracking

**User Story:** As an auditor or trade forensics investigator, I want every market observation to carry an immutable provenance record so that I can reconstruct exactly what data drove any signal or trade decision.

#### Acceptance Criteria

1. THE Platform SHALL assign a unique `dataObservationId` (UUID v4) to every market data observation at the moment of ingestion from the provider.
2. FOR ALL market data observations, THE Platform SHALL record a `DataProvenance` object containing: `dataObservationId`, `source` (DataSource enum), `sourceVersion`, `eventTimeMs` (exchange event time in UTC ms), `receivedAtMs`, `availableAtMs`, `normalisationVersion`, `validationApplied`, `isFallback`, and `fallbackReason`.
3. THE Platform SHALL maintain a lineage store that retains the most recent 100,000 observation records in memory (evicted in insertion order when the limit is reached) and persists all records to the database indefinitely until explicitly deleted by an authorised administrative operation.
4. THE Platform SHALL expose `GET /v1/lineage/{observationId}` returning the full lineage record for a single observation within 500ms; IF no record exists for the given `observationId`, THEN THE Platform SHALL return HTTP 404 with a message indicating the observation was not found.
5. THE Platform SHALL expose `GET /v1/lineage/instrument/{instrumentId}?limit=N` returning the most recent N lineage records for an instrument ordered by `receivedAtMs` descending, where N must be between 1 and 1,000 inclusive; IF N is absent or outside the range 1–1,000, THEN THE Platform SHALL return HTTP 400 with a message indicating the valid range; responses SHALL include `storeSize` (current in-memory record count) and `totalRecorded` (cumulative count since platform start).
6. THE Platform SHALL persist provenance records to the database within 1,000ms of successful data acquisition; IF the persistence operation fails, THEN THE Platform SHALL retry up to 3 times at 500ms intervals before logging a persistence failure and discarding the record; persisted fields SHALL include: `datasetKey`, `instrumentId`, `exchange`, `intervalStr`, `sessionDate`, `provider`, `sourceType`, `authenticated`, `fetchedAt`, `sourceTimestamp`, `dataAsOf`, `responseHash`, `fromTs`, `toTs`, `datasetVersion`, `dataTrustStatus`, and `rowCount`.
7. WHEN a paper trade or signal is generated, THE Platform SHALL record `dataObservationId`, `quoteAgeAtEntryMs`, `dataConfidenceAtEntry`, `dataQualityAtEntry`, `dataProviderAtEntry`, `dataIsFallback`, and `observationEventTime` in the trade record so that any future forensics audit can reconstruct the full data→signal→trade chain.
8. WHEN `GET /v1/lineage/forensics/{tradeId}` is requested, THE Platform SHALL return a joined response within 1,000ms comprising the trade record, signal record, provenance record, and lineage entry; IF any constituent record is missing, THEN THE Platform SHALL return HTTP 404 with a message identifying which record (trade, signal, provenance, or lineage) could not be resolved.
9. THE Platform SHALL treat all provenance fields in API responses as read-only; any request that attempts to modify a provenance record SHALL be rejected with HTTP 405 and the existing record SHALL remain unchanged.

---

### Requirement 9: Multi-Level Cache

**User Story:** As a high-frequency consumer, I want the Platform to serve frequently-requested datasets from cache with sub-millisecond L1 latency so that repeated requests do not overload external providers.

#### Acceptance Criteria

1. THE Cache SHALL implement three levels in lookup order L1 → L2 → L3: L1 (in-process LRU cache, maximum 10,000 entries, access latency ≤ 1ms at p99), L2 (Redis, access latency ≤ 5ms at p99), L3 (PostgreSQL/TimescaleDB, persistent read path); a cache miss at all three levels triggers a live provider call.
2. THE Cache SHALL use the following TTLs for L2 Redis keys: single live quote — 3 seconds; batch live quotes — 3 seconds; intraday candles — 30 seconds; daily+ candles — 4 hours; option chain snapshot — 15 seconds; instrument master — 12 hours; provider health — 5 seconds.
3. THE Cache SHALL namespace all L2 Redis keys under the `mds:` prefix with the pattern: `mds:{dataType}:{provider}:{exchange}:{symbol}:{interval}:{from}:{to}`; WHERE any field is not applicable, THE Cache SHALL substitute `_` as the placeholder for that field.
4. WHEN a dataset is served from L1 or L2 cache, THE Cache SHALL set `dataSourceType: "CACHED"` in the response and SHALL include the original live provider identifier in `provenance.sourceChain[0]`.
5. WHEN multiple concurrent requests for the identical dataset key arrive while a provider call is already in-flight, THE Cache SHALL deduplicate them into a single provider call and deliver the response to all waiting requests; IF the in-flight provider call fails, THEN THE Cache SHALL return an error response to all waiting requests.
6. WHEN a cache entry's remaining TTL is within 20% of its configured TTL, THE Cache SHALL return the cached data immediately and initiate a single background refresh; IF a background refresh is already in progress for that key, THEN THE Cache SHALL NOT initiate a second refresh.
7. WHEN the L2 Redis cache is unavailable, THE Cache SHALL fall back to direct provider calls and SHALL log a structured warning identified as `cache_l2_unavailable` but SHALL NOT return an error to the consumer.
8. IF the L1 cache entry count reaches the maximum configured capacity, THEN THE Cache SHALL evict the least-recently-used entry before inserting the new entry.
9. WHEN a live provider call returns a result, THE Cache SHALL write the result to L2 and L1 before returning the response to the consumer.

---

### Requirement 10: Historical Engine — Backfill and Gap Recovery

**User Story:** As an operator, I want the Historical Engine to continuously close data gaps in the candle database so that the signal engine always has the depth it needs.

#### Acceptance Criteria

1. THE Historical_Engine SHALL persist a backfill checkpoint to Redis after each successfully stored chunk; the checkpoint SHALL record the UTC timestamp of the last persisted candle for the key pattern `mds:backfill:checkpoint:{symbol}:{exchange}:{interval}` with no TTL (persistent).
2. WHEN a backfill job is interrupted (due to process termination, unhandled exception, or provider timeout exceeding 30 seconds), THE Historical_Engine SHALL resume from the last successfully persisted checkpoint rather than restarting from scratch.
3. THE Historical_Engine SHALL route backfill requests per the Capability_Matrix: for equity instruments covering more than 1 calendar day at intraday intervals, Angel One SmartAPI SHALL be the primary source; index intraday history SHALL route to Upstox V3; daily EOD for F&O with OI SHALL use Jugaad-data; OpenChart SHALL be used for reconciliation and as a fallback.
4. THE Historical_Engine SHALL chunk backfill requests to respect per-provider `maxChunkDays` constraints: Angel One 1m — 30 calendar days; Angel One 5m/15m — 90 calendar days; Upstox 1m — 7 calendar days; Upstox 5m/15m — 30 calendar days; Upstox 1d — 365 calendar days.
5. WHEN two providers return OHLCV data for the same `(instrumentId, exchange, interval, time)` tuple and all OHLCV field deviations are ≤ 0.5% (using formula `|A − B| / max(|A|, |B|) × 100`), THE Historical_Engine SHALL set reconciliation status to `CONFIRMED`.
6. IF any OHLCV field deviation for the same tuple is between 0.5% and 2.0%, THEN THE Historical_Engine SHALL set reconciliation status to `MINOR_DISCREPANCY`.
7. IF any OHLCV field deviation for the same tuple exceeds 2.0%, THEN THE Historical_Engine SHALL set reconciliation status to `MAJOR_DISCREPANCY`; the overall tuple status SHALL be determined by the worst-case field deviation.
8. THE Historical_Engine SHALL expose `GET /v1/india/historical/gaps` accepting `symbol`, `interval`, `status` (PENDING/RECOVERED/FAILED), and `limit` (default 100, maximum 1,000) parameters; responses SHALL include gap records with `gapStart`, `gapEnd`, `durationSec`, `recoveryStatus`, `recoveryAttempts`, `expectedProvider`, and `recoveryProvider`.
9. THE Historical_Engine SHALL expose `GET /v1/india/historical/reconciliation` returning reconciliation statistics: `totalCompared`, `matched`, `matchRatePct`, `distribution`, and `byProviderPair` breakdown.
10. IF a gap recovery attempt fails after the configured maximum retry count (default 3, valid range 1–10), THEN THE Historical_Engine SHALL mark the gap as `RECOVERY_EXHAUSTED`, generate a `DataIncident` record with fields `incidentId`, `symbol`, `exchange`, `interval`, `gapStart`, `gapEnd`, and `exhaustedAt`, and alert via the observability pipeline.
11. THE Historical_Engine SHALL enforce that `3m` interval data is never acquired, stored, or returned; any existing `3m` rows in the database SHALL be ignored by all read paths, and new acquisitions SHALL raise a `ValueError` at the acquisition planner layer.

---

### Requirement 11: Instrument Master Lifecycle Management

**User Story:** As a strategy developer, I want the Instrument Master to track NSE F&O universe changes so that strategies always operate on the current eligible instrument set.

#### Acceptance Criteria

1. WHEN the current IST time reaches 08:45 IST on a trading day, THE Instrument_Master SHALL refresh the full NSE F&O universe and produce a new snapshot with `snapshotVersion`, `checksum`, `generatedAt`, `constituentCount`, `fnoEquityCount`, and `fnoIndexCount`.
2. WHEN consecutive snapshots are compared and a change is detected, THE Instrument_Master SHALL produce a `lifecycleEvent` record with `symbol`, `changeType` (ADDED/REMOVED/SUSPENDED), `effectiveDate`, and `source` for each changed instrument.
3. WHEN the upstream source returns an F&O universe whose checksum matches the current snapshot, THE Instrument_Master SHALL not write a new snapshot or lifecycle events.
4. THE Instrument_Master SHALL store provider-specific token mappings for each connected Provider: `angelToken`, `angelSymbol`, `upstoxKey`, `upstoxSymbol`, enabling the Provider_Gateway to resolve canonical `instrumentId` to provider tokens without consumer involvement.
5. THE Instrument_Master SHALL expose `GET /v1/instruments/fno-universe/history` returning past snapshots (maximum 100 per page) with filters for `status` (ACTIVE/ADDED/REMOVED) and `version`, enabling consumers to query the universe as it was on any past date.
6. WHEN a derivative instrument's expiry date is reached, THE Instrument_Master SHALL update its `activeTo` field to the expiry date by 09:15 IST on that date and SHALL not remove the record, preserving historical point-in-time accuracy.
7. IF the NSE upstream source is unavailable at the 08:45 IST refresh time, THEN THE Instrument_Master SHALL retain the most recently successful snapshot, continue using it for the trading session, and emit a structured warning log entry indicating the failed refresh attempt.

---

### Requirement 12: NSE Market Session Management

**User Story:** As the Market Engine, I want accurate NSE session state so that data requests are handled correctly during all session phases and holidays.

#### Acceptance Criteria

1. THE Market_Engine SHALL determine the current NSE session phase using IST (UTC+5:30) as the reference timezone; NSE holidays, half-days, and special sessions (including Muhurat trading) SHALL each map to a defined session phase as specified in criterion 2.
2. THE Market_Engine SHALL classify every IST clock instant into exactly one of the following NSE session phases, where each phase boundary is inclusive at the start time and exclusive at the end time: `PRE_OPEN` (09:00–09:08), `PRE_OPEN_CALL_AUCTION` (09:08–09:15), `REGULAR` (09:15–15:30), `POST_MARKET` (15:30–16:00), and `CLOSED` (all other times including full NSE holidays); on NSE-designated half-trading days, the `REGULAR` phase SHALL end at 13:00 and `POST_MARKET` SHALL span 13:00–13:30.
3. WHEN the calendar year changes, THE Market_Engine SHALL refresh the NSE holiday calendar by fetching the updated holiday list from the NSE official holiday API before the first trading session of the new calendar year begins.
4. IF the NSE official holiday API is unavailable when a calendar refresh is attempted, THEN THE Market_Engine SHALL retain the most recently successfully fetched holiday calendar and expose a `calendarStatus` field in `GET /v1/india/market/status` indicating the date of the last successful refresh.
5. WHEN a market data request arrives during `CLOSED`, `PRE_OPEN`, or `POST_MARKET` session phases, THE Market_Engine SHALL return the most recent available data from the last completed `REGULAR` session (data no older than 24 hours) with `marketStatus` reflecting the actual current phase and SHALL NOT return HTTP 5xx.
6. THE Market_Engine SHALL expose `GET /v1/india/market/status` returning: `sessionPhase`, `nextSessionChange` (UTC ISO-8601 datetime), `tradingDay` (bool), `nextTradingDay` (IST date as `YYYY-MM-DD`), `holidays` (remaining NSE holiday dates within the current IST calendar month as `YYYY-MM-DD` list), and `calendarStatus` (date of last successful holiday calendar refresh as `YYYY-MM-DD`).

---

### Requirement 13: Crypto Market Data — Binance

**User Story:** As a crypto signal engine, I want live and historical Binance spot and futures data so that crypto trading strategies have the same quality guarantees as Indian market strategies.

#### Acceptance Criteria

1. THE Platform SHALL acquire Binance spot OHLCV (klines) for tracked symbols (BTC, ETH, SOL) supporting intervals: `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `8h`, `12h`, `1d`; the `3m` interval restriction applies only to Indian market data — Binance natively supports `3m` for crypto.
2. THE Platform SHALL acquire Binance USDⓈ-M perpetual futures data for tracked symbols, including: `markPrice`, `indexPrice`, `fundingRate`, `fundingRateAnnualized`, `nextFundingTime`, `openInterest` (in contracts), and `openInterestNotionalUsd`.
3. THE Platform SHALL acquire Binance open interest history for perpetual futures with the following periods: `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `12h`, `1d`; responses SHALL include `ts`, `openInterest`, and `notionalUsd`.
4. THE Platform SHALL acquire Binance long/short account ratio data for tracked symbols using the global account ratio endpoint; responses SHALL include `ts`, `longShortRatio`, `longAccount`, and `shortAccount`.
5. THE Streaming_Engine SHALL maintain a Binance WebSocket connection for live mini-ticker data for all tracked symbols; the connection SHALL implement exponential backoff with jitter on disconnect (base 1s, max 30s) and heartbeat pings every 30 seconds.
6. WHEN the Binance WebSocket connection transitions to `error` or `closed` state, THE Streaming_Engine SHALL log the event with `symbol`, `reason`, `reconnectAttempt`, and SHALL NOT expose the raw WebSocket error message to consumers.
7. THE Platform SHALL expose `GET /v1/crypto/futures/overview` returning `markPrice`, `fundingRate`, `fundingRateAnnualized`, `nextFundingTime`, `openInterest`, `openInterestNotionalUsd`, `oiChangePct1h`, `longShortRatio`, `longAccount`, and `shortAccount` for all tracked symbols.
8. THE Platform SHALL validate all Binance candle data against OHLC invariants before persistence and SHALL store `openTime` (UTC epoch ms), `open`, `high`, `low`, `close`, `volume` (base asset), and `closeTime` in the canonical `OHLCVCandle` schema.
9. IF a Binance REST API request fails after 3 consecutive attempts, THEN THE Platform SHALL discard the request, emit an error event to the Event_Bus indicating the affected symbol and data type, and preserve previously stored data unchanged.
10. IF OHLC invariant validation fails for a Binance candle record, THEN THE Platform SHALL reject the record, log the violation with `symbol`, `interval`, and `openTime`, and SHALL NOT persist the invalid record.
11. WHEN the Binance WebSocket connection is established or re-established, THE Streaming_Engine SHALL publish received mini-ticker ticks to the Event_Bus within 2 seconds of connection confirmation.

---

### Requirement 14: Crypto Market Data — Deribit Options

**User Story:** As an options analytics feature, I want Deribit crypto options data for BTC/ETH/SOL so that the platform can serve mark IV, OI, and max pain analytics for crypto options.

#### Acceptance Criteria

1. THE Platform SHALL acquire Deribit options book summary data for BTC, ETH, and SOL, including per-instrument: `instrumentName`, `markPrice`, `markIv`, `openInterest`, `volume`, `volumeUsd`, `underlyingPrice`, `last`, `bid`, `ask`.
2. THE Platform SHALL acquire Deribit index prices for BTC, ETH, and SOL via the `get_index_price` endpoint; responses SHALL include the `index_price` as a float.
3. THE Normaliser SHALL parse Deribit instrument names of the form `{CURRENCY}-{DD}{MON}{YY}-{STRIKE}-{C|P}` into canonical fields: `baseCurrency`, `expiryTs` (UTC epoch ms, 08:00 UTC on expiry date), `strike`, and `optionType` (`C` mapped to `CE`, `P` mapped to `PE`); instruments that fail parsing SHALL be silently dropped with a logged warning.
4. THE Platform SHALL compute `OptionsOverview` for each tracked crypto currency: `underlyingPrice`, `totalCallOi`, `totalPutOi`, `totalCallVolume`, `totalPutVolume`, `pcrOi` (null when total call OI is zero or null), `pcrVolume` (null when total call volume is zero or null), `atmIv` (IV of the contract with strike closest to `underlyingPrice` on the nearest expiry), and per-expiry `ExpiryStats` including `maxPainStrike` (strike minimising aggregate OI monetary loss), `pcrOi`, `pcrVolume`, `topStrikes` (top 5 by combined OI), and `daysToExpiry` (whole calendar days, rounded down).
5. WHEN Deribit returns `null` for `mark_iv`, `open_interest`, `volume`, or `underlying_price`, THE Normaliser SHALL preserve `null` in the canonical schema and SHALL NOT substitute zero.
6. WHEN `GET /v1/crypto/options/{currency}/overview` is requested for a supported currency (`BTC`, `ETH`, or `SOL`), THE Platform SHALL return the `OptionsOverview` with full `provenance` and `quality` metadata; IF the `currency` path parameter is not one of the supported values, THEN THE Platform SHALL return HTTP 400 with an error message listing the supported currencies.
7. FOR ALL Deribit data, THE Platform SHALL perform round-trip validation: parsing the raw Deribit instrument name then re-serialising the canonical fields SHALL produce a string with the same `currency`, `strike`, `optionType` (as `C`/`P`), and expiry formatted as `{DD}{MON}{YY}` (day zero-padded to 2 digits, month as 3-letter uppercase abbreviation); this round-trip property SHALL be verified by the test suite for each supported currency.
8. THE Platform SHALL refresh Deribit options book summary data at intervals not exceeding 60 seconds; IF a refresh attempt fails, THE Platform SHALL retain the most recently successful data and emit a structured warning log entry.

---

### Requirement 15: Event Bus and Internal Streaming

**User Story:** As a consumer service, I want to subscribe to a real-time event stream of market ticks and dataset-ready events so that I receive data as soon as it is available without polling.

#### Acceptance Criteria

1. THE Event_Bus SHALL support durable, ordered message delivery for tick streams with at-least-once delivery semantics and sequence numbers for deduplication; messages SHALL be retained for a minimum of 24 hours or until acknowledged, whichever comes first.
2. THE Streaming_Engine SHALL publish live ticks to the Event_Bus at p99 latency ≤ 500ms from receiving them from the upstream provider WebSocket connection.
3. WHEN a backfill chunk, option chain snapshot, or gap recovery completes, THE Streaming_Engine SHALL publish a `dataset_ready` event to the Event_Bus including `dataType`, `instrumentId`, `exchange`, `intervalStr`, `fromTs`, `toTs`, `rowCount`, and `provider`.
4. THE Event_Bus SHALL support at least 500 concurrently subscribed symbol tick streams; IF a new subscription request would exceed the configured maximum, THEN THE Platform SHALL return an error indicating the subscription limit has been reached.
5. THE Platform SHALL expose a WebSocket endpoint `WS /v1/stream/ticks` that accepts subscribe and unsubscribe control messages and relays Event_Bus ticks to connected consumers; the server SHALL send heartbeat messages every 10 seconds.
6. WHEN a consumer WebSocket connection closes, THE Platform SHALL unsubscribe the connection from all its symbol streams within 5 seconds without leaking resources, verified by a decrement in the `subscribedSymbols` count in `GET /v1/stream/status`.
7. THE Platform SHALL expose `GET /v1/stream/status` returning `subscribedSymbols`, `ticksPublished`, `validationFailures` (by type), `lastPublishedAt`, and `brokerConnections` (Angel One SmartStream and Upstox V3 Protobuf connection state).
8. IF a consumer WebSocket connection does not respond to 3 consecutive heartbeat messages within a 30-second window, THEN THE Platform SHALL close the connection and unsubscribe it from all symbol streams.
9. WHEN the upstream provider WebSocket connection is lost, THE Streaming_Engine SHALL cease publishing ticks for affected symbols and SHALL log a structured warning identifying the affected provider and symbols until the connection is re-established.

---

### Requirement 16: REST API Design

**User Story:** As a consumer developer, I want a consistent, versioned REST API with predictable response envelopes, error formats, and status codes so that integration is straightforward and reliable.

#### Acceptance Criteria

1. THE Platform SHALL prefix all REST API endpoints with `/v1/`; a breaking change (removal or renaming of an endpoint, parameter, or response field) SHALL require a new major version prefix (e.g., `/v2/`).
2. THE Platform SHALL use a canonical success response envelope for all successful responses: `{ "data": <payload>, "metadata": { "requestedAt", "dataAsOf", "dataSourceType", "provider", "provenance", "marketStatus", "quality" } }`.
3. THE Platform SHALL use a canonical error response for all error responses: `{ "error": { "code": <ErrorCode>, "message": <string>, "provider": <ProviderId | null>, "retryAfterMs": <int | null>, "requestId": <non-empty string> } }` with no stack traces, no credentials, and no internal file paths.
4. IF a Consumer request is invalid (unsupported interval, missing required parameter, malformed date), THEN THE Platform SHALL return HTTP 400; IF a Consumer request is unauthenticated, THEN HTTP 401; IF authenticated but unauthorised, THEN HTTP 403; IF the resource is not found, THEN HTTP 404; IF rate limit is exceeded, THEN HTTP 429; IF provider failures exhaust all fallbacks, THEN HTTP 502; IF the platform is unavailable, THEN HTTP 503.
5. THE Platform SHALL implement `GET /v1/health/live` returning `{ status: "alive", version, uptimeMs, timestamp }` always with HTTP 200 for liveness probes.
6. WHEN `GET /v1/health/ready` is called and all critical dependencies (Redis and the database) respond within 2,000ms, THE Platform SHALL return HTTP 200; IF Redis or the database does not respond within 2,000ms, THEN THE Platform SHALL return HTTP 503 with a `capabilities` map identifying the unavailable dependency.
7. THE Platform SHALL implement `GET /v1/health/data` returning current market session state, quote freshness statistics (P50ms, P99ms, successRate), gap counts, duplicate rates, circuit-breaker states, and clock-skew measurement.
8. THE Platform SHALL remove all fields whose names contain "key", "token", "secret", "password", or "credential" (case-insensitive) before serialising any API response.
9. IF the request is for a live quote, THE Platform SHALL include `Cache-Control: public, s-maxage=3, stale-while-revalidate=5`; IF for intraday candles, `public, s-maxage=30, stale-while-revalidate=60`; IF for daily candles, `public, s-maxage=300`; IF for option chain, `public, s-maxage=15, stale-while-revalidate=20`; IF the response is an error, `no-store`.
10. IF an API endpoint accepts an `interval` parameter and the value is `3m`, THEN THE Platform SHALL return HTTP 400 with the message `"interval 3m is permanently unsupported"`.

---

### Requirement 17: Data Pipeline Integrity

**User Story:** As a data engineer, I want every dataset to traverse a defined pipeline of validation, normalisation, and quality checks before being served or persisted, so that consumers never receive raw, unvalidated provider data.

#### Acceptance Criteria

1. THE Platform SHALL route every dataset through the Validation_Pipeline in the following order: (1) raw response receipt, (2) JSON schema validation, (3) Normaliser → canonical schema, (4) timestamp normalisation to UTC, (5) semantic validation (OI/volume/IV/bid-ask integrity), (6) duplicate detection, (7) gap detection, (8) freshness classification, (9) cross-provider reconciliation, (10) Quality_Engine scoring, (11) canonical dataset output, (12) Cache population, (13) persistence, (14) API/Event_Bus delivery; a dataset that fails at any step SHALL NOT advance to subsequent steps.
2. THE Platform SHALL enforce that no step in the Validation_Pipeline may be skipped; a configuration flag that disables a pipeline step SHALL require an explicit `OVERRIDE_REASON` audit entry of 1–500 characters and SHALL generate a high-severity alert within 5 seconds.
3. THE Platform SHALL perform timestamp normalisation at step 4: all provider-supplied timestamps SHALL be converted to UTC epoch milliseconds using the configured exchange timezone (IST for Indian markets, UTC for crypto); timestamps that cannot be normalised SHALL result in the dataset being rejected with a `DataIncident` record containing the raw timestamp value, the source identifier, and the reason for rejection.
4. THE Validation_Pipeline SHALL detect and flag duplicate observations using a deterministic hash of `instrumentId:eventTimeMs:source:ltp:volume`; a dataset whose hash matches any hash recorded within the preceding 24-hour rolling window SHALL be classified as a duplicate, SHALL NOT be persisted, and SHALL be counted in quality statistics.
5. THE Validation_Pipeline SHALL detect gaps in candle sequences by comparing the received `time` values against the expected sequence for the given interval; gaps SHALL generate `DataGapEvent` records containing `gapStartMs`, `gapEndMs`, `expectedCount`, `actualCount`, and `severity` (`LOW` when `expectedCount − actualCount` is 1, `MEDIUM` when 2–5, `HIGH` when > 5).
6. IF a dataset fails JSON schema validation at step 2, THEN THE Platform SHALL reject the dataset, SHALL NOT advance it beyond step 2, and SHALL record a `DataIncident` entry containing the source identifier, the failing field path, and the validation error reason.
7. WHEN the Quality_Engine scores a dataset at step 10 and assigns a score below 60, THE Platform SHALL flag the dataset with status `POOR_QUALITY`; `POOR_QUALITY` datasets SHALL NOT be delivered via API/Event_Bus but SHALL be persisted with the `POOR_QUALITY` flag for audit purposes.
8. THE Validation_Pipeline SHALL enforce a round-trip property for all parsers and serialisers: `parse(serialise(parse(raw))) == parse(raw)` for all valid inputs; this property SHALL be verified by the test suite for each parser (JSON, Protobuf, bhavcopy CSV, NSE charting response).

---

### Requirement 18: Observability

**User Story:** As an operator, I want structured logs, metrics, and traces so that I can diagnose issues, monitor SLOs, and be alerted when the platform degrades.

#### Acceptance Criteria

1. THE Platform SHALL emit structured JSON logs for every significant event; log fields SHALL include `timestamp` (UTC ISO-8601), `level`, `component`, `event`, `instrumentId` (where applicable), `provider` (where applicable), `durationMs`, and `requestId`; log entries SHALL be written within 100ms of the triggering event and SHALL NOT exceed 10KB per entry.
2. THE Platform SHALL expose Prometheus-compatible metrics at `GET /metrics` including: request latency histograms (P50, P95, P99) per endpoint; provider call latency histograms per provider per capability; cache hit/miss rates per cache level; circuit-breaker state gauges per provider; tick publication rate; gap count by severity; quality score distribution; the endpoint SHALL respond within 500ms.
3. THE Platform SHALL implement distributed tracing with OpenTelemetry; every provider call, cache lookup, and pipeline step SHALL emit a trace span with `traceId`, `spanId`, `provider`, `instrumentId`, and `durationMs`; spans SHALL be exported within 5 seconds of completion.
4. THE Platform SHALL expose `GET /v1/monitoring/stats` returning rolling 3600-second statistics per endpoint: P50ms, P99ms, successRate (float 0.0–1.0), and requestCount (integer ≥ 0); the endpoint SHALL respond within 500ms.
5. WHEN any circuit breaker transitions to `OPEN` state, THE Platform SHALL emit a `circuit_open` structured log event at `WARN` level containing `provider`, `capability`, `failureRate` (float 0.0–1.0), and `transitionTimestamp` (UTC ISO-8601) within 1 second of the transition.
6. WHEN `DataConfidenceScore` drops below 50 for any active instrument, THE Platform SHALL emit a `quality_degraded` alert event containing `instrumentId`, `score`, `previousScore`, and `blockReasons` (list of one or more reason strings); IF the score subsequently rises to 50 or above, THEN THE Platform SHALL emit a `quality_restored` event containing `instrumentId`, `score`, and `previousScore`.
7. THE Platform SHALL record `DataIncident` events for: circuit breaker opens, OHLC invariant violations, semantic integrity violations, gap detection, gap-recovery exhaustion, and cross-provider MAJOR_DISCREPANCY findings; each record SHALL include `incidentId`, `incidentType`, `instrumentId`, `provider`, `timestamp` (UTC ISO-8601), and `severity` (LOW/MEDIUM/HIGH/CRITICAL).
8. THE Platform SHALL sample NTP clock offset at intervals not exceeding 30 seconds; WHEN the skew exceeds 500ms, THE Platform SHALL emit a `clock_skew_warning` event containing `measuredSkewMs` and `timestamp`, and SHALL set `clockDegraded: true` in `GET /v1/health/data` responses; IF skew returns to ≤ 500ms, THEN THE Platform SHALL set `clockDegraded: false`.
9. IF `GET /metrics`, `GET /v1/monitoring/stats`, or `GET /v1/health/data` does not respond within 2,000ms, THEN THE Platform SHALL emit an `ERROR` level log event indicating the affected endpoint and the failure reason.

---

### Requirement 19: Security

**User Story:** As a security engineer, I want all provider credentials, consumer authentication, and internal secrets to be managed securely so that no credential is exposed in logs, responses, or error messages.

#### Acceptance Criteria

1. THE Platform SHALL store all provider API keys, TOTP seeds, OAuth tokens, and WebSocket credentials in a secrets store that provides at-rest encryption, access-controlled retrieval, and audit logging; plaintext credentials SHALL never be written to disk, application logs, or environment variable files committed to version control.
2. WHEN a request arrives at any production API endpoint without a valid API key or JWT bearer token, or with a malformed or expired JWT, THE Platform SHALL reject the request and return HTTP 401 with an error message indicating authentication failure, without disclosing token details or internal state.
3. THE Platform SHALL implement consumer rate limiting at the API gateway level; limits SHALL be configurable per consumer identifier with a maximum of 10,000 requests per minute per consumer; IF a consumer exceeds the configured limit, THEN THE Platform SHALL return HTTP 429 with a `Retry-After` header indicating seconds until reset.
4. THE Platform SHALL remove all fields whose names contain "key", "token", "secret", "password", or "credential" (case-insensitive) before serialising every API response, regardless of the originating handler or data source.
5. WHEN constructing shell commands or HTTP requests that include provider-supplied values (symbol names, date strings, query parameters), THE Platform SHALL use parameterised construction and SHALL not use string interpolation that could allow injection.
6. THE Platform SHALL restrict CORS to explicitly allowlisted consumer origins; wildcard CORS (`*`) is prohibited on production endpoints.
7. THE Platform SHALL rotate Angel One JWT tokens at 23:55 IST daily; IF the rotation attempt fails, THEN THE Platform SHALL retry up to 3 times at 60-second intervals and SHALL raise an alert if all retries are exhausted.
8. WHEN an Upstox API call returns HTTP 401 from the provider, THE Platform SHALL immediately attempt to refresh the Upstox OAuth token and retry the original request once with the new token; IF the refresh also fails, THEN THE Platform SHALL mark the Upstox provider as unavailable and return an error indicating provider authentication failure to the caller.

---

### Requirement 20: Horizontal Scalability and Deployment

**User Story:** As an infrastructure engineer, I want DATA-SERVICE 2.0 to be independently deployable, containerised, and horizontally scalable so that it can handle increasing consumer load without architecture changes.

#### Acceptance Criteria

1. THE Platform SHALL be packaged as a standalone Docker image with a documented `Dockerfile` and `docker-compose.yml` for local development; the image SHALL expose port `8200` by default.
2. THE Platform SHALL be stateless at the application layer: all shared state (cache, locks, backfill checkpoints, circuit-breaker state) SHALL reside in Redis or PostgreSQL, not in process memory, so that multiple instances may run concurrently.
3. WHEN a request is received at `GET /v1/health/live`, THE Platform SHALL return `{ status: "alive", version, uptimeMs, timestamp }` with HTTP 200 within 200ms regardless of provider or dependency state; the handler SHALL NOT block on any external dependency.
4. WHEN `GET /v1/health/ready` is called and both Redis and the database respond within 2,000ms, THE Platform SHALL return HTTP 200; IF either Redis or the database does not respond within 2,000ms, THEN THE Platform SHALL return HTTP 503 with a `capabilities` map identifying the unavailable dependency.
5. WHEN a `SIGTERM` signal is received, THE Platform SHALL stop accepting new connections, drain all in-flight requests that started before the signal within 30 seconds, close all provider WebSocket connections, flush pending cache writes, and exit; IF the drain period exceeds 30 seconds, THE Platform SHALL terminate remaining requests and exit with code 0.
6. THE Platform SHALL support TimescaleDB hypertable partitioning on the `candle_bar` table with `chunk_time_interval = '1 day'`; the table SHALL be designed for compatibility so that promotion from plain PostgreSQL to TimescaleDB requires only a single DDL command without data migration.
7. THE Platform SHALL expose all tunable parameters (provider rate limits, cache TTLs, circuit-breaker thresholds, freshness windows) via environment variables listed in the repository's environment variable reference documentation with their default values; no tunable parameter SHALL be hardcoded.

---

### Requirement 21: AlphaForge Data Requirements Matrix

**User Story:** As the AlphaForge product team, I want a companion `ALPHAFORGE_DATA_REQUIREMENTS.md` document that maps every AlphaForge feature to its data dependencies so that the Platform API is scoped to exactly what is needed.

#### Acceptance Criteria

1. WHEN the DATA-SERVICE 2.0 specification is finalised, THE Platform team SHALL produce an `ALPHAFORGE_DATA_REQUIREMENTS.md` document by inspecting the AlphaForge reference repository at `/Users/manishkumar/Desktop/alpha-forge`; the document SHALL enumerate the following AlphaForge consumers and their data dependencies:

   | Consumer Feature | Data Types | Instruments | Providers Used (Current) | Canonical Interval |
   |---|---|---|---|---|
   | India Scalping | LTP, OI, Option Chain, Ticks | NIFTY, BANKNIFTY, F&O equities | scrapling, angel_one, upstox | 1m, 5m |
   | India Daily Picks | OHLCV, OI, Option Chain, Greeks, Sentiment | F&O universe + indices | scrapling, angel_one | 1d, 5m, 15m |
   | India Expiry Trades | Option Chain, OI, IV, Greeks | Index derivatives (NIFTY, BANKNIFTY, FINNIFTY) | scrapling, angel_one | 5m, 15m |
   | India Options Workbench | Option Chain, OI, IV, Greeks, bid/ask, PCR | Index + equity options | scrapling, angel_one, upstox | live |
   | India F&O Trend History | OHLCV with OI, EOD | All F&O underlyings | jugaad, scrapling | 1d |
   | India Best Time | OHLCV, Volume, Session stats | F&O equities + indices | angel_one, upstox | 5m, 1h |
   | India Paper Trading | LTP, OI, ticks, signal gate | Signal-specific | scrapling, angel_one | 1m, 5m |
   | Crypto Futures Overview | Mark price, Funding rate, OI, L/S ratio | BTC, ETH, SOL | Binance FAPI | live |
   | Crypto Options | Mark IV, OI, Volume, Max Pain | BTC, ETH, SOL | Deribit REST | live |
   | Crypto Signals | OHLCV klines, Funding rate, OI, L/S, Fear&Greed | BTC, ETH, SOL | Binance, Deribit, AltMe | 1h, 1d |
   | Strategy Lab Backtest | OHLCV klines | BTC, ETH, SOL | Binance REST | 1h, 4h, 1d |
   | Market Heatmap | Live quotes, changePct, volume | NSE equities | scrapling, angel_one | live |
   | AI Signals India | OI buildup, PCR, gainers/losers, Option Chain | F&O universe | angel_one (SmartAPI-specific) | live |
   | Signal Quality Gate | DataQualityGate, DataConfidenceScore | All active instruments | Quality_Engine | live |

2. THE `ALPHAFORGE_DATA_REQUIREMENTS.md` SHALL document the following currently-direct provider calls in AlphaForge that MUST migrate to the Platform: `src/services/india/angelone/derivatives.ts` (PCR, OI buildup, gainers/losers), `src/features/india/scanner/engine.ts`, `src/features/india/daily-picks/builder.ts`, `src/features/india/expiry-trades/builder.ts`, `src/features/ai-signals/india-builder.ts`; for each file, the document SHALL record the current provider, the data type accessed, and the replacement Platform endpoint.
3. THE Platform SHALL provide `GET /v1/india/broker-analytics/pcr`, `GET /v1/india/broker-analytics/oi-buildup`, and `GET /v1/india/broker-analytics/gainers-losers` endpoints sourced from Angel One SmartAPI; each endpoint SHALL return the data payload with `provenance` and `quality` metadata.
4. IF an Angel One SmartAPI call fails when serving a broker-analytics endpoint, THEN THE Platform SHALL return HTTP 502 with error code `PROVIDER_UNAVAILABLE` and SHALL NOT return stale data without explicit `stale: true` metadata.

---

### Requirement 22: Technology Stack

**User Story:** As the engineering team, I want the Platform to use a documented, consistent technology stack so that all components are built with compatible and production-proven tools.

#### Acceptance Criteria

1. THE Platform SHALL use Python 3.11+ with FastAPI and Uvicorn as the HTTP framework; all async I/O SHALL use `asyncio` with `httpx` or `aiohttp` for outbound HTTP requests.
2. THE Platform SHALL use Pydantic v2 for all schema definitions, serialisation, validation, and settings management; every canonical data type SHALL be a Pydantic `BaseModel` with explicit field types and validators.
3. THE Platform SHALL use Redis (version 7+) for L2 cache, Event_Bus (Redis Streams), and distributed state (backfill checkpoints, circuit-breaker counters); the Redis client SHALL be `redis[hiredis]` with async support.
4. THE Platform SHALL use PostgreSQL (version 15+) as the persistence layer, with TimescaleDB as an optional extension for time-series optimisation on the `candle_bar` table; async database access SHALL use an async driver compatible with the ORM/query builder chosen.
5. WHERE batch ingestion or analytical queries over large OHLCV datasets are required, THE Platform SHALL use Polars or PyArrow for in-process data transformation, rather than pandas, to reduce memory overhead and increase throughput.
6. THE Platform SHALL use `structlog` for structured JSON logging; all log output SHALL be machine-parseable JSON in production mode, and each log entry SHALL include at minimum a timestamp (ISO 8601), log level, service name, and correlation ID.
7. THE Platform SHALL use OpenTelemetry SDK for distributed tracing; trace exports SHALL be configurable for Jaeger, OTLP, or a no-op exporter for local development; the active exporter SHALL be selectable via environment configuration without code changes.

---

### Requirement 23: Data Parity Contract

**User Story:** As a quant researcher, I want the Platform to guarantee that live trading, paper trading, replay, and backtest modes all use the same data path so that backtests are not biased by data unavailable in live trading.

#### Acceptance Criteria

1. THE Platform SHALL implement a `DataParityContract` that asserts live, paper, replay, and backtest modes all route through the same normalisation and validation pipeline; divergence from this contract SHALL be a critical finding that blocks release.
2. THE Platform SHALL expose `GET /v1/parity/contract` returning the `DataParityContract` object containing: `contractVersion`, `liveDataPath`, `paperDataPath`, `replayDataPath`, `backtestDataPath`, `parityVerified` (boolean), and `lastVerifiedAt` (ISO 8601 timestamp); IF the parity contract has never been verified, THEN `parityVerified` SHALL be `false` and `lastVerifiedAt` SHALL be `null`.
3. THE Platform SHALL support a replay mode that serves historical data at a configurable playback speed (a positive numeric multiplier where 1.0 equals real-time, values > 1.0 accelerate playback) while emitting the same response envelope — including `dataSourceType: "HISTORICAL"`, `provenance`, and quality fields — as live responses.
4. WHEN serving backtest data, THE Platform SHALL enforce point-in-time correctness: data served for time `T` SHALL NOT include any record whose `availableAtMs` field value is greater than `T`.
5. IF a record is missing the `availableAtMs` field or its value cannot be parsed as a valid UTC millisecond-epoch timestamp, THEN THE Platform SHALL reject that record from the backtest dataset and emit a log entry indicating the record identifier and the rejection reason.
6. WHEN the `DataParityContract` is verified, THE Platform SHALL record the verification timestamp in `lastVerifiedAt` and set `parityVerified` to `true`; IF any mode's data path diverges from the shared normalisation and validation pipeline at verification time, THEN THE Platform SHALL set `parityVerified` to `false` and SHALL NOT update `lastVerifiedAt`.
