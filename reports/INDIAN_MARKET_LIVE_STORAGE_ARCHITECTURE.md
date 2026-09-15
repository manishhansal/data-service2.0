# INDIAN MARKET LIVE DATA STORAGE ARCHITECTURE
**Version:** 2.0.0  
**Date:** 2026-09-15  
**Status:** DESIGNED — Infrastructure ready, live feed wiring pending

---

## 1. LIVE DATA FLOW

```
Angel One / Upstox / Approved Live Provider
              │
         WebSocket
              │
      Provider Adapter
    (src/providers/adapters/)
              │
     Raw Live Observation
              │
         Validation
    (src/core/validators/)
              │
    ┌─────────┴──────────┐
    ↓                    ↓
market_tick          market_quote
(TimescaleDB)        (TimescaleDB)
    │                    │
    └─────────┬──────────┘
              │
      Candle Builder
  (1m candle aggregation)
              │
       equity_candle
     futures_candle
     options_candle
              │
    ┌─────────┴──────────┐
    ↓                    ↓
Live API/WS          Redis L2 Cache
(FastAPI)            TTL=3s quotes
                     TTL=30s intraday
```

---

## 2. LIVE STORAGE TABLES

### 2.1 market_tick (primary live storage)

**Purpose:** Stores every individual tick received from the WebSocket feed.  
**TimescaleDB:** Hypertable on `timestamp`, 1-day chunks.  
**Retention policy:** 7 days (raw ticks), then data is aggregated into candles.

```
KEY FIELDS:
  instrument_id   — canonical NSE instrument ID
  exchange        — NSE | NFO | BSE
  timestamp       — exchange-side tick time (authoritative)
  received_at     — our ingestion wall-clock time (latency measurement)
  ltp             — last traded price
  volume          — cumulative day volume at tick time
  open_interest   — NULL for equities; populated for F&O
  bid / ask       — NULL when not in provider feed (zero PROHIBITED)
  sequence_number — for ordering within same millisecond
  provider        — angel_one | upstox
  quality_status  — TRUSTED | DEGRADED | POOR_QUALITY
```

**Write pattern:** High-frequency insert, never update.  
**Read pattern:** Latest tick (LIMIT 1), aggregation window (last N seconds).

### 2.2 market_quote (snapshot live storage)

**Purpose:** Full quote snapshots from REST polling or full-quote WebSocket messages.  
**TimescaleDB:** Hypertable on `timestamp`, 1-day chunks.  
**Retention policy:** 30 days.

```
KEY FIELDS:
  instrument_id     — canonical NSE instrument ID
  ltp               — last traded price
  ltq               — last traded quantity
  open/high/low     — day OHLC from provider
  close             — previous session close
  bid / ask         — best bid/ask (NULL when absent)
  upper_circuit     — exchange circuit limit
  lower_circuit     — exchange circuit limit
  week_high_52      — 52-week high
  week_low_52       — 52-week low
  total_buy_qty     — total buyer depth
  total_sell_qty    — total seller depth
```

**Write pattern:** Moderate-frequency insert from REST polling or WS full-quote.  
**Read pattern:** Latest quote per instrument (LIMIT 1).

---

## 3. LIVE DATA PERSISTENCE DECISION

### What is persisted

| Data Type | Persisted? | Where | Retention | Reason |
|---|---|---|---|---|
| Live ticks (LTP + volume) | YES | `market_tick` | 7 days | Candle aggregation, replay, audit |
| Full quotes (depth + circuit) | YES | `market_quote` | 30 days | AlphaForge live signals |
| Option chain snapshots | YES | `option_chain_snapshot` | 90 days | F&O analytics, IV surface |
| Greeks per strike | YES | `option_greeks_snapshot` | 90 days | F&O ML features |
| Aggregated candles (from ticks) | YES | `equity_candle` | Permanent | ML training data |
| Raw WebSocket frames | NO | Object storage (future) | N/A | Cost prohibitive at volume |

### What is NOT persisted permanently

- Raw WebSocket binary frames (too large, not needed after candle aggregation)
- Redundant ticks when provider sends duplicate timestamps
- Ticks outside trading session (pre-open noise)

### Rationale

Live ticks are persisted for 7 days to enable:
1. Candle re-aggregation if a job fails
2. Latency audit (received_at vs timestamp)
3. Gap detection for intraday candles
4. AlphaForge signal replay within the day

After 7 days, ticks are covered by `equity_candle` 1m candles, which are permanent.

---

## 4. CANDLE BUILDER PIPELINE

The live-to-candle pipeline aggregates ticks into canonical 1m candles:

```python
# Conceptual pipeline (implementation in src/engines/streaming_engine.py)

class CandleBuilder:
    """
    Aggregates market_tick rows into equity_candle / futures_candle rows.
    
    1m candle = first tick of minute → open
                max(ltp) in minute  → high
                min(ltp) in minute  → low
                last tick of minute → close
                max(volume) - prev_max(volume) → candle volume
    """
    
    # Triggers:
    # - On minute boundary: flush current candle → equity_candle
    # - On session close: flush final candle
    # - On gap (missing ticks): mark gap in data_gap, quality=DEGRADED
```

The candle builder runs as part of `src/engines/streaming_engine.py` (Phase 8 — live wiring in progress).

---

## 5. REDIS CACHE LAYER

Live data is cached in Redis with these TTLs (from `src/cache/redis_client.py`):

| Cache Key Pattern | TTL | Data |
|---|---|---|
| `mds:quote:{provider}:{exchange}:{symbol}:_:_:_` | 3s | Live quote |
| `mds:batch_quote:{provider}:{exchange}:*` | 3s | Batch quotes |
| `mds:candle:{provider}:{exchange}:{symbol}:{interval}:*` | 30s | Intraday candles |
| `mds:candle:{provider}:{exchange}:{symbol}:1d:*` | 14400s | Daily candles |
| `mds:option_chain:{provider}:{exchange}:{symbol}:*` | 15s | Option chain |
| `mds:instruments:*` | 43200s | Instrument master |
| `mds:health:{provider}:*` | 5s | Provider health |

**Cache invalidation triggers:**
- Market session transition (REGULAR → POST_MARKET → CLOSED): invalidate live quote cache
- New candle written to equity_candle: invalidate intraday candle cache for that instrument+interval
- Instrument update: invalidate instrument_master cache

**Staleness detection:** Every cached response includes `data_timestamp` and `cache_timestamp`. Consumers can compute staleness as `NOW() - data_timestamp`.

**Cache ≠ source of truth:** PostgreSQL/TimescaleDB is the only durable source of truth. Redis is a read-through cache only. If Redis is unavailable, the platform degrades gracefully to DB-only mode.

---

## 6. MARKET_TICK WRITE PATTERN

```sql
-- Idempotent tick insert (sequence_number for dedup)
INSERT INTO market_tick (
    instrument_id, exchange, timestamp, received_at,
    ltp, volume, open_interest, bid, ask,
    provider, source_type, sequence_number, quality_status, session_date
)
VALUES (...)
ON CONFLICT DO NOTHING;
-- Note: market_tick has no unique constraint — duplicates filtered upstream
-- by sequence_number check in the adapter before INSERT
```

---

## 7. LIVE API ENDPOINTS (existing + new)

| Endpoint | Source | Cache TTL |
|---|---|---|
| `GET /v1/india/quotes/{symbol}` | market_quote → Redis | 3s |
| `GET /v1/india/quotes/batch` | market_quote → Redis | 3s |
| `GET /v1/india/option-chain` | option_chain_snapshot → Redis | 15s |
| `GET /v1/india/market/status` | MarketSessionEngine | 5s |
| `GET /v1/india/historical` | equity_candle | 30s/4h |
| WebSocket `/v1/streaming` | market_tick (real-time) | No cache |

---

## 8. LIVE DATA QUALITY RULES

| Condition | Action |
|---|---|
| Tick LTP ≤ 0 | `quality_status = 'BLOCKED'`, do not store |
| Tick timestamp > received_at + 30s | `quality_status = 'DEGRADED'` (stale tick) |
| Provider sends bid = 0 or ask = 0 | Store NULL, not 0 (zero is not valid for bid/ask) |
| OI = 0 for F&O tick | Store NULL (0 OI is ambiguous — could be missing, not actual zero) |
| Missing tick in session | Record in `data_gap`, set next candle `quality_status = 'DEGRADED'` |
| Provider circuit open | `quality_status = 'BLOCKED'`, use cached quote |

---

## 9. LIVE STORAGE STATUS

| Component | Status | Notes |
|---|---|---|
| `market_tick` table | ✅ Created | Empty — ready for live feed |
| `market_quote` table | ✅ Created | Empty — ready for live feed |
| `option_chain_snapshot` table | ✅ Created | Empty — ready for option chain |
| `option_chain_contract` table | ✅ Created | Empty |
| `option_greeks_snapshot` table | ✅ Created | Empty |
| Redis cache layer | ✅ Operational | `data-service-redis` container healthy |
| Angel One adapter authentication | ✅ **FIXED** | Redis JWT sharing — all 4 workers authenticated, zero TOTP conflicts |
| Angel One live quotes (NSE EQ) | ⚠️ Rate limited | Server background polling saturates 3 req/s during REGULAR session |
| Angel One live quotes (NFO FUT) | ✅ Working | F&O futures quotes confirmed working |
| Upstox OAuth access token | ⚠️ Daily refresh needed | Token expires daily; historical data continues to work |
| Streaming engine | ⏳ Pending | `src/engines/streaming_engine.py` — Phase 8 work |
| Candle builder | ⏳ Pending | Part of streaming engine |

---

## 10. LIVE DATA ARCHITECTURE DECISIONS

### Decision 1: Separate tick and quote tables

**Chosen:** `market_tick` (high-frequency, minimal fields) + `market_quote` (full snapshot)  
**Reason:** Ticks arrive at 100ms–1s intervals; quotes arrive at 1s–5s intervals. Mixing them in one table creates schema pressure (tick doesn't need 52-week high; quote doesn't need sequence_number). Separate tables allow different retention policies.

### Decision 2: 7-day tick retention

**Chosen:** 7 days raw, then aggregate to candles  
**Reason:** At 43 NSE symbols × ~375 ticks/min × 375 minutes/session = ~6M ticks/day. 7 days = ~42M rows = acceptable. Beyond 7 days, 1m candles provide equivalent resolution for ML.

### Decision 3: No raw WebSocket frame storage in DB

**Chosen:** Do not store raw WebSocket binary payloads in PostgreSQL  
**Reason:** Raw frames at Angel One WebSocket rates would be ~500 MB/day of opaque binary. Object storage (S3/MinIO) with DB metadata pointers is the correct approach when forensic replay is required.

### Decision 4: Redis is cache-only, not source of truth

**Chosen:** PostgreSQL is the only durable source of truth  
**Reason:** Redis data is volatile. A Redis flush or eviction must never cause data loss. All live data written to `market_tick`/`market_quote` before being cached.
