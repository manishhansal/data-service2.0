# INDIAN MARKET SCHEMA REDESIGN
**Version:** 2.0.0  
**Date:** 2026-09-15  
**Status:** APPROVED FOR IMPLEMENTATION  
**Alembic revision:** b1c2d3e4f5a6 (follows a1b2c3d4e5f6)

---

## 1. EXECUTIVE SUMMARY

The existing `candle_bar` table is a single flat table that merges equities, indices, crypto, and (in future) F&O data without any semantic separation. The instrument_master is empty. There are no live tick tables, no option chain tables, no calendar tables, no ingestion tracking tables, and no TimescaleDB hypertables.

This redesign introduces **16 new tables** organized into 6 logical layers:

```
LAYER 1 — INSTRUMENT IDENTITY
  instrument_master            (enhanced — already exists, now populated)
  instrument_provider_mapping  (NEW — separates provider tokens from canonical identity)
  instrument_identity_history  (NEW — symbol/token change audit trail)

LAYER 2 — CANONICAL CANDLES
  equity_candle                (NEW — NSE equity + index OHLCV, TimescaleDB hypertable)
  futures_candle               (NEW — NSE F&O futures OHLCV+OI, TimescaleDB hypertable)
  options_candle               (NEW — NSE F&O options OHLCV+OI, TimescaleDB hypertable)

LAYER 3 — LIVE DATA
  market_tick                  (NEW — live ticks from WebSocket, TimescaleDB hypertable)
  market_quote                 (NEW — live quote snapshots, TimescaleDB hypertable)

LAYER 4 — OPTION CHAIN
  option_chain_snapshot        (NEW — point-in-time chain header)
  option_chain_contract        (NEW — per-strike chain rows)
  option_greeks_snapshot       (NEW — IV + Greeks time-series, TimescaleDB hypertable)

LAYER 5 — CALENDAR + SESSIONS
  exchange_calendar            (NEW — NSE/BSE holiday + trading day registry)
  market_session               (NEW — actual session open/close records)
  fno_universe_membership      (NEW — point-in-time F&O eligibility per instrument)

LAYER 6 — OPERATIONS
  ingestion_job                (NEW — backfill/ingestion job tracking)
  ingestion_checkpoint         (NEW — resumable job state per symbol+interval)
  candle_bar_quarantine        (NEW — migration quarantine for unclassifiable rows)

EXISTING — RETAINED AS-IS
  candle_bar                   (DEPRECATED after migration — kept as archive)
  fno_universe_snapshot        (RETAINED — point-in-time universe snapshots)
  data_gap                     (RETAINED — gap detection records)
  data_incident                (RETAINED — integrity incident log)
  data_provenance              (RETAINED — full lineage records)
  provider_health              (RETAINED — provider health time-series)
```

---

## 2. DESIGN PRINCIPLES

### 2.1 Semantic Separation
Every instrument type has its own canonical candle table. Equities, futures, and options have fundamentally different semantics (OI meaning, expiry, strike, lot size) and must not be merged.

### 2.2 Instrument Identity Is Central
`candle_bar` currently stores `instrument_id` as a string with no FK to `instrument_master` (which is empty anyway). The new design requires `instrument_master` to be populated and all candle tables to reference it as the canonical source of instrument identity.

### 2.3 Provider Tokens Are Separate
Provider-specific tokens (`angel_token`, `upstox_key`) belong in `instrument_provider_mapping`, not in the canonical `instrument_master`. The master table describes what an instrument IS; the mapping table describes how each provider refers to it.

### 2.4 TimescaleDB for Write-Heavy Time-Series
Tables with high write rates and time-range query patterns are promoted to TimescaleDB hypertables. Tables with low write rates (calendar, sessions, ingestion jobs) remain as standard PostgreSQL tables.

### 2.5 Provenance on Every Canonical Record
Every candle row carries `provenance_id` (FK to `data_provenance`) and `data_origin` (PROVIDER vs DERIVED). This is required for ML dataset reproducibility.

### 2.6 Data Quality States Are Standardized
All quality fields use the same enum vocabulary:
```
TRUSTED | DEGRADED | POOR_QUALITY | BLOCKED | MISSING | QUARANTINED | DERIVED
```

### 2.7 3m Is Permanently Banned
All candle tables carry a `CHECK (interval_str <> '3m')` constraint as defense-in-depth.

### 2.8 Idempotent Upserts
All candle tables have a deterministic unique key enabling `ON CONFLICT DO UPDATE` upserts.

---

## 3. TABLE SPECIFICATIONS

### 3.1 LAYER 1 — INSTRUMENT IDENTITY

#### instrument_master (ENHANCED)
The existing table schema is correct. The missing piece is population.  
New: Add `instrument_class` column (EQ | IDX | FUT | OPT | CRYPTO) for fast classification.  
New: Add `name` column for display name independent of trading symbol.  
No structural changes break existing code; all additions are nullable.

```sql
-- New columns added in migration b1c2d3e4f5a6:
instrument_class  VARCHAR(8)   -- EQ | IDX | FUT | OPT | CRYPTO | ETF
name              VARCHAR(256) -- Full display name
```

#### instrument_provider_mapping (NEW)
```sql
id                BIGSERIAL PRIMARY KEY
instrument_id     VARCHAR(64)  NOT NULL REFERENCES instrument_master(instrument_id)
provider          VARCHAR(32)  NOT NULL
provider_instrument_id  VARCHAR(128) -- provider's native ID/token
provider_symbol   VARCHAR(128) -- provider's trading symbol
exchange_segment  VARCHAR(32)  -- provider-specific segment string
valid_from        DATE         NOT NULL
valid_to          DATE         -- NULL = currently active
is_active         BOOLEAN      NOT NULL DEFAULT TRUE
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()

UNIQUE (instrument_id, provider, valid_from)
INDEX: (provider, provider_instrument_id) -- reverse lookup
INDEX: (instrument_id, provider) WHERE is_active = TRUE
```

#### instrument_identity_history (NEW)
```sql
id                BIGSERIAL PRIMARY KEY
instrument_id     VARCHAR(64)  NOT NULL REFERENCES instrument_master(instrument_id)
old_symbol        VARCHAR(64)
new_symbol        VARCHAR(64)
exchange          VARCHAR(8)
segment           VARCHAR(8)
change_type       VARCHAR(32)  -- SYMBOL_CHANGE | TOKEN_CHANGE | CORPORATE_ACTION | EXPIRY | DELISTED
valid_from        DATE         NOT NULL
valid_to          DATE
reason            TEXT
source            VARCHAR(64)
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
```

---

### 3.2 LAYER 2 — CANONICAL CANDLES

#### equity_candle (NEW — TimescaleDB hypertable on `time`)
Stores OHLCV for NSE/BSE equities AND indices. Both are unified here because they share the same semantic structure (no expiry, no strike, no lot size at the candle level).

```sql
id                BIGSERIAL
instrument_id     VARCHAR(64)  NOT NULL
exchange          VARCHAR(8)   NOT NULL
segment           VARCHAR(8)   NOT NULL DEFAULT 'EQ'  -- EQ | IDX | ETF
interval_str      VARCHAR(4)   NOT NULL
time              TIMESTAMPTZ  NOT NULL               -- hypertable partition key
session_date      DATE         NOT NULL
open              NUMERIC(18,6) NOT NULL
high              NUMERIC(18,6) NOT NULL
low               NUMERIC(18,6) NOT NULL
close             NUMERIC(18,6) NOT NULL
volume            BIGINT        NOT NULL DEFAULT 0
vwap              NUMERIC(18,6)
turnover          NUMERIC(24,4)                       -- total traded value INR
data_origin       VARCHAR(16)   NOT NULL DEFAULT 'PROVIDER'  -- PROVIDER | DERIVED
derived_from_interval  VARCHAR(4)                     -- NULL if origin=PROVIDER
aggregation_version    VARCHAR(16)                    -- NULL if origin=PROVIDER
provider          VARCHAR(32)   NOT NULL
source_type       VARCHAR(32)   NOT NULL
source_timestamp  TIMESTAMPTZ
received_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
normalisation_version  VARCHAR(16) NOT NULL DEFAULT '2.0.0'
dataset_version   BIGINT        NOT NULL DEFAULT 1
quality_status    VARCHAR(16)   NOT NULL DEFAULT 'TRUSTED'
poor_quality      BOOLEAN       NOT NULL DEFAULT FALSE
reconciliation_status  VARCHAR(32)
provenance_id     UUID
volume_unavailable  BOOLEAN     NOT NULL DEFAULT FALSE
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()

PRIMARY KEY (id, time)
UNIQUE (instrument_id, exchange, interval_str, time)

CHECK (interval_str <> '3m')
CHECK (high >= open)
CHECK (high >= close)
CHECK (low <= open)
CHECK (low <= close)
CHECK (high >= low)
CHECK (volume >= 0)
CHECK (data_origin IN ('PROVIDER', 'DERIVED'))
CHECK (quality_status IN ('TRUSTED','DEGRADED','POOR_QUALITY','BLOCKED','MISSING','QUARANTINED','DERIVED'))
```

**TimescaleDB:**
- Hypertable on `time`, chunk_interval `7 days`
- Compression after `30 days`
- Retention policy: **permanent** (ML training data)

**Indexes:**
```sql
-- Primary query pattern: symbol + interval + time range
(instrument_id, interval_str, time DESC)

-- Exchange + interval scan (e.g. all NSE 1d)
(exchange, segment, interval_str, time DESC)

-- Latest candle per instrument+interval (point query)
(instrument_id, interval_str, session_date DESC)

-- Quality filter (gap recovery, ML eligibility)
(quality_status, instrument_id, interval_str, time DESC) WHERE quality_status <> 'TRUSTED'
```

---

#### futures_candle (NEW — TimescaleDB hypertable on `time`)
```sql
id                BIGSERIAL
instrument_id     VARCHAR(64)  NOT NULL
underlying_id     VARCHAR(64)                          -- instrument_id of underlying equity/index
exchange          VARCHAR(8)   NOT NULL                -- NFO | BFO
interval_str      VARCHAR(4)   NOT NULL
time              TIMESTAMPTZ  NOT NULL
session_date      DATE         NOT NULL
expiry            DATE         NOT NULL
contract_type     VARCHAR(8)   NOT NULL DEFAULT 'FUT'
open              NUMERIC(18,6) NOT NULL
high              NUMERIC(18,6) NOT NULL
low               NUMERIC(18,6) NOT NULL
close             NUMERIC(18,6) NOT NULL
volume            BIGINT        NOT NULL DEFAULT 0
open_interest     BIGINT                               -- NULL if unavailable
oi_change         BIGINT                               -- change from prior candle
vwap              NUMERIC(18,6)
turnover          NUMERIC(24,4)
data_origin       VARCHAR(16)   NOT NULL DEFAULT 'PROVIDER'
derived_from_interval  VARCHAR(4)
aggregation_version    VARCHAR(16)
provider          VARCHAR(32)   NOT NULL
source_type       VARCHAR(32)   NOT NULL
source_timestamp  TIMESTAMPTZ
received_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
normalisation_version  VARCHAR(16) NOT NULL DEFAULT '2.0.0'
dataset_version   BIGINT        NOT NULL DEFAULT 1
quality_status    VARCHAR(16)   NOT NULL DEFAULT 'TRUSTED'
poor_quality      BOOLEAN       NOT NULL DEFAULT FALSE
reconciliation_status  VARCHAR(32)
provenance_id     UUID
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()

PRIMARY KEY (id, time)
UNIQUE (instrument_id, exchange, interval_str, time)

CHECK (interval_str <> '3m')
CHECK (high >= open)
CHECK (high >= close)
CHECK (low <= open)
CHECK (low <= close)
CHECK (high >= low)
CHECK (volume >= 0)
CHECK (open_interest IS NULL OR open_interest >= 0)
CHECK (contract_type = 'FUT')
```

**TimescaleDB:** hypertable on `time`, chunk `7 days`

**Additional Indexes:**
```sql
(underlying_id, expiry, interval_str, time DESC)  -- expiry chain lookup
(exchange, expiry, interval_str, time DESC)
```

---

#### options_candle (NEW — TimescaleDB hypertable on `time`)
```sql
id                BIGSERIAL
instrument_id     VARCHAR(64)  NOT NULL
underlying_id     VARCHAR(64)
exchange          VARCHAR(8)   NOT NULL
interval_str      VARCHAR(4)   NOT NULL
time              TIMESTAMPTZ  NOT NULL
session_date      DATE         NOT NULL
expiry            DATE         NOT NULL
strike            NUMERIC(18,2) NOT NULL
option_type       VARCHAR(2)   NOT NULL               -- CE | PE only
open              NUMERIC(18,6) NOT NULL
high              NUMERIC(18,6) NOT NULL
low               NUMERIC(18,6) NOT NULL
close             NUMERIC(18,6) NOT NULL
volume            BIGINT        NOT NULL DEFAULT 0
open_interest     BIGINT
oi_change         BIGINT
vwap              NUMERIC(18,6)
turnover          NUMERIC(24,4)
data_origin       VARCHAR(16)   NOT NULL DEFAULT 'PROVIDER'
derived_from_interval  VARCHAR(4)
aggregation_version    VARCHAR(16)
provider          VARCHAR(32)   NOT NULL
source_type       VARCHAR(32)   NOT NULL
source_timestamp  TIMESTAMPTZ
received_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
normalisation_version  VARCHAR(16) NOT NULL DEFAULT '2.0.0'
dataset_version   BIGINT        NOT NULL DEFAULT 1
quality_status    VARCHAR(16)   NOT NULL DEFAULT 'TRUSTED'
poor_quality      BOOLEAN       NOT NULL DEFAULT FALSE
reconciliation_status  VARCHAR(32)
provenance_id     UUID
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()

PRIMARY KEY (id, time)
UNIQUE (instrument_id, exchange, interval_str, time)

CHECK (interval_str <> '3m')
CHECK (option_type IN ('CE', 'PE'))
CHECK (strike > 0)
CHECK (high >= open)
CHECK (high >= close)
CHECK (low <= open)
CHECK (low <= close)
CHECK (high >= low)
CHECK (volume >= 0)
CHECK (open_interest IS NULL OR open_interest >= 0)
```

**Additional Indexes:**
```sql
(underlying_id, expiry, strike, option_type, interval_str, time DESC)  -- option chain lookup
(underlying_id, expiry, interval_str, time DESC)
```

---

### 3.3 LAYER 3 — LIVE DATA

#### market_tick (NEW — TimescaleDB hypertable on `timestamp`)
Hot storage for live WebSocket ticks. Retention: 7 days raw; aggregate into candles.

```sql
tick_id           BIGSERIAL
instrument_id     VARCHAR(64)  NOT NULL
exchange          VARCHAR(8)   NOT NULL
timestamp         TIMESTAMPTZ  NOT NULL               -- exchange timestamp
received_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
ltp               NUMERIC(18,6)
open              NUMERIC(18,6)                       -- day open
high              NUMERIC(18,6)                       -- day high
low               NUMERIC(18,6)                       -- day low
close             NUMERIC(18,6)                       -- previous close
volume            BIGINT
open_interest     BIGINT
bid               NUMERIC(18,6)
ask               NUMERIC(18,6)
bid_quantity      BIGINT
ask_quantity      BIGINT
last_traded_quantity   BIGINT
total_buy_quantity     BIGINT
total_sell_quantity    BIGINT
provider          VARCHAR(32)   NOT NULL
source_type       VARCHAR(32)   NOT NULL
sequence_number   BIGINT
source_timestamp  TIMESTAMPTZ
quality_status    VARCHAR(16)   NOT NULL DEFAULT 'TRUSTED'
session_date      DATE

PRIMARY KEY (tick_id, timestamp)

CHECK (quality_status IN ('TRUSTED','DEGRADED','POOR_QUALITY','BLOCKED','MISSING','QUARANTINED','DERIVED'))
```

**TimescaleDB:** hypertable on `timestamp`, chunk `1 day`, retention `7 days`

---

#### market_quote (NEW — TimescaleDB hypertable on `timestamp`)
Snapshot-level quote records (e.g. from REST polling or WebSocket full-quote messages).

```sql
quote_id          BIGSERIAL
instrument_id     VARCHAR(64)  NOT NULL
exchange          VARCHAR(8)   NOT NULL
timestamp         TIMESTAMPTZ  NOT NULL
received_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
ltp               NUMERIC(18,6)
ltq               BIGINT
open              NUMERIC(18,6)
high              NUMERIC(18,6)
low               NUMERIC(18,6)
close             NUMERIC(18,6)
volume            BIGINT
open_interest     BIGINT
bid               NUMERIC(18,6)
ask               NUMERIC(18,6)
bid_quantity      BIGINT
ask_quantity      BIGINT
total_buy_quantity     BIGINT
total_sell_quantity    BIGINT
upper_circuit     NUMERIC(18,6)
lower_circuit     NUMERIC(18,6)
week_high_52      NUMERIC(18,6)
week_low_52       NUMERIC(18,6)
provider          VARCHAR(32)   NOT NULL
source_timestamp  TIMESTAMPTZ
quality_status    VARCHAR(16)   NOT NULL DEFAULT 'TRUSTED'
session_date      DATE

PRIMARY KEY (quote_id, timestamp)
```

**TimescaleDB:** hypertable on `timestamp`, chunk `1 day`, retention `30 days`

---

### 3.4 LAYER 4 — OPTION CHAIN

#### option_chain_snapshot (NEW)
```sql
snapshot_id       UUID PRIMARY KEY DEFAULT gen_random_uuid()
underlying_id     VARCHAR(64)  NOT NULL
exchange          VARCHAR(8)   NOT NULL
timestamp         TIMESTAMPTZ  NOT NULL
expiry            DATE         NOT NULL
spot_price        NUMERIC(18,6)
atm_strike        NUMERIC(18,2)
provider          VARCHAR(32)   NOT NULL
source_timestamp  TIMESTAMPTZ
received_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
pcr_oi            NUMERIC(10,4)
pcr_volume        NUMERIC(10,4)
total_ce_oi       BIGINT
total_pe_oi       BIGINT
max_pain          NUMERIC(18,2)
atm_iv            NUMERIC(10,4)
quality_status    VARCHAR(16)   NOT NULL DEFAULT 'TRUSTED'
session_date      DATE

INDEX: (underlying_id, expiry, timestamp DESC)
INDEX: (exchange, timestamp DESC)
```

#### option_chain_contract (NEW)
```sql
id                BIGSERIAL PRIMARY KEY
snapshot_id       UUID NOT NULL REFERENCES option_chain_snapshot(snapshot_id)
instrument_id     VARCHAR(64)
strike            NUMERIC(18,2) NOT NULL
option_type       VARCHAR(2)   NOT NULL CHECK (option_type IN ('CE','PE'))
ltp               NUMERIC(18,6)
open              NUMERIC(18,6)
high              NUMERIC(18,6)
low               NUMERIC(18,6)
close             NUMERIC(18,6)
volume            BIGINT
open_interest     BIGINT
oi_change         BIGINT
bid               NUMERIC(18,6)
ask               NUMERIC(18,6)
bid_quantity      BIGINT
ask_quantity      BIGINT
iv                NUMERIC(10,4)                       -- NULL if not supplied
delta             NUMERIC(10,6)
gamma             NUMERIC(10,8)
theta             NUMERIC(10,6)
vega              NUMERIC(10,6)
rho               NUMERIC(10,6)
intrinsic_value   NUMERIC(18,6)
time_value        NUMERIC(18,6)
is_atm            BOOLEAN       NOT NULL DEFAULT FALSE

INDEX: (snapshot_id, strike, option_type)
CHECK (option_type IN ('CE', 'PE'))
CHECK (strike > 0)
```

#### option_greeks_snapshot (NEW — TimescaleDB hypertable on `timestamp`)
```sql
id                BIGSERIAL
instrument_id     VARCHAR(64)  NOT NULL
timestamp         TIMESTAMPTZ  NOT NULL
underlying_price  NUMERIC(18,6)
option_price      NUMERIC(18,6)
iv                NUMERIC(10,4)
delta             NUMERIC(10,6)
gamma             NUMERIC(10,8)
theta             NUMERIC(10,6)
vega              NUMERIC(10,6)
rho               NUMERIC(10,6)
calculation_method  VARCHAR(32)                       -- BS | BINOMIAL | PROVIDER
provider          VARCHAR(32)
source_timestamp  TIMESTAMPTZ
received_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
quality_status    VARCHAR(16)  NOT NULL DEFAULT 'TRUSTED'
session_date      DATE

PRIMARY KEY (id, timestamp)
INDEX: (instrument_id, timestamp DESC)
```

---

### 3.5 LAYER 5 — CALENDAR + SESSIONS

#### exchange_calendar (NEW)
```sql
id                BIGSERIAL PRIMARY KEY
exchange          VARCHAR(8)   NOT NULL
segment           VARCHAR(8)
market            VARCHAR(32)
calendar_date     DATE         NOT NULL
day_type          VARCHAR(32)  NOT NULL   -- TRADING_DAY | WEEKEND | OFFICIAL_HOLIDAY | SPECIAL_SESSION | NOT_PUBLISHED | UNKNOWN
holiday_name      VARCHAR(256)
holiday_type      VARCHAR(32)             -- NATIONAL | EXCHANGE_SPECIFIC | HALF_DAY
is_trading_day    BOOLEAN      NOT NULL
session_status    VARCHAR(32)  NOT NULL   -- OPEN | CLOSED | HALF_DAY | MUHURAT | UNKNOWN
session_open      TIME                    -- IST session open time
session_close     TIME                    -- IST session close time
special_session   BOOLEAN      NOT NULL DEFAULT FALSE
is_official       BOOLEAN      NOT NULL DEFAULT FALSE
source            VARCHAR(64)             -- NSE_CIRCULAR | NSE_API | MANUAL
source_reference  VARCHAR(256)
year              INTEGER      NOT NULL
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()

UNIQUE (exchange, segment, calendar_date)
INDEX: (exchange, year, is_trading_day)
INDEX: (calendar_date) WHERE is_trading_day = TRUE
```

#### market_session (NEW)
```sql
session_id        BIGSERIAL PRIMARY KEY
exchange          VARCHAR(8)   NOT NULL
segment           VARCHAR(8)   NOT NULL DEFAULT 'EQ'
session_date      DATE         NOT NULL
session_type      VARCHAR(32)  NOT NULL   -- REGULAR | PRE_OPEN | POST_MARKET | MUHURAT | SPECIAL
open_time         TIMESTAMPTZ
close_time        TIMESTAMPTZ
is_trading_day    BOOLEAN      NOT NULL
actual_open       TIMESTAMPTZ             -- actual first trade time
actual_close      TIMESTAMPTZ             -- actual last trade time
source            VARCHAR(64)
notes             TEXT
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()

UNIQUE (exchange, segment, session_date, session_type)
INDEX: (exchange, session_date DESC)
INDEX: (session_date) WHERE is_trading_day = TRUE
```

#### fno_universe_membership (NEW)
```sql
id                BIGSERIAL PRIMARY KEY
snapshot_id       BIGINT REFERENCES fno_universe_snapshot(id)
instrument_id     VARCHAR(64)  NOT NULL
underlying        VARCHAR(64)
segment           VARCHAR(8)   NOT NULL DEFAULT 'FO'
effective_from    DATE         NOT NULL
effective_to      DATE
status            VARCHAR(16)  NOT NULL DEFAULT 'ACTIVE'  -- ACTIVE | REMOVED | SUSPENDED
removal_reason    TEXT
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()

UNIQUE (instrument_id, effective_from)
INDEX: (instrument_id, effective_to) WHERE status = 'ACTIVE'
INDEX: (underlying, effective_from)
```

---

### 3.6 LAYER 6 — OPERATIONS

#### ingestion_job (NEW)
```sql
job_id            UUID PRIMARY KEY DEFAULT gen_random_uuid()
job_type          VARCHAR(32)  NOT NULL   -- BACKFILL | LIVE | RECONCILE | GAP_RECOVERY
dataset           VARCHAR(32)  NOT NULL   -- EQUITY_CANDLE | FUTURES_CANDLE | OPTIONS_CANDLE | etc.
provider          VARCHAR(32)  NOT NULL
instrument_id     VARCHAR(64)
exchange          VARCHAR(8)
interval_str      VARCHAR(4)
start_time        TIMESTAMPTZ
end_time          TIMESTAMPTZ
status            VARCHAR(16)  NOT NULL DEFAULT 'PENDING'
requested_rows    INTEGER
received_rows     INTEGER
inserted_rows     INTEGER
updated_rows      INTEGER
skipped_rows      INTEGER
invalid_rows      INTEGER
duplicate_rows    INTEGER
failed_rows       INTEGER
quarantine_rows   INTEGER
started_at        TIMESTAMPTZ
completed_at      TIMESTAMPTZ
error             TEXT
retry_count       INTEGER      NOT NULL DEFAULT 0
parent_job_id     UUID REFERENCES ingestion_job(job_id)
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()

INDEX: (instrument_id, interval_str, dataset, status)
INDEX: (status, created_at DESC) WHERE status <> 'COMPLETED'
```

#### ingestion_checkpoint (NEW)
```sql
id                BIGSERIAL PRIMARY KEY
provider          VARCHAR(32)  NOT NULL
dataset           VARCHAR(32)  NOT NULL
instrument_id     VARCHAR(64)  NOT NULL
exchange          VARCHAR(8)   NOT NULL
interval_str      VARCHAR(4)   NOT NULL
last_successful_timestamp  TIMESTAMPTZ
last_attempted_timestamp   TIMESTAMPTZ
last_job_id       UUID REFERENCES ingestion_job(job_id)
status            VARCHAR(16)  NOT NULL DEFAULT 'IDLE'
consecutive_failures  INTEGER  NOT NULL DEFAULT 0
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()

UNIQUE (provider, dataset, instrument_id, exchange, interval_str)
INDEX: (instrument_id, exchange, interval_str, dataset)
INDEX: (status) WHERE status <> 'IDLE'
```

#### candle_bar_quarantine (NEW)
Holds candle_bar rows that could not be classified during migration.

```sql
id                BIGSERIAL PRIMARY KEY
original_id       BIGINT       NOT NULL   -- candle_bar.id
instrument_id     VARCHAR(64)  NOT NULL
exchange          VARCHAR(8)
interval_str      VARCHAR(4)
time              TIMESTAMPTZ  NOT NULL
open              NUMERIC(18,6)
high              NUMERIC(18,6)
low               NUMERIC(18,6)
close             NUMERIC(18,6)
volume            BIGINT
oi                BIGINT
provider          VARCHAR(32)
source_type       VARCHAR(32)
normalisation_version  VARCHAR(16)
quarantine_reason  TEXT        NOT NULL
quarantine_status  VARCHAR(16) NOT NULL DEFAULT 'PENDING'  -- PENDING | RESOLVED | DISCARDED
resolved_to_table  VARCHAR(64)
resolved_at       TIMESTAMPTZ
created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
```

---

## 4. TIMESCALEDB PROMOTION PLAN

| Table | Hypertable | Chunk Interval | Compression After | Retention |
|---|---|---|---|---|
| `equity_candle` | YES | 7 days | 30 days | Permanent |
| `futures_candle` | YES | 7 days | 30 days | Permanent |
| `options_candle` | YES | 7 days | 30 days | Permanent |
| `market_tick` | YES | 1 day | 7 days | 7 days |
| `market_quote` | YES | 1 day | 7 days | 30 days |
| `option_greeks_snapshot` | YES | 1 day | 30 days | 90 days |
| `provider_health` | Promote existing | 1 day | 7 days | 30 days |
| All others | NO | — | — | Permanent |

---

## 5. DATA QUALITY ENUM STANDARD

All tables using `quality_status` must use exactly this vocabulary:

| Status | Meaning |
|---|---|
| `TRUSTED` | Verified, complete, no issues |
| `DEGRADED` | Minor gaps or quality concerns; usable with caveats |
| `POOR_QUALITY` | Multiple issues; use only as last resort |
| `BLOCKED` | Must not be used; blocked by quality rule |
| `MISSING` | Expected but not present |
| `QUARANTINED` | Under investigation; do not use |
| `DERIVED` | Computed from other data, not directly observed |

ML eligibility:
- `TRUSTED` → ML_READY
- `DEGRADED` → ML_READY_WITH_LIMITATIONS
- `POOR_QUALITY`, `BLOCKED`, `QUARANTINED` → NOT_ML_READY
- `DERIVED` → ML_READY (if source was TRUSTED)

---

## 6. API COMPATIBILITY PLAN

The `candle_bar` table is NOT dropped. It is marked DEPRECATED.

All reads currently targeting `candle_bar` in:
- `src/api/india.py` → redirect to `equity_candle` (after migration)
- `src/engines/historical_engine.py` → write to `equity_candle` / `futures_candle` / `options_candle`
- `src/api/compat.py` → redirect to `equity_candle`

The `candle_bar` table remains accessible as a read-only archive until:
1. All consumers are confirmed switched to new tables
2. Zero application writes for 7+ days
3. Explicit deprecation migration is run

---

## 7. MIGRATION SAFETY CONTROLS

1. All new tables created WITHOUT dropping `candle_bar`
2. Migration uses `INSERT … SELECT` with ON CONFLICT DO NOTHING (idempotent)
3. Row counts verified before and after each migration segment
4. Checksums (MD5 aggregate of sorted OHLCV) verified for sampled rows
5. `candle_bar.reconciliation_status` updated to `MIGRATED_V2` after successful migration
6. `candle_bar_quarantine` receives all rows that could not be classified

---

## 8. FINAL TABLE COUNT

| Layer | Tables | Notes |
|---|---|---|
| Instrument Identity | 3 | instrument_master(enhanced) + 2 new |
| Canonical Candles | 3 | equity_candle, futures_candle, options_candle |
| Live Data | 2 | market_tick, market_quote |
| Option Chain | 3 | snapshot, contract, greeks |
| Calendar + Sessions | 3 | calendar, session, fno_membership |
| Operations | 3 | ingestion_job, checkpoint, quarantine |
| Existing (retained) | 6 | candle_bar(deprecated), fno_universe_snapshot, data_gap, data_incident, data_provenance, provider_health |
| **Total** | **23** | |
