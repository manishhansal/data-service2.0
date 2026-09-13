"""
tests/unit/engines/test_reconciliation.py

Unit tests for cross-provider OHLCV reconciliation (Task 7.2).

Coverage:
  - ≤ 0.5% deviation on all fields → CONFIRMED
  - Any field between 0.5% and 2.0% → MINOR_DISCREPANCY
  - Any field > 2.0% → MAJOR_DISCREPANCY
  - Worst-field determines overall status
  - MAJOR_DISCREPANCY generates a DataIncident dict
  - CONFIRMED and MINOR do NOT generate an incident
  - Edge case: both values are zero → 0.0% deviation (CONFIRMED)
  - Edge case: identical values → 0.0% deviation (CONFIRMED)
  - Deviation formula correctness: |A − B| / max(|A|, |B|) × 100
  - ReconciliationResult has the correct shape
  - get_reconciliation_stats() aggregates correctly
  - get_reconciliation_stats() handles zero results

Requirements: 10.5, 10.6, 10.7
"""

from __future__ import annotations

import pytest

from src.engines.historical_engine import (
    RECONCILIATION_CONFIRMED,
    RECONCILIATION_MAJOR,
    RECONCILIATION_MINOR,
    ReconciliationResult,
    HistoricalEngine,
    _deviation_pct,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _engine() -> HistoricalEngine:
    """Return a fresh HistoricalEngine instance (no shared state)."""
    return HistoricalEngine()


def _candle(open=100.0, high=110.0, low=90.0, close=105.0, volume=10000):
    """Return a minimal OHLCV dict."""
    return {
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


# ---------------------------------------------------------------------------
# _deviation_pct formula correctness
# ---------------------------------------------------------------------------


class TestDeviationPctFormula:
    """Verify the |A − B| / max(|A|, |B|) × 100 formula."""

    def test_identical_values_yields_zero(self):
        assert _deviation_pct(100.0, 100.0) == 0.0

    def test_both_zero_yields_zero(self):
        assert _deviation_pct(0.0, 0.0) == 0.0

    def test_symmetric(self):
        """Deviation of (A, B) equals deviation of (B, A)."""
        assert _deviation_pct(100.0, 101.0) == _deviation_pct(101.0, 100.0)

    def test_small_deviation(self):
        # |100 - 100.3| / max(100, 100.3) × 100 = 0.3 / 100.3 × 100 ≈ 0.299%
        result = _deviation_pct(100.0, 100.3)
        assert abs(result - (0.3 / 100.3 * 100)) < 1e-9

    def test_exactly_0_5_pct(self):
        # |100 - 100.5| / 100.5 × 100 = 0.5/100.5×100 ≈ 0.4975%
        # For a clean 0.5%: use A=100, B=100*(1+0.005) — deviates just under 0.5%
        # Compute manually: |200 - 201| / 201 × 100 = 1/201 × 100 ≈ 0.4975
        result = _deviation_pct(200.0, 201.0)
        expected = 1.0 / 201.0 * 100.0
        assert abs(result - expected) < 1e-9

    def test_2_pct(self):
        # |100 - 102| / 102 × 100 ≈ 1.9608%
        result = _deviation_pct(100.0, 102.0)
        expected = 2.0 / 102.0 * 100.0
        assert abs(result - expected) < 1e-9

    def test_large_deviation(self):
        # |50 - 100| / 100 × 100 = 50%
        result = _deviation_pct(50.0, 100.0)
        assert abs(result - 50.0) < 1e-9

    def test_zero_vs_nonzero(self):
        # |0 - 100| / max(0, 100) × 100 = 100/100 × 100 = 100%
        result = _deviation_pct(0.0, 100.0)
        assert abs(result - 100.0) < 1e-9


# ---------------------------------------------------------------------------
# reconcile() — status classification
# ---------------------------------------------------------------------------


class TestReconcileClassification:
    """Verify status classification logic across all deviation bands."""

    def test_identical_candles_confirmed(self):
        engine = _engine()
        a = _candle()
        b = _candle()
        result = engine.reconcile(
            instrument_id="NSE:RELIANCE",
            exchange="NSE",
            interval="1m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="openchart",
        )
        assert result.status == RECONCILIATION_CONFIRMED
        assert result.incident is None

    def test_tiny_deviation_confirmed(self):
        """All fields within 0.5% → CONFIRMED."""
        engine = _engine()
        # 0.1% deviation on every field
        a = _candle(open=100.0, high=110.0, low=90.0, close=105.0, volume=10000)
        b = _candle(open=100.1, high=110.11, low=90.09, close=105.105, volume=10010)
        result = engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="5m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="upstox",
        )
        assert result.status == RECONCILIATION_CONFIRMED
        assert result.incident is None
        assert result.worst_deviation_pct <= 0.5

    def test_minor_discrepancy_boundary(self):
        """One field just above 0.5% → MINOR_DISCREPANCY."""
        engine = _engine()
        # open deviates by ≈ 0.6% (well above 0.5%)
        a = _candle(open=100.0)
        b = _candle(open=100.6)  # |0.6|/100.6 × 100 ≈ 0.596%
        result = engine.reconcile(
            instrument_id="NSE:RELIANCE",
            exchange="NSE",
            interval="1m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="openchart",
        )
        assert result.status == RECONCILIATION_MINOR
        assert result.incident is None

    def test_minor_discrepancy_close_to_2pct(self):
        """Field just below 2% → MINOR_DISCREPANCY."""
        engine = _engine()
        # close deviates by ~1.96% (just under 2%)
        a = _candle(close=100.0)
        b = _candle(close=102.0)   # 2/102 × 100 ≈ 1.96%
        result = engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1d",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="upstox",
            provider_b="openchart",
        )
        assert result.status == RECONCILIATION_MINOR
        assert result.incident is None

    def test_major_discrepancy_above_2pct(self):
        """Any field > 2% → MAJOR_DISCREPANCY."""
        engine = _engine()
        # high deviates by ~2.86%
        a = _candle(high=105.0)
        b = _candle(high=108.0)   # 3/108 × 100 ≈ 2.778%
        result = engine.reconcile(
            instrument_id="NSE:BANKNIFTY",
            exchange="NSE",
            interval="5m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="jugaad",
        )
        assert result.status == RECONCILIATION_MAJOR

    def test_major_discrepancy_generates_incident(self):
        """MAJOR_DISCREPANCY must produce a non-None DataIncident dict."""
        engine = _engine()
        a = _candle(open=100.0)
        b = _candle(open=105.0)   # 5/105 × 100 ≈ 4.76%
        result = engine.reconcile(
            instrument_id="NSE:RELIANCE",
            exchange="NSE",
            interval="15m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="upstox",
        )
        assert result.status == RECONCILIATION_MAJOR
        assert result.incident is not None
        # DataIncident must have required fields
        incident = result.incident
        assert "incidentId" in incident
        assert incident["incidentType"] == "MAJOR_DISCREPANCY"
        assert incident["instrumentId"] == "NSE:RELIANCE"
        assert "severity" in incident
        assert "details" in incident
        details = incident["details"]
        assert details["providerA"] == "angel_one"
        assert details["providerB"] == "upstox"
        assert details["worstField"] in ("open", "high", "low", "close", "volume")

    def test_confirmed_no_incident(self):
        """CONFIRMED result must have incident=None."""
        engine = _engine()
        a = _candle()
        b = _candle()
        result = engine.reconcile(
            instrument_id="NSE:TCS",
            exchange="NSE",
            interval="1h",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="openchart",
        )
        assert result.incident is None

    def test_minor_no_incident(self):
        """MINOR_DISCREPANCY result must have incident=None."""
        engine = _engine()
        a = _candle(open=100.0)
        b = _candle(open=101.0)   # ~0.99%
        result = engine.reconcile(
            instrument_id="NSE:INFY",
            exchange="NSE",
            interval="1m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="openchart",
        )
        assert result.status == RECONCILIATION_MINOR
        assert result.incident is None


# ---------------------------------------------------------------------------
# reconcile() — worst-field logic
# ---------------------------------------------------------------------------


class TestWorstField:
    """Verify that the worst-case field drives the overall status."""

    def test_single_bad_field_escalates_status(self):
        """One high-deviation field escalates status to MAJOR even if others are fine."""
        engine = _engine()
        # Only 'volume' has a large deviation; all prices are identical.
        a = _candle(open=100.0, high=110.0, low=90.0, close=105.0, volume=10000)
        b = _candle(open=100.0, high=110.0, low=90.0, close=105.0, volume=10300)
        # volume deviation: 300/10300 × 100 ≈ 2.91%
        result = engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="openchart",
        )
        assert result.status == RECONCILIATION_MAJOR
        assert result.worst_field == "volume"

    def test_worst_field_identified_correctly(self):
        """worst_field is the field with the highest individual deviation."""
        engine = _engine()
        # low has the highest deviation
        a = _candle(open=100.0, high=110.0, low=90.0, close=105.0, volume=10000)
        b = _candle(open=100.1, high=110.1, low=93.0, close=105.1, volume=10010)
        # low deviation: 3/93 × 100 ≈ 3.23% — clearly largest
        result = engine.reconcile(
            instrument_id="NSE:SBIN",
            exchange="NSE",
            interval="5m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="upstox",
        )
        assert result.worst_field == "low"

    def test_all_fields_present_in_field_deviations(self):
        """field_deviations must contain entries for all 5 OHLCV fields."""
        engine = _engine()
        a = _candle()
        b = _candle()
        result = engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="openchart",
        )
        assert set(result.field_deviations.keys()) == {
            "open", "high", "low", "close", "volume"
        }


# ---------------------------------------------------------------------------
# reconcile() — edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases: zeros, missing fields, large deviations."""

    def test_both_zero_all_fields(self):
        """Both providers return zero for all fields → CONFIRMED, zero deviations."""
        engine = _engine()
        a = _candle(open=0.0, high=0.0, low=0.0, close=0.0, volume=0)
        b = _candle(open=0.0, high=0.0, low=0.0, close=0.0, volume=0)
        result = engine.reconcile(
            instrument_id="NSE:TEST",
            exchange="NSE",
            interval="1m",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="angel_one",
            provider_b="openchart",
        )
        assert result.status == RECONCILIATION_CONFIRMED
        assert result.worst_deviation_pct == 0.0
        assert all(v == 0.0 for v in result.field_deviations.values())

    def test_missing_field_treated_as_zero(self):
        """Fields missing from provider dict are coerced to 0."""
        engine = _engine()
        a = {"open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0}  # volume missing
        b = _candle(volume=5000)
        # volume: |0 - 5000| / 5000 × 100 = 100%
        result = engine.reconcile(
            instrument_id="NSE:HDFC",
            exchange="NSE",
            interval="1d",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="openchart",
            provider_b="jugaad",
        )
        assert result.status == RECONCILIATION_MAJOR
        assert result.field_deviations["volume"] == 100.0

    def test_very_large_deviation(self):
        """100% deviation on a field is classified as MAJOR."""
        engine = _engine()
        a = _candle(close=100.0)
        b = _candle(close=200.0)
        result = engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1h",
            timestamp=1705300000,
            a_values=a,
            b_values=b,
            provider_a="upstox",
            provider_b="openchart",
        )
        assert result.status == RECONCILIATION_MAJOR
        # |100-200|/200×100 = 50%
        assert abs(result.field_deviations["close"] - 50.0) < 1e-9


# ---------------------------------------------------------------------------
# ReconciliationResult shape
# ---------------------------------------------------------------------------


class TestReconciliationResultShape:
    """Verify the dataclass has all required fields."""

    def test_result_fields(self):
        engine = _engine()
        result = engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1m",
            timestamp=1705300000,
            a_values=_candle(),
            b_values=_candle(),
            provider_a="angel_one",
            provider_b="openchart",
        )
        assert isinstance(result, ReconciliationResult)
        assert result.instrument_id == "NSE:NIFTY"
        assert result.exchange == "NSE"
        assert result.interval == "1m"
        assert result.timestamp == 1705300000
        assert result.provider_a == "angel_one"
        assert result.provider_b == "openchart"
        assert isinstance(result.field_deviations, dict)
        assert isinstance(result.worst_field, str)
        assert isinstance(result.worst_deviation_pct, float)
        assert result.worst_deviation_pct >= 0.0


# ---------------------------------------------------------------------------
# get_reconciliation_stats()
# ---------------------------------------------------------------------------


class TestGetReconciliationStats:
    """Verify aggregated statistics from get_reconciliation_stats()."""

    def test_empty_stats(self):
        """No reconciliations → all zeros."""
        engine = _engine()
        stats = engine.get_reconciliation_stats()
        assert stats["totalCompared"] == 0
        assert stats["matched"] == 0
        assert stats["matchRatePct"] == 0.0
        assert stats["distribution"][RECONCILIATION_CONFIRMED] == 0
        assert stats["distribution"][RECONCILIATION_MINOR] == 0
        assert stats["distribution"][RECONCILIATION_MAJOR] == 0
        assert stats["byProviderPair"] == {}

    def test_all_confirmed(self):
        """Three identical candle comparisons → 100% match rate."""
        engine = _engine()
        for _ in range(3):
            engine.reconcile(
                instrument_id="NSE:NIFTY",
                exchange="NSE",
                interval="1m",
                timestamp=1705300000,
                a_values=_candle(),
                b_values=_candle(),
                provider_a="angel_one",
                provider_b="openchart",
            )
        stats = engine.get_reconciliation_stats()
        assert stats["totalCompared"] == 3
        assert stats["matched"] == 3
        assert stats["matchRatePct"] == 100.0
        assert stats["distribution"][RECONCILIATION_CONFIRMED] == 3
        assert stats["distribution"][RECONCILIATION_MINOR] == 0
        assert stats["distribution"][RECONCILIATION_MAJOR] == 0

    def test_mixed_statuses(self):
        """Mix of CONFIRMED, MINOR, MAJOR → correct distribution and match rate."""
        engine = _engine()

        # 2 CONFIRMED
        for _ in range(2):
            engine.reconcile(
                instrument_id="NSE:NIFTY",
                exchange="NSE",
                interval="1m",
                timestamp=1705300000,
                a_values=_candle(),
                b_values=_candle(),
                provider_a="angel_one",
                provider_b="openchart",
            )

        # 1 MINOR (~0.8% deviation on open)
        engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1m",
            timestamp=1705300001,
            a_values=_candle(open=100.0),
            b_values=_candle(open=100.8),
            provider_a="angel_one",
            provider_b="openchart",
        )

        # 1 MAJOR (~4.76% deviation on open)
        engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1m",
            timestamp=1705300002,
            a_values=_candle(open=100.0),
            b_values=_candle(open=105.0),
            provider_a="angel_one",
            provider_b="openchart",
        )

        stats = engine.get_reconciliation_stats()
        assert stats["totalCompared"] == 4
        assert stats["matched"] == 2
        assert stats["matchRatePct"] == 50.0
        assert stats["distribution"][RECONCILIATION_CONFIRMED] == 2
        assert stats["distribution"][RECONCILIATION_MINOR] == 1
        assert stats["distribution"][RECONCILIATION_MAJOR] == 1

    def test_by_provider_pair_grouping(self):
        """Results from different provider pairs are grouped correctly."""
        engine = _engine()

        engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1m",
            timestamp=1705300000,
            a_values=_candle(),
            b_values=_candle(),
            provider_a="angel_one",
            provider_b="openchart",
        )

        engine.reconcile(
            instrument_id="NSE:NIFTY",
            exchange="NSE",
            interval="1m",
            timestamp=1705300001,
            a_values=_candle(),
            b_values=_candle(),
            provider_a="upstox",
            provider_b="jugaad",
        )

        stats = engine.get_reconciliation_stats()
        pairs = stats["byProviderPair"]
        assert "angel_one/openchart" in pairs
        assert "upstox/jugaad" in pairs
        assert pairs["angel_one/openchart"]["totalCompared"] == 1
        assert pairs["upstox/jugaad"]["totalCompared"] == 1
