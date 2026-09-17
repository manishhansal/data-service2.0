# UPSTOX PROTOBUF CERTIFICATION
## data-service2.0 — Phase 51 Report

**Report date:** 2026-09-17  
**Scope:** Upstox WebSocket V3 Protobuf binary decode implementation and certification status  
**Evidence basis:** Static codebase analysis only — NO live WebSocket messages received

---

## CERTIFICATION STATUS

```
NOT_CERTIFIED — pb2 FILE ABSENT, LIVE VERIFICATION REQUIRED
```

The WebSocket V3 connection architecture is implemented and unit-tested. The protobuf binary decode has a two-path design. However, the compiled pb2 module is absent from the repository, making the primary (pb2) decode path unreachable at runtime. Real binary protobuf frames from Upstox cannot be decoded in the current state.

---

## ARCHITECTURE

### Two-path decode design (`src/providers/streams/upstox_stream.py`)

```
Raw bytes received
       │
       ▼
_decode_feed_frame()
       │
       ├─► _try_import_pb2() ──► pb2 module found?
       │                              │
       │                         YES  │  NO
       │                              │
       ├─► _decode_protobuf_with_pb2() │
       │     feed = pb2.FeedResponse() │
       │     feed.ParseFromString(raw) │
       │     → structured dict         │
       │                              ▼
       └─────────────────► _decode_protobuf_generic()
                               Try UTF-8 JSON decode
                               (test/mock path only)
                               If not JSON: log warning
                               upstox_protobuf_no_pb2_available
                               Return None → tick dropped
```

### pb2 import path

`_try_import_pb2()` at `upstox_stream.py:162` attempts:
```python
importlib.import_module("src.providers.streams.upstox_market_data_feeder_pb2")
```

**Result as of 2026-09-17:** Module does not exist. `find . -name "*pb2*"` returns no results. `_try_import_pb2()` returns `None` on every call. The pb2 decode path is never reached.

---

## IMPACT OF MISSING pb2

| Scenario | Outcome |
|----------|---------|
| JSON-format test frame (mocks) | Decoded correctly via `_decode_protobuf_generic()` UTF-8 path |
| Real Upstox binary protobuf frame | `ParseFromString` not called. UTF-8 decode fails. `None` returned. Tick is silently dropped. |
| Log evidence | `upstox_protobuf_no_pb2_available` warning emitted on first frame |

**Every real Upstox WebSocket binary tick is currently dropped.** The fallback path is a test/mock path only — it handles JSON bytes, not binary protobuf.

---

## WEBSOCKET CONNECTION STATUS

| Item | Status | Evidence |
|------|--------|---------|
| Auth endpoint (`/v2/feed/market-data-feed/authorize`) | `UT` | `fetch_ws_authorized_url()` at `upstox.py:966` |
| Dynamic authorized URI used per reconnect | `UT` | `_connect_once()` calls `auth_url_fetcher()` before every connection |
| No hardcoded WSS URL | `UT` | grep confirms no hardcoded `wss://` in stream code |
| Mode: ltpc subscription | `UT` | `SUBSCRIPTION_MODE_LTPC`; subscription message format tested |
| Mode: option_greeks subscription | `UT` | `SUBSCRIPTION_MODE_OPTION_GREEKS` |
| Mode: full subscription | `UT` | `SUBSCRIPTION_MODE_FULL` |
| Mode: full_d30 subscription | `UT` | Code present; `BLK` — requires Upstox Plus plan (not active) |
| Reconnect with exponential backoff | `UT` | base=1s, max=60s, 10 attempts |
| Resubscribe after reconnect | `UT` | `_subscribed_keys` replayed with fresh auth URI |
| change_mode | `UT` | Method implemented and tested |
| unsubscribe | `UT` | Method implemented and tested |
| **Live authorize → connect → tick received** | **`NVL`** | Access token expired; pb2 absent |
| **Real binary frame decode** | **`BLOCKED`** | pb2 absent |
| **1000 real messages decoded** | **`BLOCKED`** | pb2 absent |
| **decode_failure = 0** | **`BLOCKED`** | Cannot be measured without pb2 |
| **silent_drops = 0** | **`BLOCKED`** | All real binary ticks are currently dropped |

---

## SUBSCRIPTION LIMITS (STANDARD PLAN — NOT PLUS)

| Mode | Individual limit | Combined limit |
|------|-----------------|----------------|
| ltpc | 5,000 keys | 2,000 keys |
| option_greeks | 3,000 keys | 2,000 keys |
| full | 2,000 keys | 1,500 keys |
| full_d30 | N/A | N/A (Plus required) |

---

## REMEDIATION REQUIRED

### Step 1: Obtain the .proto schema

Upstox distributes `MarketDataFeed.proto` via their developer portal or SDK repository. As of 2026-09-17 the file is not in this repository.

**Source:** Upstox developer documentation at `https://upstox.com/developer/api-documentation/websocket-market-data`

### Step 2: Compile the pb2 module

```bash
# Install protoc if not present
pip install grpcio-tools

# Compile (adjust paths as needed)
python -m grpc_tools.protoc \
  --proto_path=. \
  --python_out=src/providers/streams/ \
  MarketDataFeed.proto
```

This produces `src/providers/streams/upstox_market_data_feeder_pb2.py`.

### Step 3: Verify the import path

The import in `_try_import_pb2()` expects the module at:
```
src.providers.streams.upstox_market_data_feeder_pb2
```
The compiled file must be placed at:
```
src/providers/streams/upstox_market_data_feeder_pb2.py
```

### Step 4: Live certification

After placing the pb2 file, during market hours with a valid access token:

1. Connect to Upstox WebSocket V3 (call `fetch_ws_authorized_url()` → connect to returned URI)
2. Subscribe `NIFTY 50` in `ltpc` mode
3. Subscribe `NIFTY ATM CE + PE`, `BANKNIFTY ATM CE + PE` in `option_greeks` mode
4. Subscribe 5 liquid equities in `full` mode
5. Record: `messages_received`, `messages_decoded`, `messages_failed`, `messages_dropped`, `decode_latency_ms`
6. Required: 1,000 messages_decoded, messages_failed = 0, messages_dropped = 0
7. Compare decoded LTP against REST quote at same timestamp (tolerance: ≤0.5%)
8. Verify OI from `option_greeks` mode matches REST option chain OI (tolerance: ≤1%)

---

## FIELD COVERAGE (WHEN pb2 IS PRESENT)

Fields extracted by `_decode_protobuf_with_pb2()` from the `FeedResponse` message:

### ltpc mode
`ltp`, `ltt`, `ltq`, `cp` (close price)

### option_greeks mode
`ltp`, `delta`, `gamma`, `theta`, `vega`, `iv`, `oi`, `volume`

### full mode
`ltp`, `ltt`, `ltq`, `cp`, `atp`, `volume`, `tbq`, `tsq`, `oi`, `ohlc` (open/high/low/close), `depth` (5 bid + 5 ask levels), `net_change`

### CAS fields (from full mode sub-message)
`indicative_equilibrium_price`, `indicative_equilibrium_quantity`, `total_indicative_quantity`, `market_indicative_imbalance`, `reference_price`, `cas_eligible`

---

## PROTOBUF JSON FALLBACK — CLASSIFICATION

The `_decode_protobuf_generic()` JSON path is **TEST_ONLY**. It handles mock/test frames sent as JSON bytes. It must not be the production path for real Upstox binary ticks. If `upstox_protobuf_no_pb2_available` appears in production logs, it means the pb2 file is absent and the feed is not being decoded.

```
JSON fallback: TEST_ONLY
```

---

## SAMPLE COUNTS

| Metric | Current value | Required for LRV |
|--------|--------------|-----------------|
| Real binary messages received | 0 | ≥1,000 |
| Real messages decoded via pb2 | 0 | ≥1,000 |
| Decode failures | N/A | = 0 |
| Silent drops | 0 known (pb2 absent, all silently dropped) | = 0 |

---

*No live Upstox WebSocket messages were received for this report.*  
*pb2 file absent as of 2026-09-17. This report will be updated after pb2 compilation and live certification.*
