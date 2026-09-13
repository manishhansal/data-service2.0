"""
Property-based tests for OHLCV candle invariants and the PointInTimeFilter.

# Feature: data-service-platform, Property 1: OHLCV Candle Invariants

**Validates: Requirements 4.3, 13.8, 23.4**

Properties tested
-----------------
1. **OHLCV Consistency** — for any valid candle, the OHLC invariants hold
   after the normaliser processes it:
     * high >= max(open, close)
     * low  <= min(open, close)
     * all prices > 0
     * volume >= 0

2. **Rejecting invalid candles** — for any candle whose constructed data
   violates the OHLC invariants, ``BinanceOHLCVNormaliser.normalise()``
   must return ``None`` (Requirement 13.8 / 13.10).

3. **No mutation** — ``PointInTimeFilter.filter()`` must never mutate the
   original input list (defensive correctness invariant).

4. **Monotonicity of filter** — if ``as_of_ts`` is increased, the set of
   returned records can only grow; it never shrinks (Requirement 23.4).

5. **Round-trip property** — for any valid ``BinanceCandleRecord``,
   serialising with ``model_dump()`` and reconstructing via the model
   constructor produces an equal record (Pydantic v2 round-trip).

Requirements: 4.3, 13.8, 23.4
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.backtest.point_in_time import PointInTimeFilter
from src.providers.adapters.binance_rest import BINANCE_INTERVALS
from src.providers.binance_normaliser import BinanceCandleRecord, BinanceOHLCVNormaliser

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NORMALISER = BinanceOHLCVNormaliser()
_PIT_FILTER = PointInTimeFilter()

# Representative set of valid Binance intervals used in generators.
_VALID_INTERVALS = sorted(BINANCE_INTERVALS)

# Epoch-ms boundaries: 2020-01-01 00:00:00 UTC → 2030-01-01 00:00:00 UTC
_MIN_EPOCH_MS: int = 1_577_836_800_000
_MAX_EPOCH_MS: int = 1_893_456_000_000

# ---------------------------------------------------------------------------
# Shared Hypothesis strategies
# ---------------------------------------------------------------------------

# A strictly positive price (> 0.0).  Use realistic market-data ranges to
# keep generated examples fast; NaN/Inf are excluded because the normaliser
# rejects them via Python's float → positive-float coercion.
_positive_price = st.floats(
    min_value=0.0001,
    max_value=1_000_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Non-negative volume (includes zero, which is valid for zero-volume bars).
_non_negative_volume = st.floats(
    min_value=0.0,
    max_value=1_000_000.0,
    allow_nan=False,
    allow_infinity=False,
)

# Positive epoch-ms timestamps in a realistic range.
_epoch_ms = st.integers(min_value=_MIN_EPOCH_MS, max_value=_MAX_EPOCH_MS)


@st.composite
def valid_ohlcv_raw(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a raw kline dict that satisfies all OHLC invariants.

    Constructs prices in a way that guarantees the constraints:
      * Base price ``p`` is drawn first.
      * ``open`` and ``close`` are derived from ``p`` with small deltas.
      * ``high = max(open, close) + margin`` (≥ both).
      * ``low  = min(open, close) - margin`` (≤ both).
      * ``volume`` is non-negative.
      * ``time < closeTime`` to mimic a real candle.
    """
    p = draw(_positive_price)

    # Small relative deltas to build realistic O/C spread
    delta_open = draw(st.floats(min_value=0.0, max_value=p * 0.05, allow_nan=False, allow_infinity=False))
    delta_close = draw(st.floats(min_value=0.0, max_value=p * 0.05, allow_nan=False, allow_infinity=False))
    open_price = p + delta_open
    close_price = p + delta_close

    oc_max = max(open_price, close_price)
    oc_min = min(open_price, close_price)

    high_margin = draw(st.floats(min_value=0.0, max_value=p * 0.10, allow_nan=False, allow_infinity=False))
    low_margin = draw(st.floats(min_value=0.0, max_value=p * 0.10, allow_nan=False, allow_infinity=False))

    high_price = oc_max + high_margin
    low_price = oc_min - low_margin

    # Ensure low stays positive after subtracting the margin.
    assume(low_price > 0.0)

    volume = draw(_non_negative_volume)
    time_ms = draw(_epoch_ms)
    # closeTime must be after openTime (at least 1 ms later)
    close_time_ms = draw(st.integers(min_value=time_ms + 1, max_value=time_ms + 86_400_000))

    return {
        "time":      time_ms,
        "open":      open_price,
        "high":      high_price,
        "low":       low_price,
        "close":     close_price,
        "volume":    volume,
        "closeTime": close_time_ms,
    }


@st.composite
def invalid_ohlcv_raw(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a raw kline dict with at least one OHLC invariant violation.

    Three violation types are generated with equal probability:
      A) high < open   (high below opening price)
      B) high < close  (high below closing price)
      C) low  > close  (low above closing price)
    """
    violation = draw(st.sampled_from(["high_below_open", "high_below_close", "low_above_close"]))

    p = draw(st.floats(min_value=10.0, max_value=100_000.0, allow_nan=False, allow_infinity=False))
    open_price = p
    close_price = draw(st.floats(min_value=p * 0.9, max_value=p * 1.1, allow_nan=False, allow_infinity=False))
    assume(open_price > 0.0 and close_price > 0.0)

    oc_max = max(open_price, close_price)
    oc_min = min(open_price, close_price)

    if violation == "high_below_open":
        # high is strictly below open
        high_price = draw(st.floats(min_value=0.0001, max_value=open_price * 0.99, allow_nan=False, allow_infinity=False))
        assume(high_price > 0.0 and high_price < open_price)
        low_price = draw(st.floats(min_value=0.0001, max_value=oc_min, allow_nan=False, allow_infinity=False))
        assume(low_price > 0.0)
    elif violation == "high_below_close":
        # high is strictly below close
        high_price = draw(st.floats(min_value=0.0001, max_value=close_price * 0.99, allow_nan=False, allow_infinity=False))
        assume(high_price > 0.0 and high_price < close_price)
        low_price = draw(st.floats(min_value=0.0001, max_value=oc_min, allow_nan=False, allow_infinity=False))
        assume(low_price > 0.0)
    else:  # low_above_close
        # low is strictly above close
        high_price = oc_max + draw(st.floats(min_value=0.0, max_value=p * 0.10, allow_nan=False, allow_infinity=False))
        low_price = draw(st.floats(min_value=close_price * 1.001, max_value=close_price * 1.10, allow_nan=False, allow_infinity=False))
        assume(low_price > close_price)

    volume = draw(_non_negative_volume)
    time_ms = draw(_epoch_ms)
    close_time_ms = time_ms + 60_000

    return {
        "time":      time_ms,
        "open":      open_price,
        "high":      high_price,
        "low":       low_price,
        "close":     close_price,
        "volume":    volume,
        "closeTime": close_time_ms,
    }


@st.composite
def ohlcv_record_dict(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a raw dict suitable for building a PointInTimeFilter record."""
    time_ms = draw(_epoch_ms)
    available_at_ms = draw(st.integers(min_value=time_ms, max_value=time_ms + 5_000))
    return {
        "time":          time_ms,
        "availableAtMs": available_at_ms,
        "symbol":        draw(st.sampled_from(["BTCUSDT", "ETHUSDT", "SOLUSDT"])),
        "open":          draw(_positive_price),
        "close":         draw(_positive_price),
    }


# ---------------------------------------------------------------------------
# Helper to build a valid BinanceCandleRecord kwargs dict
# ---------------------------------------------------------------------------

@st.composite
def valid_candle_record_kwargs(draw: st.DrawFn) -> dict[str, Any]:
    """Generate keyword arguments for a valid BinanceCandleRecord."""
    p = draw(st.floats(min_value=0.01, max_value=500_000.0, allow_nan=False, allow_infinity=False))
    assume(p > 0)

    delta_oc = draw(st.floats(min_value=0.0, max_value=p * 0.05, allow_nan=False, allow_infinity=False))
    open_price = p
    close_price = p + delta_oc

    oc_max = max(open_price, close_price)
    oc_min = min(open_price, close_price)

    high_margin = draw(st.floats(min_value=0.0, max_value=p * 0.10, allow_nan=False, allow_infinity=False))
    low_margin = draw(st.floats(min_value=0.0, max_value=oc_min * 0.05, allow_nan=False, allow_infinity=False))

    high_price = oc_max + high_margin
    low_price = oc_min - low_margin
    assume(low_price > 0.0)

    time_ms = draw(_epoch_ms)
    close_time_ms = time_ms + draw(st.integers(min_value=1, max_value=86_400_000))
    volume = draw(_non_negative_volume)
    interval = draw(st.sampled_from(_VALID_INTERVALS))
    symbol = draw(st.sampled_from(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BTCBUSD"]))

    return {
        "symbol":    symbol,
        "interval":  interval,
        "time":      time_ms,
        "open":      open_price,
        "high":      high_price,
        "low":       low_price,
        "close":     close_price,
        "volume":    volume,
        "closeTime": close_time_ms,
    }


# ===========================================================================
# Property 1 — OHLCV Consistency
#
# For any raw candle that satisfies OHLC invariants, the normaliser must
# produce a non-None record whose fields obey those same invariants.
#
# Validates: Requirements 4.3, 13.8
# ===========================================================================

@pytest.mark.property
@settings(max_examples=100)
@given(raw=valid_ohlcv_raw(), interval=st.sampled_from(_VALID_INTERVALS))
def test_property1_ohlcv_consistency(raw: dict[str, Any], interval: str) -> None:
    """Property 1 — OHLCV Consistency.

    For any valid OHLCV candle, the normaliser must:
      * Return a non-None record.
      * Guarantee high >= max(open, close).
      * Guarantee low  <= min(open, close).
      * Guarantee all prices > 0.
      * Guarantee volume >= 0.

    **Validates: Requirements 4.3, 13.8**
    """
    record = _NORMALISER.normalise(raw, symbol="BTCUSDT", interval=interval)

    # The raw data was constructed to be valid, so we must get a record back.
    assert record is not None, (
        f"normalise() returned None for a valid candle: {raw!r}, interval={interval!r}"
    )

    # Invariant 1: high >= max(open, close)
    assert record.high >= max(record.open, record.close), (
        f"high({record.high}) < max(open={record.open}, close={record.close})"
    )

    # Invariant 2: low <= min(open, close)
    assert record.low <= min(record.open, record.close), (
        f"low({record.low}) > min(open={record.open}, close={record.close})"
    )

    # Invariant 3: all prices strictly positive
    for field_name, value in [
        ("open",  record.open),
        ("high",  record.high),
        ("low",   record.low),
        ("close", record.close),
    ]:
        assert value > 0.0, f"{field_name}={value} is not > 0"

    # Invariant 4: volume non-negative
    assert record.volume >= 0.0, f"volume={record.volume} is negative"


# ===========================================================================
# Property 2 — Rejecting invalid candles
#
# For any candle whose OHLC data violates the invariants, normalise() must
# return None.
#
# Validates: Requirements 4.3, 13.10
# ===========================================================================

@pytest.mark.property
@settings(max_examples=100)
@given(raw=invalid_ohlcv_raw(), interval=st.sampled_from(_VALID_INTERVALS))
def test_property2_invalid_candle_rejected(raw: dict[str, Any], interval: str) -> None:
    """Property 2 — Rejecting invalid candles.

    For any candle where the OHLC invariants are violated,
    ``BinanceOHLCVNormaliser.normalise()`` must return ``None``.
    The caller is responsible for skipping ``None`` results (Requirement 13.10).

    **Validates: Requirements 4.3, 13.10**
    """
    result = _NORMALISER.normalise(raw, symbol="BTCUSDT", interval=interval)
    assert result is None, (
        f"normalise() returned a record instead of None for an invalid candle: "
        f"raw={raw!r}, interval={interval!r}"
    )


# ===========================================================================
# Property 3 — No mutation
#
# PointInTimeFilter.filter() must never mutate the input list or any
# of its contained dicts.
#
# Validates: Requirements 23.4 (filter must be pure / side-effect-free)
# ===========================================================================

@pytest.mark.property
@settings(max_examples=100)
@given(
    records=st.lists(ohlcv_record_dict(), min_size=0, max_size=20),
    as_of_ts=_epoch_ms,
)
def test_property3_no_mutation(records: list[dict], as_of_ts: int) -> None:
    """Property 3 — No mutation.

    ``PointInTimeFilter.filter()`` must never mutate the input list
    or any of the dicts it contains.

    **Validates: Requirements 23.4**
    """
    # Take a deep copy to compare against after the call.
    records_before = copy.deepcopy(records)

    _PIT_FILTER.filter(records, as_of_ts)

    # The original list must be identical (same length, same contents).
    assert len(records) == len(records_before), (
        "filter() changed the length of the input list"
    )
    for i, (original, after) in enumerate(zip(records_before, records)):
        assert original == after, (
            f"filter() mutated record at index {i}: before={original!r}, after={after!r}"
        )


# ===========================================================================
# Property 4 — Monotonicity of filter
#
# If as_of_ts is increased, the set of records returned by
# PointInTimeFilter.filter() can only grow — it never shrinks.
#
# Validates: Requirement 23.4
# ===========================================================================

@pytest.mark.property
@settings(max_examples=100)
@given(
    records=st.lists(ohlcv_record_dict(), min_size=1, max_size=20),
    as_of_ts_small=_epoch_ms,
    delta=st.integers(min_value=1, max_value=86_400_000),
)
def test_property4_filter_monotonicity(
    records: list[dict], as_of_ts_small: int, delta: int
) -> None:
    """Property 4 — Monotonicity of filter.

    Increasing ``as_of_ts`` can only add records to the result; it never
    removes previously included records.

    Formally: results(T) ⊆ results(T + delta) for all delta > 0.

    **Validates: Requirement 23.4**
    """
    as_of_ts_large = as_of_ts_small + delta

    smaller_result = _PIT_FILTER.filter(records, as_of_ts_small)
    larger_result  = _PIT_FILTER.filter(records, as_of_ts_large)

    # Every record returned for the smaller timestamp must also appear in
    # the larger timestamp's result.
    smaller_times = {r["time"] for r in smaller_result if "time" in r}
    larger_times  = {r["time"] for r in larger_result  if "time" in r}

    assert smaller_times <= larger_times, (
        f"Monotonicity violated: records present at as_of_ts={as_of_ts_small} "
        f"are missing at as_of_ts={as_of_ts_large}.\n"
        f"  Lost timestamps: {smaller_times - larger_times}"
    )

    # The result for the larger timestamp must be at least as large.
    assert len(larger_result) >= len(smaller_result), (
        f"filter() returned fewer records for a larger as_of_ts "
        f"({len(larger_result)} < {len(smaller_result)})"
    )


# ===========================================================================
# Property 5 — Round-trip property
#
# For any valid BinanceCandleRecord, serialising via model_dump() and
# reconstructing via the model constructor produces an equal record.
#
# Validates: Requirements 4.11, 17.8
# ===========================================================================

@pytest.mark.property
@settings(max_examples=100)
@given(kwargs=valid_candle_record_kwargs())
def test_property5_round_trip(kwargs: dict[str, Any]) -> None:
    """Property 5 — Round-trip property.

    ``parse(serialise(parse(raw))) == parse(raw)`` for all valid
    ``BinanceCandleRecord`` instances.

    Specifically: constructing a ``BinanceCandleRecord``, serialising with
    ``model_dump()``, and re-constructing from the dump must yield a record
    equal to the original.

    **Validates: Requirements 4.11, 17.8**
    """
    original = BinanceCandleRecord(**kwargs)

    # Serialise → reconstruct
    serialised = original.model_dump()
    reconstructed = BinanceCandleRecord(**serialised)

    # The two records must be equal.
    assert original == reconstructed, (
        f"Round-trip produced a different record.\n"
        f"  original:      {original!r}\n"
        f"  reconstructed: {reconstructed!r}"
    )

    # Spot-check that every field survived the round trip correctly.
    for field in ("symbol", "interval", "time", "open", "high", "low",
                  "close", "volume", "closeTime", "exchange", "poor_quality"):
        assert getattr(original, field) == getattr(reconstructed, field), (
            f"Field '{field}' differs after round trip: "
            f"{getattr(original, field)!r} != {getattr(reconstructed, field)!r}"
        )
