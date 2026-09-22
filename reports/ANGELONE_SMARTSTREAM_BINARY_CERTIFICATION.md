# ANGEL ONE SMARTSTREAM BINARY CERTIFICATION
## data-service2.0 — Phase 51 Report

**Report date:** 2026-09-17  
**Scope:** SmartStream V2 binary protocol implementation review and certification status  
**Evidence basis:** Static codebase analysis only — NO live binary frames captured  
**Prior live evidence:** None (SmartStream not tested against live feed as of 2026-09-17)

---

## CERTIFICATION STATUS

```
NOT_CERTIFIED — LIVE VERIFICATION REQUIRED
```

The binary decode implementation exists and passes unit tests with synthetic frames. No real SmartStream V2 binary messages have been received and validated. This report documents what is implemented and what must be verified before this capability can be certified.

---

## IMPLEMENTATION REVIEW

### Binary decoder: `_decode_smartstream_binary()` in `src/providers/streams/angel_one_stream.py`

The decoder uses Python `struct` module with little-endian unpacking (`<`). Implemented fields by mode:

#### All modes (minimum frame: 44 bytes)

| Field | Byte offset | Type | Conversion |
|-------|------------|------|-----------|
| mode | 0 | uint16 LE | raw integer (1=LTP, 2=QUOTE, 3=FULL) |
| exchange_type | 2 | uint16 LE | raw integer (1=NSE_EQ, 2=NFO, 3=BSE_EQ, 5=MCX) |
| token | 4–23 | 20-byte ASCII | strip null bytes |
| seq_num | 24 | int64 LE | raw |
| exchange_ts_ms | 32 | int64 LE | milliseconds → datetime |
| ltp_paise | 40 | int64 LE | ÷100 → INR float |

#### QUOTE mode (mode=2, minimum frame: 120 bytes)

| Field | Byte offset | Type | Conversion |
|-------|------------|------|-----------|
| ltq | 48 | int64 LE | raw (last traded quantity) |
| atp_paise | 56 | int64 LE | ÷100 → INR |
| vol | 64 | int64 LE | raw |
| tbq | 72 | float64 LE | raw (total buy qty) |
| tsq | 80 | float64 LE | raw (total sell qty) |
| open_paise | 88 | int64 LE | ÷100 → INR |
| high_paise | 96 | int64 LE | ÷100 → INR |
| low_paise | 104 | int64 LE | ÷100 → INR |
| close_paise | 112 | int64 LE | ÷100 → INR |

#### FULL mode (mode=3, minimum frame: 168 bytes for OHLC+OI+circuit; more for depth)

| Field | Byte offset | Type | Conversion |
|-------|------------|------|-----------|
| last_trade_ts | 120 | int64 LE | milliseconds |
| oi | 128 | int64 LE | raw (open interest in contracts) |
| oi_change | 136 | int64 LE | raw |
| week_high_52_paise | 144 | int64 LE | ÷100 → INR |
| week_low_52_paise | 152 | int64 LE | ÷100 → INR |
| upper_circuit_paise | 160 | int64 LE | ÷100 → INR |
| lower_circuit_paise | 168 | int64 LE | ÷100 → INR |
| depth entries | 176+ | repeated struct | 5 buy levels + 5 sell levels |

### Exchange type constants

| Constant | Value | Instruments |
|----------|-------|------------|
| `EXCHANGE_TYPE_NSE_EQ` | 1 | NSE equities |
| `EXCHANGE_TYPE_NFO` | 2 | NSE F&O — options and futures |
| `EXCHANGE_TYPE_BSE_EQ` | 3 | BSE equities |
| `EXCHANGE_TYPE_MCX` | 5 | MCX commodities |

### NFO (F&O) subscription

Subscribe call at `angel_one_stream.py:306-311` demonstrates:
```python
{"exchange_type": EXCHANGE_TYPE_NFO, "tokens": ["43985", "43986"]}
```
This is the mechanism by which NIFTY options / futures would be subscribed.

---

## UNIT TEST COVERAGE

| Test | File | What it tests |
|------|------|--------------|
| LTP mode decode | `test_angel_one_stream_binary.py` | Synthetic 44-byte frame; verifies ltp, exchange_type, token |
| QUOTE mode decode | Same | Synthetic 120-byte frame; verifies O/H/L/C, vol, ATP |
| FULL mode decode | Same | Synthetic 168+ byte frame; verifies OI at offset 128, depth |
| NFO exchange type | Same | exchange_type=2 round-trips correctly |
| Paise→INR conversion | Same | Divide-by-100 applied to all price fields |
| Reconnect + resubscribe | Same | token_groups replayed after disconnect |

All tests pass (confirmed 2026-09-17 — 4,397 total tests, 0 failures).

---

## WHAT REQUIRES LIVE VERIFICATION

The following cannot be confirmed without a real SmartStream V2 connection during market hours:

### Critical: byte offset accuracy

The byte offsets in the table above are derived from Angel One's SmartStream V2 documentation. They have **not been validated against a real binary frame**. Angel One does not publish a formal binary schema file (no `.proto`, no `.bin` descriptor). If the documented offsets are incorrect, the decoded values will be silently wrong.

Specific fields most at risk:
- OI at offset 128 — if any undocumented padding or new field was added between OHLC and OI, OI will be read from the wrong bytes
- Depth fields at 176+ — variable-length structures are especially prone to offset drift
- `exchange_ts_ms` at 32 — timestamp correctness must be validated against REST quote `exchFeedTime`

### Required live test procedure

1. Connect to `wss://smartapisocket.angelone.in/smart-stream` with real JWT + feedToken
2. Subscribe with `exchange_type=2` (NFO) to at minimum: one NIFTY option, one BANKNIFTY option, one NIFTY future
3. Subscribe with `exchange_type=1` (NSE EQ) to RELIANCE and HDFCBANK
4. Subscribe in mode=3 (FULL) to capture OI and depth
5. Receive ≥100 valid frames per subscribed exchange type (≥100 NFO ticks, ≥100 NSE_EQ ticks)
6. Simultaneously call `fetch_live_quote()` (FULL REST) for the same tokens
7. Compare decoded binary LTP against REST LTP for each tick (tolerance: ≤0.5% or 1 paise)
8. Compare decoded binary OI against REST `opnInterest` (tolerance: exact match or ±1 contract rounding)
9. Verify depth buy[0].price matches REST depth.buy[0].price within 1 paise
10. Record: frames_received, frames_decoded_successfully, frames_failed, offset_mismatches

### Minimum certification thresholds

| Metric | Required for `LRV` |
|--------|-------------------|
| Real frames received per mode | ≥100 LTP, ≥100 QUOTE, ≥100 FULL |
| NFO frames received | ≥100 |
| Decode success rate | 100% (0 failures on certification sample) |
| LTP vs REST deviation | ≤1 paise on ≥95% of ticks |
| OI vs REST deviation | Exact match on ≥95% of ticks |
| Depth price[0] deviation | ≤1 paise on ≥95% of ticks |

---

## CURRENT GAP SUMMARY

| Item | Status | Blocker |
|------|--------|---------|
| Binary decode code exists | ✅ | — |
| NFO exchange type implemented | ✅ | — |
| All 3 modes implemented | ✅ | — |
| Unit tests pass | ✅ | — |
| feedToken from live getProfile | ❌ `NVL` | Requires market-hours connection |
| ≥100 real frames received (NSE EQ) | ❌ `NVL` | Requires market-hours connection |
| ≥100 real frames received (NFO) | ❌ `NVL` | Requires market-hours connection |
| OI offset confirmed vs REST | ❌ `NVL` | Not validated |
| Depth offset confirmed vs REST | ❌ `NVL` | Not validated |
| Timestamp offset confirmed | ❌ `NVL` | Not validated |

---

## CERTIFICATION CONCLUSION

```
ANGELONE_SMARTSTREAM_BINARY_CERTIFICATION: NOT_CERTIFIED
```

The implementation is structurally correct based on documented byte offsets. It passes all synthetic unit tests. It cannot be certified until ≥100 real binary frames have been received and their decoded values compared against REST quotes at the same timestamp. Run `scripts/test_angel_live.py` extended with SmartStream connection during market hours (09:15–15:30 IST on a trading day) to obtain this evidence.

---

*No live SmartStream frames were captured for this report. All offset tables are from Angel One documentation.*  
*Sample counts: 0 real frames. This report will be updated when live certification is achieved.*
