"""
tests/unit/backtest/test_point_in_time.py

Unit tests for src/backtest/point_in_time.py — PointInTimeFilter,
BacktestContext, and the backtest_context FastAPI dependency.

Coverage:
  PointInTimeFilter.filter
    - records with time <= as_of_ts are included
    - records with time > as_of_ts are excluded
    - empty input returns empty list
    - records missing the time field are excluded
    - records with an invalid (non-integer) time field are excluded
    - available_at_offset_ms is applied correctly
    - negative offset raises ValueError
    - mixed valid/invalid records only return valid ones

  PointInTimeFilter.filter_by_available_at
    - records with availableAtMs <= as_of_ts are included
    - records with availableAtMs > as_of_ts are excluded
    - records missing availableAtMs are rejected (Requirement 23.5)
    - records with invalid availableAtMs are rejected (Requirement 23.5)
    - empty input returns empty list
    - all-rejected input returns empty list
    - partial field absence (some records have it, some don't)

  PointInTimeFilter.is_look_ahead
    - record is look-ahead when availableAtMs > as_of_ts
    - record is not look-ahead when availableAtMs == as_of_ts
    - record is not look-ahead when availableAtMs < as_of_ts
    - falls back to time field when availableAtMs absent
    - time-based: look-ahead when time > as_of_ts
    - time-based: not look-ahead when time <= as_of_ts
    - returns True when both availableAtMs and time are absent
    - invalid availableAtMs → treated as look-ahead
    - invalid time field (absent availableAtMs) → treated as look-ahead

  BacktestContext
    - valid construction with mode BACKTEST
    - valid construction with mode LIVE
    - as_of_ts must be > 0 (Pydantic validation)
    - mode must be BACKTEST or LIVE (Pydantic Literal)

  backtest_context FastAPI dependency
    - returns BacktestContext with mode=BACKTEST when as_of is supplied
    - returns BacktestContext with mode=LIVE when as_of is None
    - LIVE mode as_of_ts is a positive integer (current wall-clock ms)

Requirements: 23.4, 23.5
"""

from __future__ import annotations

import time as _time
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.backtest.point_in_time import (
    BacktestContext,
    PointInTimeFilter,
    backtest_context,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A fixed reference "now" expressed in epoch-ms.
_NOW_MS: int = 1_700_000_000_000  # 2023-11-14 ~22:13 UTC


def _record(
    time: int | None = None,
    available_at: int | None = None,
    instrument_id: str = "NSE:RELIANCE",
    **extra,
) -> dict:
    """Build a minimal OHLCV record dict for test use."""
    r: dict = {
        "instrumentId": instrument_id,
        "open": 100.0,
        "high": 105.0,
        "low": 98.0,
        "close": 103.0,
        "volume": 500,
    }
    if time is not None:
        r["time"] = time
    if available_at is not None:
        r["availableAtMs"] = available_at
    r.update(extra)
    return r


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pit() -> PointInTimeFilter:
    return PointInTimeFilter()


# ===========================================================================
# Section 1: PointInTimeFilter.filter — time-based filter
# ===========================================================================


class TestFilter:
    """filter() uses the ``time`` field with an optional latency offset."""

    def test_record_at_boundary_is_included(self, pit: PointInTimeFilter) -> None:
        """A record whose time == as_of_ts is included (boundary inclusive)."""
        records = [_record(time=_NOW_MS)]
        result = pit.filter(records, as_of_ts=_NOW_MS)
        assert len(result) == 1

    def test_record_before_boundary_is_included(self, pit: PointInTimeFilter) -> None:
        """A record whose time < as_of_ts is included."""
        records = [_record(time=_NOW_MS - 1)]
        result = pit.filter(records, as_of_ts=_NOW_MS)
        assert len(result) == 1

    def test_record_after_boundary_is_excluded(self, pit: PointInTimeFilter) -> None:
        """A record whose time > as_of_ts is excluded (look-ahead bias prevention)."""
        records = [_record(time=_NOW_MS + 1)]
        result = pit.filter(records, as_of_ts=_NOW_MS)
        assert len(result) == 0

    def test_empty_input_returns_empty(self, pit: PointInTimeFilter) -> None:
        assert pit.filter([], as_of_ts=_NOW_MS) == []

    def test_record_missing_time_is_excluded(self, pit: PointInTimeFilter) -> None:
        """Records without a ``time`` key are excluded conservatively."""
        records = [_record()]  # no time kwarg → time key absent
        result = pit.filter(records, as_of_ts=_NOW_MS)
        assert len(result) == 0

    def test_record_with_none_time_is_excluded(self, pit: PointInTimeFilter) -> None:
        records = [_record(time=None)]
        # _record sets time=None explicitly when passed — but our helper only
        # inserts the key when the value is not None; call directly instead.
        r = {"instrumentId": "X", "time": None, "open": 1.0}
        result = pit.filter([r], as_of_ts=_NOW_MS)
        assert len(result) == 0

    def test_record_with_invalid_time_is_excluded(self, pit: PointInTimeFilter) -> None:
        """Records with a non-integer ``time`` (e.g. string junk) are excluded."""
        r = {"time": "not-a-number", "open": 1.0}
        result = pit.filter([r], as_of_ts=_NOW_MS)
        assert len(result) == 0

    def test_offset_shifts_effective_timestamp(self, pit: PointInTimeFilter) -> None:
        """available_at_offset_ms shifts the effective comparison timestamp."""
        # Record at NOW - 100ms; as_of = NOW; offset = 200ms.
        # effective_ts = (NOW - 100) + 200 = NOW + 100 > as_of → excluded.
        records = [_record(time=_NOW_MS - 100)]
        result = pit.filter(records, as_of_ts=_NOW_MS, available_at_offset_ms=200)
        assert len(result) == 0

    def test_zero_offset_is_default_behaviour(self, pit: PointInTimeFilter) -> None:
        """Default offset is 0 — matches calling filter without the kwarg."""
        records = [_record(time=_NOW_MS)]
        without_kwarg = pit.filter(records, as_of_ts=_NOW_MS)
        with_zero = pit.filter(records, as_of_ts=_NOW_MS, available_at_offset_ms=0)
        assert without_kwarg == with_zero

    def test_negative_offset_raises(self, pit: PointInTimeFilter) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            pit.filter([], as_of_ts=_NOW_MS, available_at_offset_ms=-1)

    def test_mixed_records_only_past_included(self, pit: PointInTimeFilter) -> None:
        """Only records at or before as_of_ts are returned from a mixed list."""
        records = [
            _record(time=_NOW_MS - 2000),  # included
            _record(time=_NOW_MS),          # included (boundary)
            _record(time=_NOW_MS + 1),      # excluded
            _record(time=_NOW_MS + 5000),   # excluded
        ]
        result = pit.filter(records, as_of_ts=_NOW_MS)
        assert len(result) == 2

    def test_all_records_past_returns_all(self, pit: PointInTimeFilter) -> None:
        records = [_record(time=_NOW_MS - i * 1000) for i in range(1, 6)]
        result = pit.filter(records, as_of_ts=_NOW_MS)
        assert len(result) == 5

    def test_all_records_future_returns_none(self, pit: PointInTimeFilter) -> None:
        records = [_record(time=_NOW_MS + i * 1000) for i in range(1, 6)]
        result = pit.filter(records, as_of_ts=_NOW_MS)
        assert len(result) == 0

    def test_original_list_not_mutated(self, pit: PointInTimeFilter) -> None:
        """filter() must not modify the input list."""
        records = [_record(time=_NOW_MS), _record(time=_NOW_MS + 1)]
        original_len = len(records)
        pit.filter(records, as_of_ts=_NOW_MS)
        assert len(records) == original_len

    def test_order_preserved(self, pit: PointInTimeFilter) -> None:
        """Records that pass the filter appear in the same order as input."""
        t_values = [_NOW_MS - 3000, _NOW_MS - 2000, _NOW_MS - 1000, _NOW_MS]
        records = [_record(time=t) for t in t_values]
        result = pit.filter(records, as_of_ts=_NOW_MS)
        assert [r["time"] for r in result] == t_values


# ===========================================================================
# Section 2: PointInTimeFilter.filter_by_available_at — provenance filter
# ===========================================================================


class TestFilterByAvailableAt:
    """filter_by_available_at() uses the ``availableAtMs`` provenance field."""

    def test_record_at_boundary_is_included(self, pit: PointInTimeFilter) -> None:
        records = [_record(available_at=_NOW_MS)]
        assert len(pit.filter_by_available_at(records, _NOW_MS)) == 1

    def test_record_before_boundary_is_included(self, pit: PointInTimeFilter) -> None:
        records = [_record(available_at=_NOW_MS - 1)]
        assert len(pit.filter_by_available_at(records, _NOW_MS)) == 1

    def test_record_after_boundary_is_excluded(self, pit: PointInTimeFilter) -> None:
        """Records whose availableAtMs > as_of_ts must be excluded."""
        records = [_record(available_at=_NOW_MS + 1)]
        assert pit.filter_by_available_at(records, _NOW_MS) == []

    def test_empty_input_returns_empty(self, pit: PointInTimeFilter) -> None:
        assert pit.filter_by_available_at([], _NOW_MS) == []

    def test_record_missing_available_at_ms_is_rejected(
        self, pit: PointInTimeFilter
    ) -> None:
        """Records without availableAtMs are rejected per Requirement 23.5."""
        records = [_record(time=_NOW_MS - 5000)]  # has time but no availableAtMs
        result = pit.filter_by_available_at(records, _NOW_MS)
        assert result == [], (
            "Record missing availableAtMs must be rejected from backtest dataset"
        )

    def test_record_with_none_available_at_ms_is_rejected(
        self, pit: PointInTimeFilter
    ) -> None:
        r = {"instrumentId": "X", "time": _NOW_MS, "availableAtMs": None}
        result = pit.filter_by_available_at([r], _NOW_MS)
        assert result == []

    def test_record_with_invalid_available_at_ms_is_rejected(
        self, pit: PointInTimeFilter
    ) -> None:
        """Non-integer availableAtMs cannot be parsed → rejected per Requirement 23.5."""
        r = {"instrumentId": "X", "availableAtMs": "not-an-integer"}
        result = pit.filter_by_available_at([r], _NOW_MS)
        assert result == []

    def test_all_rejected_returns_empty_list(self, pit: PointInTimeFilter) -> None:
        records = [_record() for _ in range(5)]  # no availableAtMs in any
        result = pit.filter_by_available_at(records, _NOW_MS)
        assert result == []

    def test_mixed_valid_and_missing_available_at(self, pit: PointInTimeFilter) -> None:
        """Only records with valid availableAtMs <= as_of_ts pass."""
        records = [
            _record(available_at=_NOW_MS - 1000),   # pass
            _record(available_at=_NOW_MS),            # pass (boundary)
            _record(available_at=_NOW_MS + 500),      # fail (future)
            _record(),                                # fail (missing)
        ]
        result = pit.filter_by_available_at(records, _NOW_MS)
        assert len(result) == 2
        assert all(r["availableAtMs"] <= _NOW_MS for r in result)

    def test_order_preserved_after_filter(self, pit: PointInTimeFilter) -> None:
        at_values = [_NOW_MS - 3000, _NOW_MS - 2000, _NOW_MS - 1000, _NOW_MS]
        records = [_record(available_at=t) for t in at_values]
        result = pit.filter_by_available_at(records, _NOW_MS)
        assert [r["availableAtMs"] for r in result] == at_values

    def test_original_list_not_mutated(self, pit: PointInTimeFilter) -> None:
        records = [_record(available_at=_NOW_MS), _record(available_at=_NOW_MS + 1)]
        original = list(records)
        pit.filter_by_available_at(records, _NOW_MS)
        assert records == original

    def test_filter_by_available_at_ignores_time_field(
        self, pit: PointInTimeFilter
    ) -> None:
        """filter_by_available_at uses availableAtMs even if time field differs."""
        # time says it's in the past (would pass filter), but availableAtMs is
        # in the future (should fail filter_by_available_at).
        r = _record(time=_NOW_MS - 5000, available_at=_NOW_MS + 5000)
        result = pit.filter_by_available_at([r], _NOW_MS)
        assert result == [], (
            "filter_by_available_at must use availableAtMs, not time"
        )

    def test_records_with_large_available_at_excluded(
        self, pit: PointInTimeFilter
    ) -> None:
        """Records from the far future are excluded regardless of time field."""
        far_future = _NOW_MS + 10_000_000_000  # ~115 days ahead
        records = [_record(available_at=far_future)]
        assert pit.filter_by_available_at(records, _NOW_MS) == []


# ===========================================================================
# Section 3: PointInTimeFilter.is_look_ahead
# ===========================================================================


class TestIsLookAhead:
    """is_look_ahead() identifies whether a single record would be future data."""

    def test_not_look_ahead_when_available_at_before_boundary(
        self, pit: PointInTimeFilter
    ) -> None:
        r = _record(available_at=_NOW_MS - 1)
        assert pit.is_look_ahead(r, _NOW_MS) is False

    def test_not_look_ahead_when_available_at_equals_boundary(
        self, pit: PointInTimeFilter
    ) -> None:
        r = _record(available_at=_NOW_MS)
        assert pit.is_look_ahead(r, _NOW_MS) is False

    def test_look_ahead_when_available_at_after_boundary(
        self, pit: PointInTimeFilter
    ) -> None:
        r = _record(available_at=_NOW_MS + 1)
        assert pit.is_look_ahead(r, _NOW_MS) is True

    def test_falls_back_to_time_field_when_available_at_absent(
        self, pit: PointInTimeFilter
    ) -> None:
        """When availableAtMs is absent, time is used as fallback."""
        r = _record(time=_NOW_MS)
        assert pit.is_look_ahead(r, _NOW_MS) is False

    def test_time_fallback_look_ahead_when_time_after_boundary(
        self, pit: PointInTimeFilter
    ) -> None:
        r = _record(time=_NOW_MS + 1)
        assert pit.is_look_ahead(r, _NOW_MS) is True

    def test_time_fallback_not_look_ahead_when_time_before_boundary(
        self, pit: PointInTimeFilter
    ) -> None:
        r = _record(time=_NOW_MS - 1000)
        assert pit.is_look_ahead(r, _NOW_MS) is False

    def test_true_when_both_fields_absent(self, pit: PointInTimeFilter) -> None:
        """No temporal anchor → treat as look-ahead (unsafe to include)."""
        r = {"instrumentId": "X", "open": 1.0}
        assert pit.is_look_ahead(r, _NOW_MS) is True

    def test_invalid_available_at_treated_as_look_ahead(
        self, pit: PointInTimeFilter
    ) -> None:
        """Unparseable availableAtMs → look-ahead (safe default)."""
        r = {"availableAtMs": "bad-value", "time": _NOW_MS - 1000}
        assert pit.is_look_ahead(r, _NOW_MS) is True

    def test_invalid_time_treated_as_look_ahead_when_available_at_absent(
        self, pit: PointInTimeFilter
    ) -> None:
        r = {"time": "bad-value"}
        assert pit.is_look_ahead(r, _NOW_MS) is True

    def test_available_at_takes_precedence_over_time(
        self, pit: PointInTimeFilter
    ) -> None:
        """availableAtMs is used even when time says it would be safe."""
        # time in the past (would be safe), but availableAtMs is in the future.
        r = _record(time=_NOW_MS - 5000, available_at=_NOW_MS + 5000)
        assert pit.is_look_ahead(r, _NOW_MS) is True

    def test_available_at_zero_not_look_ahead_for_any_positive_boundary(
        self, pit: PointInTimeFilter
    ) -> None:
        """availableAtMs=0 is always in the past relative to any positive as_of_ts."""
        r = _record(available_at=0)
        assert pit.is_look_ahead(r, 1) is False


# ===========================================================================
# Section 4: BacktestContext model validation
# ===========================================================================


class TestBacktestContext:
    """BacktestContext Pydantic v2 model construction and validation."""

    def test_valid_backtest_context(self) -> None:
        ctx = BacktestContext(as_of_ts=_NOW_MS, mode="BACKTEST")
        assert ctx.as_of_ts == _NOW_MS
        assert ctx.mode == "BACKTEST"

    def test_valid_live_context(self) -> None:
        ctx = BacktestContext(as_of_ts=_NOW_MS, mode="LIVE")
        assert ctx.mode == "LIVE"

    def test_default_mode_is_live(self) -> None:
        """mode defaults to LIVE when not supplied."""
        ctx = BacktestContext(as_of_ts=_NOW_MS)
        assert ctx.mode == "LIVE"

    def test_as_of_ts_must_be_positive(self) -> None:
        """as_of_ts must be > 0 (gt=0 constraint)."""
        with pytest.raises(ValidationError):
            BacktestContext(as_of_ts=0, mode="BACKTEST")

    def test_negative_as_of_ts_raises(self) -> None:
        with pytest.raises(ValidationError):
            BacktestContext(as_of_ts=-1, mode="BACKTEST")

    def test_invalid_mode_raises(self) -> None:
        """Mode must be BACKTEST or LIVE — anything else is rejected."""
        with pytest.raises(ValidationError):
            BacktestContext(as_of_ts=_NOW_MS, mode="REPLAY")  # type: ignore[arg-type]

    def test_mode_case_sensitive(self) -> None:
        """Lowercase mode values are not accepted."""
        with pytest.raises(ValidationError):
            BacktestContext(as_of_ts=_NOW_MS, mode="backtest")  # type: ignore[arg-type]

    def test_large_as_of_ts_accepted(self) -> None:
        """Large epoch-ms values (well into the future) are valid."""
        far_future_ms = 9_999_999_999_000
        ctx = BacktestContext(as_of_ts=far_future_ms, mode="BACKTEST")
        assert ctx.as_of_ts == far_future_ms


# ===========================================================================
# Section 5: backtest_context FastAPI dependency
# ===========================================================================


class TestBacktestContextDependency:
    """backtest_context() dependency function creates the correct context."""

    def test_returns_backtest_mode_when_as_of_supplied(self) -> None:
        ctx = backtest_context(as_of=_NOW_MS)
        assert ctx.mode == "BACKTEST"
        assert ctx.as_of_ts == _NOW_MS

    def test_returns_live_mode_when_as_of_is_none(self) -> None:
        ctx = backtest_context(as_of=None)
        assert ctx.mode == "LIVE"

    def test_live_mode_as_of_ts_is_positive(self) -> None:
        """LIVE mode must still produce a positive as_of_ts (current wall-clock ms)."""
        ctx = backtest_context(as_of=None)
        assert ctx.as_of_ts > 0

    def test_live_mode_as_of_ts_approximates_now(self) -> None:
        """LIVE context as_of_ts should be within 5 seconds of the system clock."""
        before = int(_time.time() * 1000)
        ctx = backtest_context(as_of=None)
        after = int(_time.time() * 1000)
        assert before <= ctx.as_of_ts <= after + 5000, (
            f"LIVE context as_of_ts {ctx.as_of_ts} is not near the current time"
        )

    def test_backtest_as_of_ts_matches_query_param(self) -> None:
        """The supplied as_of value must be used verbatim as as_of_ts."""
        specific_ts = 1_700_123_456_789
        ctx = backtest_context(as_of=specific_ts)
        assert ctx.as_of_ts == specific_ts

    def test_multiple_calls_with_none_produce_live_mode(self) -> None:
        """Calling with as_of=None always returns mode=LIVE."""
        for _ in range(3):
            ctx = backtest_context(as_of=None)
            assert ctx.mode == "LIVE"

    def test_multiple_calls_with_as_of_produce_backtest_mode(self) -> None:
        """Calling with various as_of values always returns mode=BACKTEST."""
        timestamps = [_NOW_MS, _NOW_MS - 1000, _NOW_MS + 1000]
        for ts in timestamps:
            ctx = backtest_context(as_of=ts)
            assert ctx.mode == "BACKTEST"
            assert ctx.as_of_ts == ts


# ===========================================================================
# Section 6: Integration — filter_by_available_at enforces Requirement 23.4
# ===========================================================================


class TestLookAheadBiasPrevention:
    """End-to-end scenarios demonstrating Requirement 23.4 compliance."""

    def test_no_future_records_leak_into_backtest_at_time_T(
        self, pit: PointInTimeFilter
    ) -> None:
        """All records returned by filter_by_available_at satisfy availableAtMs <= T."""
        T = _NOW_MS
        records = [
            _record(available_at=T - 60_000),     # 1 min before T — safe
            _record(available_at=T - 1),           # 1 ms before T — safe
            _record(available_at=T),               # exactly T — safe (boundary)
            _record(available_at=T + 1),           # 1 ms after T — look-ahead
            _record(available_at=T + 60_000),      # 1 min after T — look-ahead
        ]
        result = pit.filter_by_available_at(records, T)
        assert len(result) == 3
        for rec in result:
            assert rec["availableAtMs"] <= T, (
                f"Look-ahead record leaked into backtest result: {rec}"
            )

    def test_records_missing_provenance_never_appear_in_backtest(
        self, pit: PointInTimeFilter
    ) -> None:
        """Records without availableAtMs are always excluded (Requirement 23.5)."""
        T = _NOW_MS
        records = [
            _record(time=T - 10_000),  # has time but no availableAtMs
            _record(time=T),            # has time but no availableAtMs
        ]
        result = pit.filter_by_available_at(records, T)
        assert result == [], "Records missing availableAtMs must never appear in backtest"

    def test_is_look_ahead_consistent_with_filter_by_available_at(
        self, pit: PointInTimeFilter
    ) -> None:
        """is_look_ahead and filter_by_available_at must agree for each record."""
        T = _NOW_MS
        records = [
            _record(available_at=T - 1000),
            _record(available_at=T),
            _record(available_at=T + 1000),
        ]
        filtered = set(id(r) for r in pit.filter_by_available_at(records, T))
        for rec in records:
            in_result = id(rec) in filtered
            look_ahead = pit.is_look_ahead(rec, T)
            # A record is in the result iff it is NOT a look-ahead.
            assert in_result != look_ahead, (
                f"Inconsistency for record availableAtMs={rec['availableAtMs']}: "
                f"in_result={in_result}, is_look_ahead={look_ahead}"
            )
