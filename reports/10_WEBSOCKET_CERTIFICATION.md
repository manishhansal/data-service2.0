# REPORT 10 — WEBSOCKET CERTIFICATION
**Audit date:** 2026-09-13

---

## Status: ⛔ BLOCKED — Service Not Running

No WebSocket tests can be performed. Docker Compose not running.

---

## WebSocket Implementation Matrix

| Provider | Stream File | Connect | Auth | Subscribe | Tick | Dedup | Heartbeat | Reconnect | Test File | Runtime |
|---|---|---|---|---|---|---|---|---|---|---|
| Angel One SmartStream | `angel_one_stream.py` | ✅ code | JWT + feedToken ✅ | ✅ | ✅ | ✅ code | ✅ | ✅ | `test_angel_one_stream.py` | ⛔ BLOCKED |
| Upstox V3 WebSocket | `upstox_stream.py` | ✅ code | OAuth ✅ | ✅ | ✅ | ✅ code | ✅ | ✅ | `test_upstox_stream.py` | ⛔ BLOCKED |
| Binance WebSocket | `binance_stream.py` | ✅ code | Optional API key ✅ | ✅ | ✅ | ✅ code | ✅ | ✅ | `test_binance_stream.py` | ⛔ BLOCKED |
| Delta Exchange WS | ❌ NO FILE | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ NOT IMPL |
| Deribit WS | ❌ NO STREAM | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ NOT IMPL |

---

## DATA-SERVICE WebSocket Endpoint

`WS /v1/stream/ticks`

Implemented in `src/api/streaming.py`:
- Subscribe/unsubscribe control messages: ✅ code
- Heartbeat every 10s: ✅ code
- Max 500 concurrent streams: ✅ code
- Consumer fanout: ✅ code

Status: 🟡 IMPLEMENTED BUT NOT RUNTIME-VERIFIED

---

## AlphaForge WebSocket Usage

| AF Component | Target | Direct/Via DATA-SERVICE | Status |
|---|---|---|---|
| `BinanceWsClient` (src/services/binance/ws.ts) | `wss://stream.binance.com:9443` | **DIRECT** — P0 bypass | 🔴 |
| Delta WS client (brokers/delta/ws.ts) | `wss://public-socket.india.delta.exchange` | **DIRECT** — P0 bypass | 🔴 |
| SmartStream (angelone/smartstream.ts) | Angel One SmartStream | **DIRECT** — fallback | P1 |
| ScraplingProvider subscribe | DATA-SERVICE Redis pub/sub | CORRECT via Redis | ✅ |
| Worker scraping-tick-listener | DATA-SERVICE Redis pub/sub | CORRECT | ✅ |

---

## WebSocket Failure Tests (All Blocked)

| Test | Expected | Actual | Status |
|---|---|---|---|
| Kill Angel One WS | Reconnect, resubscribe, no stale ticks | Cannot test | ⛔ BLOCKED |
| Kill Binance WS | Reconnect, dedup | Cannot test | ⛔ BLOCKED |
| Kill Upstox WS | Reconnect, resubscribe | Cannot test | ⛔ BLOCKED |
| Consumer fanout during disconnect | Heartbeat timeout to consumer | Cannot test | ⛔ BLOCKED |
| Stale detection (>60s no frame) | Force reconnect | Cannot test | ⛔ BLOCKED |

---

## Upstox V3 Protobuf Note

The `upstox_stream.py` adapter skeleton is in place. Full Protobuf schema decoding requires the Upstox-generated `.proto` definitions from the Upstox SDK. This is documented as a known limitation in `PRODUCTION_CERTIFICATION.md`.

**Assessment**: This means Upstox V3 WebSocket binary data may not be correctly decoded until the proto definitions are available. This is P2 — the REST path works; the WebSocket binary decoding is incomplete.
