"""
Property 14 — Reconciliation Deviation Classification

When two providers return data for the same (symbol, interval, timestamp):
- All fields within 0.5% → CONFIRMED
- Any field 0.5%–2.0% → MINOR_DISCREPANCY
- Any field > 2.0% → MAJOR_DISCREPANCY → DataIncident

Deviation formula: |A - B| / max(|A|, |B|) * 100

Requirement: 10.5–10.7
"""
from __future__ import annotations

import math
from hypothesis import given, settings
from hypothesis import strategies as st

from src.engines.historical_engine import HistoricalEngine
from src.engines.historical_engine import (
    RECONCILIATION_CONFIRMED,
    RECONCILIATION_MINOR,
    RECONCILIATION_MAJOR,
)


positive_price = st.floats(
    min_value=0.01, max_value=1_000_000.0, allow_nan=False, allow_infinity=False
)
small_pct = st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False)
minor_pct = st.floats(min_value=0.51, max_value=2.0, allow_nan=False, allow_infinity=False)
major_pct = st.floats(min_value=2.1, max_value=50.0, allow_nan=False, allow_infinity=False)


def apply_pct_deviation(base: float, pct: float) -> float:
    return base * (1.0 + pct / 100.0)


def make_engine():
    """Create a properly-initialised HistoricalEngine for reconciliation tests."""
    engine = HistoricalEngine.__new__(HistoricalEngine)
    # Initialise only the attributes reconcile() needs
    engine._reconciliation_results = []
    return engine


COMMON = dict(
    instrument_id="NSE:RELIANCE",
    exchange="NSE",
    interval="1d",
    timestamp=1_700_000_000,
    provider_a="angel_one",
    provider_b="upstox",
)


class TestReconciliationDeviation:
    @given(positive_price, positive_price, positive_price, positive_price, small_pct)
    @settings(max_examples=100)
    def test_within_0_5_pct_is_confirmed(
        self, open_: float, high: float, low: float, close: float, deviation_pct: float
    ) -> None:
        """Deviations <= 0.5% on all fields → CONFIRMED."""
        engine = make_engine()
        a = {"open": open_, "high": high, "low": low, "close": close, "volume": 1000.0}
        b = {
            "open": apply_pct_deviation(open_, deviation_pct),
            "high": apply_pct_deviation(high, deviation_pct),
            "low": apply_pct_deviation(low, deviation_pct),
            "close": apply_pct_deviation(close, deviation_pct),
            "volume": 1000.0,
        }
        result = engine.reconcile(**COMMON, a_values=a, b_values=b)
        assert result.status == RECONCILIATION_CONFIRMED, (
            f"Expected CONFIRMED for deviation <= 0.5%, got {result.status}"
        )

    @given(positive_price, minor_pct)
    @settings(max_examples=100)
    def test_0_5_to_2_pct_is_minor_discrepancy(
        self, close: float, deviation_pct: float
    ) -> None:
        """At least one field in (0.5%, 2%] range → MINOR_DISCREPANCY."""
        engine = make_engine()
        a = {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000.0}
        b = {**a, "close": apply_pct_deviation(close, deviation_pct)}
        result = engine.reconcile(**COMMON, a_values=a, b_values=b)
        assert result.status == RECONCILIATION_MINOR, (
            f"Expected MINOR_DISCREPANCY for deviation ~{deviation_pct:.2f}%, got {result.status}"
        )

    @given(positive_price, major_pct)
    @settings(max_examples=100)
    def test_over_2_pct_is_major_discrepancy(
        self, close: float, deviation_pct: float
    ) -> None:
        """Any field > 2% deviation → MAJOR_DISCREPANCY + incident generated."""
        engine = make_engine()
        a = {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000.0}
        b = {**a, "close": apply_pct_deviation(close, deviation_pct)}
        result = engine.reconcile(**COMMON, a_values=a, b_values=b)
        assert result.status == RECONCILIATION_MAJOR, (
            f"Expected MAJOR_DISCREPANCY for deviation {deviation_pct:.2f}%, got {result.status}"
        )
        # A DataIncident must be generated for major discrepancies
        assert result.incident is not None, "MAJOR_DISCREPANCY must produce a DataIncident"
