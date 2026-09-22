# LIVE LATENCY REPORT
## data-service2.0 — Updated with 2026-09-17 Measurements

**Report date:** 2026-09-17  
**Measurement session:** 04:12–04:20 UTC (09:42–09:50 IST) — market OPEN  
**Method:** `time.monotonic()` around each adapter call; single-call measurements (not p-percentile load test)

---

## MEASURED API CALL LATENCIES (DIRECT ADAPTER, MARKET OPEN)

These are round-trip HTTP latencies from inside the Docker container to the provider API and back.

### Angel One

| Operation | Latency | Notes |
|-----------|---------|-------|
| Authentication (TOTP + JWT) | 277–294ms | Network-bound; TOTP generation ~1ms |
| RELIANCE FULL quote (token 2885) | 242ms | |
| RELIANCE getLtpData | 327ms | Slightly higher than FULL — different endpoint |
| HDFCBANK getLtpData | 113ms | |
| RELIANCE 1d historical (20 days) | 330ms | 13 bars |
| INFY 1d historical (20 days) | 316ms | 13 bars |
| HDFCBANK 1m historical (3 days) | 386ms | 750 bars |
| NIFTY 5m historical (5 days) | 108ms | 152 bars |
| NIFTY FUT 1d historical | 79–232ms | 0 bars (token issue) |
| NIFTY option Greeks | 758ms | 163 contracts |
| BANKNIFTY option Greeks | 540ms | 165 contracts |

### Upstox

| Operation | Latency | Notes |
|-----------|---------|-------|
| LTP V3 (3 instruments) | 328ms | RELIANCE + HDFCBANK + NIFTY 50 |
| Full Quote V2 (1 instrument) | 376ms | RELIANCE |
| RELIANCE 1d historical (20 days) | 330ms | 13 bars |
| HDFCBANK 1d historical (20 days) | 94ms | 13 bars |
| BANKNIFTY 1d historical (20 days) | 90ms | 13 bars |
| NIFTY 1m historical (2 days) | 114ms | 750 bars |
| NIFTY 30m historical (5 days) | 92ms | 26 bars |
| RELIANCE 1w historical (90 days) | 110ms | 14 bars |
| RELIANCE 1M historical (180 days) | 114ms | 7 bars |
| NIFTY option chain (128 strikes) | 333ms | |
| BANKNIFTY option chain (150 strikes) | 304ms | |
| FINNIFTY option chain (123 strikes) | 314ms | |

---

## LATENCY SUMMARY TABLE

| Provider | Operation type | Min observed | Max observed | Notes |
|---------|---------------|-------------|-------------|-------|
| Angel One | REST historical | 79ms | 386ms | Varies by candle count |
| Angel One | REST live quote | 113ms | 327ms | |
| Angel One | REST Greeks | 540ms | 758ms | Heavier response |
| Angel One | Authentication | 277ms | 294ms | |
| Upstox | REST historical | 90ms | 330ms | |
| Upstox | REST LTP/OHLC | 94ms | 376ms | |
| Upstox | REST option chain | 304ms | 333ms | |

**Note:** These are single-call measurements from a containerized environment. Production p50/p95/p99 under sustained load will differ. Run Locust load test (`locust -f locustfile.py`) for production latency percentiles.

---

## END-TO-END PIPELINE LATENCY

Not yet measured. Requires instrumentation of:
- `T1`: provider source timestamp (exchFeedTime)
- `T2`: adapter receives response
- `T3`: normalizer completes
- `T4`: DB write completes
- `T5`: API response serialized

**Current state:** T2 (API response latency) is measured. T1 through T5 pipeline not instrumented.

---

## DESIGN TARGETS vs MEASURED

| Layer | Design target | Measured | Status |
|-------|--------------|---------|--------|
| L1 in-process cache | ≤1ms p99 | Not load-tested | NOT_MEASURED |
| L2 Redis cache | ≤5ms p99 | Not load-tested | NOT_MEASURED |
| Tick publish ≤200ms p99 | ≤200ms p99 | Not load-tested | NOT_MEASURED |
| Angel One REST call | Not specified | 79–758ms observed | MEASURED (single calls) |
| Upstox REST call | Not specified | 90–376ms observed | MEASURED (single calls) |

---

## OBSERVATION

At 09:42–09:50 IST (peak morning session), REST API latencies are 90–760ms per call. Angel One option Greeks calls (500–750ms) are the slowest. Upstox historical calls (90–330ms) are consistently fast. These are network + provider processing times — not controllable by data-service2.0.

For real-time use cases, WebSocket (SmartStream / Upstox V3) eliminates the per-call latency. WebSocket latency is not yet measured (streams not certified).

---

*Single-call measurements only. No p50/p95/p99 load-test data available.*  
*Load test command: `locust -f locustfile.py --host http://localhost:8200 --headless -u 20 -r 2 -t 30m`*


---

## MEASURED API CALL LATENCIES — 2026-09-17 (market OPEN 09:42–09:50 IST)

These are real single-call round-trip latencies measured with `time.monotonic()` inside the Docker container.

### Angel One (direct adapter calls)

| Operation | Latency | Notes |
|-----------|---------|-------|
| Authentication (TOTP + JWT) | 277–294ms | Two measurements |
| RELIANCE FULL quote (token 2885) | 242ms | ltp=1244.1 |
| HDFCBANK getLtpData (token 1333) | 113ms | ltp=715.8 |
| RELIANCE getLtpData (token 2885) | 327ms | ltp=1244.0 |
| RELIANCE 1d historical (20 days, 13 bars) | 330ms | |
| INFY 1d historical (20 days, 13 bars) | 316ms | |
| HDFCBANK 1m historical (3 days, 750 bars) | 386ms | |
| NIFTY 5m historical (5 days, 152 bars) | 108ms | |
| NIFTY option Greeks (163 contracts) | 758ms | |
| BANKNIFTY option Greeks (165 contracts) | 540ms | |

### Upstox (direct adapter calls)

| Operation | Latency | Notes |
|-----------|---------|-------|
| LTP V3 (3 instruments) | 328ms | RELIANCE+HDFCBANK+NIFTY |
| Full Quote V3 (1 instrument) | 376ms | RELIANCE |
| RELIANCE 1d historical (13 bars) | 330ms | |
| HDFCBANK 1d historical (13 bars) | 94ms | |
| BANKNIFTY 1d historical (13 bars) | 90ms | |
| NIFTY 1m historical (750 bars) | 114ms | |
| NIFTY 30m historical (26 bars) | 92ms | |
| NIFTY option chain (128 strikes) | 333ms | |
| BANKNIFTY option chain (150 strikes) | 304ms | |
| FINNIFTY option chain (123 strikes) | 314ms | |
| NIFTY option Greeks (10 contracts) | ~500ms | includes chain prefetch |

**Summary:** Angel One 79–758ms (REST). Upstox 90–376ms (REST). Option Greeks heaviest call (500–760ms). WebSocket latency not yet measured.
