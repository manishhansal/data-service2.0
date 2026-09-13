"""
Property 13 — Look-Ahead Bias Prevention

For any backtest request at time T, no returned record has availableAtMs > T.
Records with availableAtMs > T must be excluded — they are "from the future"
relative to T.

Requirement: 23.4
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.backtest.point_in_time import PointInTimeFilter


# Epoch ms range: 2020-01-01 to 2030-01-01
EPOCH_MIN = 1_577_836_800_000
EPOCH_MAX = 1_893_456_000_000


@st.composite
def record_at_time(draw, backtest_t: int):
    """Generate a record with a random availableAtMs relative to backtest_t."""
    available_at = draw(st.integers(min_value=EPOCH_MIN, max_value=EPOCH_MAX))
    return {
        "availableAtMs": available_at,
        "symbol": "RELIANCE",
        "close": draw(st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)),
    }


class TestLookAheadBiasPrevention:
    @given(
        st.integers(min_value=EPOCH_MIN, max_value=EPOCH_MAX),  # backtest T
        st.lists(
            st.integers(min_value=EPOCH_MIN, max_value=EPOCH_MAX),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100)
    def test_no_future_records_returned(
        self,
        backtest_t_ms: int,
        record_times: list[int],
    ) -> None:
        """All returned records must have time <= backtest_t_ms."""
        records = [
            {"time": t, "symbol": "TEST", "close": 100.0}
            for t in record_times
        ]

        pit_filter = PointInTimeFilter()
        filtered = pit_filter.filter(records, as_of_ts=backtest_t_ms)

        for record in filtered:
            assert record["time"] <= backtest_t_ms, (
                f"Record with time={record['time']} was returned "
                f"for backtest_t={backtest_t_ms} — look-ahead bias detected"
            )

    @given(
        st.integers(min_value=EPOCH_MIN + 1001, max_value=EPOCH_MAX),
    )
    @settings(max_examples=100)
    def test_past_records_not_excluded(self, backtest_t_ms: int) -> None:
        """Records with time <= T must be included, not excluded."""
        records = [
            {"time": backtest_t_ms - 1000, "symbol": "TEST", "close": 100.0},
            {"time": backtest_t_ms, "symbol": "TEST", "close": 200.0},
        ]

        pit_filter = PointInTimeFilter()
        filtered = pit_filter.filter(records, as_of_ts=backtest_t_ms)

        # Both records are at or before T — both must be included
        assert len(filtered) == 2, (
            f"Expected 2 records at/before T={backtest_t_ms}, got {len(filtered)}"
        )
