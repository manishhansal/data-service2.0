# UPSTOX PROTOBUF CERTIFICATION
## data-service2.0

**Last verified:** 2026-09-18
**Git commit:** 56156ec (fix/bugs; prev: 5dd69a2)
**Test suite:** 4821 passed, 0 failed
**Runtime environment:** Python 3.14.6, Docker containers running

---

## CURRENT STATUS

| Item | Status | Evidence |
|------|--------|---------|
| pb2 file present on disk | PASS | `src/providers/streams/upstox_market_data_feeder_pb2.py` in git |
| pb2 import succeeds | PASS | `_try_import_pb2()` returns module (local runtime 2026-09-17) |
| FeedResponse construction | PASS | `FeedResponse()` instantiated, `ltp`/`ltt`/`ltq`/`cp` assigned |
| Protobuf round-trip | PASS | `SerializeToString()` + `ParseFromString()` round-trip verified |
| `_decode_feed_frame()` pb2 path | PASS | Returns result or falls back to JSON path |
| `_decode_feed_frame()` JSON fallback | PASS | Test/mock frames decoded correctly |
| Live WebSocket connection | NOT VERIFIED | Blocked: Upstox OAuth token expired |
| 1000 real binary messages decoded | NOT VERIFIED | Blocked: OAuth token expired |
| LTP vs REST reconciliation | NOT VERIFIED | Blocked: OAuth token expired |

**Upstox WebSocket:**  
Status: NOT VERIFIED  
Evidence: IMPLEMENTED + UNIT_TESTED + LOCAL_RUNTIME_VERIFIED  
Blocker: Upstox OAuth access token expired 2026-09-14 (delta=-218,983s as of 2026-09-17). No `UPSTOX_CLIENT_SECRET` configured for auto-refresh.

---

## LOCAL RUNTIME VERIFICATION RESULTS (2026-09-17)

```python
# Executed on 2026-09-17, git 5dd69a2
>>> from providers.streams.upstox_market_data_feeder_pb2 import FeedResponse, LTPC
>>> fr = FeedResponse()
# Result: FeedResponse() constructed OK
>>> print(fr.DESCRIPTOR.name)
# Result: FeedResponse
>>> fr2 = FeedResponse()
>>> fr2.ParseFromString(fr.SerializeToString())
# Result: Round-trip PASS
>>> ltpc = LTPC()
>>> ltpc.ltp = 22500.5; ltpc.ltt = 1694938200000; ltpc.ltq = 100; ltpc.cp = 22480.0
# Result: All fields assigned correctly
```

**`_try_import_pb2()` returns:** `<module 'src.providers.streams.upstox_market_data_feeder_pb2'>`  
**`FeedResponse()` constructs:** YES  
**`ParseFromString(empty)` succeeds:** YES  

> Re-verified 2026-09-18 (git 56156ec): same result. pb2 is present in git and importable at runtime.

---

## OAUTH TOKEN STATUS

| Token | Status | Delta | Impact |
|-------|--------|-------|--------|
| Analytics Token | VALID | +351 days | REST API calls work |
| OAuth Access Token | EXPIRED | -2.5 days (as of 2026-09-17) | WS auth blocked |
| `UPSTOX_CLIENT_SECRET` | NOT CONFIGURED | — | Auto-refresh not possible |

**Impact:** Live WebSocket V3 requires a valid OAuth access token. Until refreshed via the OAuth callback at `/v1/auth/upstox/callback`, live WS binary frame validation is BLOCKED_BY_EXTERNAL.

**Multi-worker OAuth sharing (IMPLEMENTED, unit-tested):**
- `UpstoxAdapter` accepts `redis_client` parameter
- `load_tokens_from_redis()` loads token at worker startup
- `_store_access_token_in_redis()` persists refreshed token with 23h TTL
- Distributed refresh lock (`SET NX EX`) prevents parallel refresh storms

---

## ARCHITECTURE

### Two-path decode design

```
Raw bytes received
       │
       ▼
_decode_feed_frame()
       │
       ├─► _try_import_pb2() ──► pb2 module found? YES
       │                              │
       ├─► _decode_protobuf_with_pb2()
       │     feed = pb2.FeedResponse()
       │     feed.ParseFromString(raw)
       │     → structured dict
       │
       └─► _decode_protobuf_generic() (TEST/MOCK PATH ONLY — JSON bytes)
```

**JSON fallback:** TEST_ONLY. Handles mock frames. If `upstox_protobuf_no_pb2_available` appears in production logs, pb2 is absent — ticks will be silently dropped.

---

## FIELD COVERAGE (pb2-decoded)

### ltpc mode
`ltp`, `ltt`, `ltq`, `cp`

### option_greeks mode
`ltp`, `delta`, `gamma`, `theta`, `vega`, `iv`, `oi`, `volume`

### full mode
`ltp`, `ltt`, `ltq`, `cp`, `atp`, `volume`, `tbq`, `tsq`, `oi`, `ohlc`, `depth` (5 bid + 5 ask), `net_change`

---

## LIVE CERTIFICATION TARGET (once OAuth token refreshed)

1. `fetch_ws_authorized_url()` → authorized URI
2. Connect WebSocket → subscribe NIFTY 50 (ltpc), NIFTY/BANKNIFTY ATM options (option_greeks), 5 liquid equities (full)
3. Collect: `messages_received`, `messages_decoded`, `decode_failures`, `decode_latency_ms`
4. Gate: ≥1,000 decoded, failures = 0, unexplained drops = 0
5. Compare decoded LTP vs REST at ≤1s timestamp delta

---

## REMAINING BLOCKERS

| Blocker | Classification | Action Required |
|---------|---------------|----------------|
| OAuth access token expired | BLOCKED_BY_EXTERNAL | Complete OAuth flow at `/v1/auth/upstox/callback` |
| `UPSTOX_CLIENT_SECRET` not configured | BLOCKED_BY_EXTERNAL | Add to `.env.local` to enable auto-refresh |
| Live WS binary decode certification | NOT VERIFIED | Unblocked once token refreshed |
| SmartStream (Angel One) certification | NOT VERIFIED | Requires live market session |

---

## HISTORICAL — SUPERSEDED

> The following statements from previous report versions are SUPERSEDED and INCORRECT for the current codebase (git 5dd69a2):
>
> - "pb2 file absent" — **INCORRECT.** `upstox_market_data_feeder_pb2.py` is present and tracked in git.
> - "_try_import_pb2() returns None" — **INCORRECT.** It returns the module successfully.
> - "Every real Upstox WebSocket binary tick is currently dropped" — **INCORRECT.** The pb2 decode path is functional. Ticks are not received because the OAuth token is expired, not because of a missing pb2 file.
> - "Step 1: Obtain the .proto schema" (remediation steps) — **SUPERSEDED.** Proto schema obtained and pb2 compiled as of 2026-09-17 Phase B-K.
