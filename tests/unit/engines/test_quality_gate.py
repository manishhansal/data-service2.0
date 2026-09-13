"""
tests/unit/engines/test_quality_gate.py

Unit tests for DataQualityGate (task 9.2) and GateResult (Pydantic v2 model).

Covers:
  - All-pass case: passed=True, failed_conditions=[], score correct
  - Each of the five conditions failing independently
  - signalEngineAllowed = True iff all five conditions are True (32 combinations
    represented by the critical boundary cases)
  - blockReasons is non-empty when passed=False (Requirement 7.9)
  - blockReasons is empty when passed=True
  - Confidence score below 30 always fails (BLOCKED invariant, Requirement 7.11)
  - Confidence score below default threshold (60) fails condition 5
  - DataQualityGate.check() returns GateResult with correct fields
  - QualityEngine.evaluate_gate() delegates to DataQualityGate

Requirements: 7.2, 7.9, 7.11
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from src.engines.quality_engine import DataQualityGate, GateResult, QualityEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW_MS = int(time.time() * 1000)
_RECENT_MS = _NOW_MS - 5_000  # 5 seconds ago — always fresh


def _valid_data(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid market-data dict that passes all five conditions."""
    base: dict[str, Any] = {
        # Completeness fields
        "symbol": "NIFTY",
        "timestamp": _RECENT_MS,
        "open": 22_000.0,
        "high": 22_200.0,
        "low": 21_900.0,
        "close": 22_100.0,
        "volume": 1_234_567,
        # Freshness
        "eventTimeMs": _RECENT_MS,
        # Provider availability
        "providerAvailable": True,
        # Confidence (≥ default 60)
        "confidenceScore": 80,
    }
    base.update(overrides)
    return base


_engine = QualityEngine()


# ---------------------------------------------------------------------------
# GateResult model
# ---------------------------------------------------------------------------


class TestGateResult:
    """Tests for the GateResult Pydantic model."""

    def test_passed_true_empty_reasons(self) -> None:
        result = GateResult(passed=True, failed_conditions=[], score=80.0)
        assert result.passed is True
        assert result.failed_conditions == []

    def test_passed_false_with_reasons(self) -> None:
        result = GateResult(
            passed=False,
            failed_conditions=["dataFresh=False: stale"],
            score=55.0,
        )
        assert result.passed is False
        assert len(result.failed_conditions) == 1

    def test_score_bounds(self) -> None:
        """score must be in [0, 95]."""
        GateResult(passed=True, failed_conditions=[], score=0.0)
        GateResult(passed=True, failed_conditions=[], score=95.0)

    def test_score_below_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            GateResult(passed=True, failed_conditions=[], score=-1.0)

    def test_score_above_95_rejected(self) -> None:
        with pytest.raises(Exception):
            GateResult(passed=True, failed_conditions=[], score=96.0)

    def test_frozen_model(self) -> None:
        """GateResult is immutable (frozen=True)."""
        result = GateResult(passed=True, failed_conditions=[], score=80.0)
        with pytest.raises(Exception):
            result.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DataQualityGate.check() — all-pass case
# ---------------------------------------------------------------------------


class TestDataQualityGateAllPass:
    """All five conditions pass → passed=True, empty failed_conditions."""

    def test_all_pass_returns_passed_true(self) -> None:
        result = DataQualityGate.check(_valid_data())
        assert result.passed is True

    def test_all_pass_empty_failed_conditions(self) -> None:
        result = DataQualityGate.check(_valid_data())
        assert result.failed_conditions == []

    def test_all_pass_score_is_data_confidence_score(self) -> None:
        result = DataQualityGate.check(_valid_data(confidenceScore=85))
        assert result.score == 85.0

    def test_all_pass_gate_has_all_true(self) -> None:
        gate = DataQualityGate.evaluate(_valid_data())
        assert gate.dataFresh is True
        assert gate.dataComplete is True
        assert gate.dataTimestampValid is True
        assert gate.dataProviderHealthy is True
        assert gate.dataSemanticallyValid is True
        assert gate.signalEngineAllowed is True


# ---------------------------------------------------------------------------
# Condition 1: Freshness (dataFresh)
# ---------------------------------------------------------------------------


class TestCondition1Freshness:
    """dataFresh condition failure tests."""

    def test_stale_via_quote_age_ms(self) -> None:
        """quoteAgeMs > freshnessFreshMs → dataFresh=False."""
        data = _valid_data(quoteAgeMs=60_000, freshnessFreshMs=30_000)
        result = DataQualityGate.check(data)
        assert result.passed is False
        assert any("dataFresh" in r for r in result.failed_conditions)

    def test_fresh_via_quote_age_ms(self) -> None:
        """quoteAgeMs <= freshnessFreshMs → dataFresh=True (other conditions pass)."""
        data = _valid_data(quoteAgeMs=10_000, freshnessFreshMs=30_000)
        result = DataQualityGate.check(data)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataFresh is True

    def test_stale_event_time_older_than_30_days(self) -> None:
        """eventTimeMs older than 30 days → dataFresh=False."""
        thirty_one_days_ms = 31 * 24 * 60 * 60 * 1000
        old_ts = _NOW_MS - thirty_one_days_ms
        data = _valid_data(eventTimeMs=old_ts, timestamp=old_ts)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataFresh is False

    def test_recent_event_time_is_fresh(self) -> None:
        """eventTimeMs within 30 days → dataFresh=True (by default)."""
        data = _valid_data(eventTimeMs=_RECENT_MS)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataFresh is True

    def test_no_timing_info_defaults_to_fresh(self) -> None:
        """When no timing fields are present, freshness defaults to True."""
        data = _valid_data()
        # Remove all timing info
        data.pop("eventTimeMs", None)
        data.pop("quoteAgeMs", None)
        data.pop("freshnessFreshMs", None)
        # timestamp is required for completeness — keep it, but remove eventTimeMs
        gate = DataQualityGate.evaluate(data)
        assert gate.dataFresh is True

    def test_block_reason_mentions_dataFresh(self) -> None:
        """When freshness fails, blockReasons must mention dataFresh."""
        data = _valid_data(quoteAgeMs=999_999, freshnessFreshMs=1_000)
        result = DataQualityGate.check(data)
        assert any("dataFresh" in r for r in result.failed_conditions)


# ---------------------------------------------------------------------------
# Condition 2: Completeness (dataComplete)
# ---------------------------------------------------------------------------


class TestCondition2Completeness:
    """dataComplete condition failure tests."""

    @pytest.mark.parametrize("missing_field", [
        "symbol", "timestamp", "open", "high", "low", "close", "volume",
    ])
    def test_missing_required_field_fails(self, missing_field: str) -> None:
        """Removing any single required field must fail completeness."""
        data = _valid_data()
        data.pop(missing_field)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataComplete is False
        assert gate.signalEngineAllowed is False

    @pytest.mark.parametrize("null_field", [
        "symbol", "open", "high", "low", "close", "volume",
    ])
    def test_null_required_field_fails(self, null_field: str) -> None:
        """Setting any required field to None must fail completeness."""
        data = _valid_data(**{null_field: None})
        gate = DataQualityGate.evaluate(data)
        assert gate.dataComplete is False

    def test_block_reason_mentions_missing_field(self) -> None:
        """blockReasons must name the missing field."""
        data = _valid_data()
        data.pop("volume")
        result = DataQualityGate.check(data)
        assert any("volume" in r for r in result.failed_conditions)

    def test_all_required_fields_present_passes(self) -> None:
        """When all required fields are present, completeness passes."""
        gate = DataQualityGate.evaluate(_valid_data())
        assert gate.dataComplete is True


# ---------------------------------------------------------------------------
# Condition 3: Timestamp validity + OHLCV consistency (dataTimestampValid)
# ---------------------------------------------------------------------------


class TestCondition3TimestampAndOHLCV:
    """dataTimestampValid condition failure tests."""

    def test_high_below_open_fails(self) -> None:
        """high < open violates high >= max(open, close)."""
        data = _valid_data(open=22_000.0, high=21_000.0, low=20_000.0, close=21_500.0)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is False

    def test_high_below_close_fails(self) -> None:
        """high < close violates high >= max(open, close)."""
        data = _valid_data(open=21_000.0, high=22_000.0, low=20_000.0, close=23_000.0)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is False

    def test_low_above_open_fails(self) -> None:
        """low > open violates low <= min(open, close)."""
        data = _valid_data(open=20_000.0, high=22_000.0, low=21_000.0, close=21_500.0)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is False

    def test_low_above_close_fails(self) -> None:
        """low > close violates low <= min(open, close)."""
        data = _valid_data(open=22_000.0, high=23_000.0, low=21_500.0, close=21_000.0)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is False

    def test_negative_volume_fails(self) -> None:
        """volume < 0 is invalid."""
        data = _valid_data(volume=-1)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is False

    def test_zero_volume_is_valid(self) -> None:
        """volume = 0 is valid (e.g. pre-market candle)."""
        data = _valid_data(volume=0)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is True

    def test_zero_price_fails(self) -> None:
        """A price of 0 violates price > 0 invariant."""
        data = _valid_data(open=0.0, high=100.0, low=0.0, close=50.0)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is False

    def test_negative_price_fails(self) -> None:
        """Negative open price is invalid."""
        data = _valid_data(open=-100.0, high=100.0, low=-200.0, close=50.0)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is False

    def test_valid_ohlcv_passes(self) -> None:
        """A clean, consistent OHLCV candle must pass the condition."""
        gate = DataQualityGate.evaluate(_valid_data())
        assert gate.dataTimestampValid is True

    def test_future_timestamp_fails(self) -> None:
        """A timestamp more than 1 minute in the future must fail."""
        future_ts = _NOW_MS + 120_000  # 2 minutes ahead
        data = _valid_data(timestamp=future_ts, eventTimeMs=future_ts)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is False

    def test_negative_timestamp_fails(self) -> None:
        """A non-positive timestamp is invalid."""
        data = _valid_data(timestamp=-1)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is False

    def test_ohlc_consistency_high_equals_open(self) -> None:
        """high == open is valid (e.g. a gap-down bar)."""
        data = _valid_data(open=22_000.0, high=22_000.0, low=21_000.0, close=21_500.0)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is True

    def test_block_reason_mentions_dataTimestampValid(self) -> None:
        data = _valid_data(volume=-5)
        result = DataQualityGate.check(data)
        assert any("dataTimestampValid" in r for r in result.failed_conditions)


# ---------------------------------------------------------------------------
# Condition 4: Provider / source availability (dataProviderHealthy)
# ---------------------------------------------------------------------------


class TestCondition4ProviderAvailability:
    """dataProviderHealthy condition failure tests."""

    def test_no_provider_info_fails(self) -> None:
        """No providerAvailable/source/provider fields → dataProviderHealthy=False."""
        data = _valid_data()
        data.pop("providerAvailable", None)
        data.pop("source", None)
        data.pop("provider", None)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataProviderHealthy is False

    def test_provider_available_false_no_source_fails(self) -> None:
        """providerAvailable=False and no source → fails."""
        data = _valid_data(providerAvailable=False)
        data.pop("source", None)
        data.pop("provider", None)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataProviderHealthy is False

    def test_provider_available_true_passes(self) -> None:
        """providerAvailable=True → passes."""
        data = _valid_data(providerAvailable=True)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataProviderHealthy is True

    def test_source_string_passes(self) -> None:
        """Non-empty source string (with providerAvailable absent) → passes."""
        data = _valid_data()
        data.pop("providerAvailable", None)
        data["source"] = "angel_one"
        gate = DataQualityGate.evaluate(data)
        assert gate.dataProviderHealthy is True

    def test_provider_string_passes(self) -> None:
        """Non-empty provider string → passes."""
        data = _valid_data()
        data.pop("providerAvailable", None)
        data.pop("source", None)
        data["provider"] = "upstox"
        gate = DataQualityGate.evaluate(data)
        assert gate.dataProviderHealthy is True

    def test_empty_source_string_fails(self) -> None:
        """Empty string source does not count as available."""
        data = _valid_data(providerAvailable=False, source="  ", provider="")
        gate = DataQualityGate.evaluate(data)
        assert gate.dataProviderHealthy is False

    def test_block_reason_mentions_dataProviderHealthy(self) -> None:
        data = _valid_data()
        data.pop("providerAvailable", None)
        result = DataQualityGate.check(data)
        assert any("dataProviderHealthy" in r for r in result.failed_conditions)


# ---------------------------------------------------------------------------
# Condition 5: Confidence threshold / semantic validity (dataSemanticallyValid)
# ---------------------------------------------------------------------------


class TestCondition5Confidence:
    """dataSemanticallyValid condition failure tests."""

    def test_confidence_at_default_threshold_passes(self) -> None:
        """confidenceScore == 60 (default threshold) → passes."""
        data = _valid_data(confidenceScore=60)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataSemanticallyValid is True

    def test_confidence_above_threshold_passes(self) -> None:
        """confidenceScore > 60 → passes."""
        data = _valid_data(confidenceScore=80)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataSemanticallyValid is True

    def test_confidence_below_threshold_fails(self) -> None:
        """confidenceScore < 60 (e.g. 59) → fails."""
        data = _valid_data(confidenceScore=59)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataSemanticallyValid is False

    def test_confidence_zero_fails(self) -> None:
        """confidenceScore=0 → fails."""
        data = _valid_data(confidenceScore=0)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataSemanticallyValid is False

    def test_confidence_absent_defaults_to_zero_fails(self) -> None:
        """Missing confidenceScore defaults to 0 → fails condition 5."""
        data = _valid_data()
        data.pop("confidenceScore", None)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataSemanticallyValid is False

    def test_confidence_below_30_always_fails(self) -> None:
        """score < 30 (BLOCKED) always fails, even with min_confidence_score=0.

        Requirement 7.11: DataConfidenceScore < 30 → BLOCKED → no exceptions.
        """
        data = _valid_data(confidenceScore=29)
        gate = DataQualityGate.evaluate(data, min_confidence_score=0.0)
        assert gate.dataSemanticallyValid is False
        assert gate.signalEngineAllowed is False

    def test_confidence_29_is_blocked(self) -> None:
        """Explicit check: score=29 is one below BLOCKED threshold."""
        data = _valid_data(confidenceScore=29)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataSemanticallyValid is False

    def test_confidence_30_with_min_30_passes(self) -> None:
        """score=30 with min_confidence_score=30 → passes condition 5."""
        data = _valid_data(confidenceScore=30)
        gate = DataQualityGate.evaluate(data, min_confidence_score=30.0)
        assert gate.dataSemanticallyValid is True

    def test_custom_min_confidence_respected(self) -> None:
        """Custom min_confidence_score is honoured when above the BLOCKED floor."""
        data = _valid_data(confidenceScore=70)
        gate_fail = DataQualityGate.evaluate(data, min_confidence_score=75.0)
        gate_pass = DataQualityGate.evaluate(data, min_confidence_score=70.0)
        assert gate_fail.dataSemanticallyValid is False
        assert gate_pass.dataSemanticallyValid is True

    def test_block_reason_mentions_dataSemanticallyValid(self) -> None:
        data = _valid_data(confidenceScore=20)
        result = DataQualityGate.check(data)
        assert any("dataSemanticallyValid" in r for r in result.failed_conditions)

    def test_confidence_score_capped_at_95(self) -> None:
        """confidenceScore values > 95 are clamped to 95 internally."""
        data = _valid_data(confidenceScore=200)
        gate = DataQualityGate.evaluate(data)
        assert gate.confidenceScore == 95


# ---------------------------------------------------------------------------
# signalEngineAllowed — closed-form correctness (all 2^5 = 32 combinations
# represented by key boundary cases)
# ---------------------------------------------------------------------------


class TestSignalEngineAllowedClosedForm:
    """signalEngineAllowed = True iff ALL five conditions are True.

    Requirement 7.2.
    """

    def test_all_true_allows(self) -> None:
        gate = DataQualityGate.evaluate(_valid_data())
        assert gate.signalEngineAllowed is True

    @pytest.mark.parametrize("failing_condition", [
        "fresh", "complete", "timestamp_valid", "provider", "confidence",
    ])
    def test_single_false_condition_blocks(self, failing_condition: str) -> None:
        """Any single failing condition must block signalEngineAllowed."""
        data = _valid_data()

        if failing_condition == "fresh":
            data["quoteAgeMs"] = 999_999
            data["freshnessFreshMs"] = 1_000
        elif failing_condition == "complete":
            data.pop("symbol")
        elif failing_condition == "timestamp_valid":
            data["volume"] = -1
        elif failing_condition == "provider":
            data.pop("providerAvailable", None)
            data.pop("source", None)
            data.pop("provider", None)
        elif failing_condition == "confidence":
            data["confidenceScore"] = 10

        gate = DataQualityGate.evaluate(data)
        assert gate.signalEngineAllowed is False, (
            f"Expected signalEngineAllowed=False when '{failing_condition}' fails"
        )

    def test_two_conditions_failing_still_blocks(self) -> None:
        """Multiple failing conditions still produce signalEngineAllowed=False."""
        data = _valid_data(confidenceScore=10)
        data.pop("symbol")  # also fails completeness
        gate = DataQualityGate.evaluate(data)
        assert gate.signalEngineAllowed is False

    def test_all_conditions_in_gates_dict(self) -> None:
        """The gates dict must contain all five condition keys."""
        gate = DataQualityGate.evaluate(_valid_data())
        assert set(gate.gates.keys()) == {
            "dataFresh",
            "dataComplete",
            "dataTimestampValid",
            "dataProviderHealthy",
            "dataSemanticallyValid",
        }

    def test_gates_dict_reflects_conditions(self) -> None:
        """gates dict values must match the individual boolean fields."""
        gate = DataQualityGate.evaluate(_valid_data())
        assert gate.gates["dataFresh"] == gate.dataFresh
        assert gate.gates["dataComplete"] == gate.dataComplete
        assert gate.gates["dataTimestampValid"] == gate.dataTimestampValid
        assert gate.gates["dataProviderHealthy"] == gate.dataProviderHealthy
        assert gate.gates["dataSemanticallyValid"] == gate.dataSemanticallyValid


# ---------------------------------------------------------------------------
# blockReasons invariants (Requirement 7.9)
# ---------------------------------------------------------------------------


class TestBlockReasons:
    """When signalEngineAllowed=False, blockReasons must be non-empty."""

    def test_block_reasons_non_empty_when_blocked(self) -> None:
        """At least one block reason must be present when gate blocks."""
        data = _valid_data(confidenceScore=0)
        gate = DataQualityGate.evaluate(data)
        assert gate.signalEngineAllowed is False
        assert len(gate.blockReasons) >= 1

    def test_block_reasons_empty_when_allowed(self) -> None:
        """blockReasons must be empty when all conditions pass."""
        gate = DataQualityGate.evaluate(_valid_data())
        assert gate.signalEngineAllowed is True
        assert gate.blockReasons == []

    def test_multiple_failures_produce_multiple_reasons(self) -> None:
        """Multiple failed conditions must produce multiple distinct block reasons."""
        data = _valid_data(confidenceScore=0)
        data.pop("symbol")   # completeness also fails
        gate = DataQualityGate.evaluate(data)
        assert len(gate.blockReasons) >= 2


# ---------------------------------------------------------------------------
# QualityEngine.evaluate_gate() delegation
# ---------------------------------------------------------------------------


class TestQualityEngineEvaluateGate:
    """QualityEngine.evaluate_gate() must delegate to DataQualityGate."""

    def test_returns_gate_result(self) -> None:
        result = _engine.evaluate_gate(_valid_data())
        assert isinstance(result, GateResult)

    def test_all_pass_via_evaluate_gate(self) -> None:
        result = _engine.evaluate_gate(_valid_data())
        assert result.passed is True

    def test_failure_propagated_via_evaluate_gate(self) -> None:
        data = _valid_data(confidenceScore=10)
        result = _engine.evaluate_gate(data)
        assert result.passed is False
        assert len(result.failed_conditions) >= 1

    def test_custom_min_confidence_respected(self) -> None:
        data = _valid_data(confidenceScore=75)
        result_pass = _engine.evaluate_gate(data, min_confidence_score=70.0)
        result_fail = _engine.evaluate_gate(data, min_confidence_score=80.0)
        assert result_pass.passed is True
        assert result_fail.passed is False

    def test_score_in_result_matches_data(self) -> None:
        data = _valid_data(confidenceScore=82)
        result = _engine.evaluate_gate(data)
        assert result.score == 82.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge-case and boundary tests."""

    def test_empty_dict_fails_multiple_conditions(self) -> None:
        """An empty dict must fail completeness, provider, and confidence."""
        gate = DataQualityGate.evaluate({})
        assert gate.signalEngineAllowed is False
        assert gate.dataComplete is False
        assert gate.dataProviderHealthy is False
        assert gate.dataSemanticallyValid is False

    def test_score_95_with_threshold_95_passes_condition5(self) -> None:
        """Score exactly at its maximum (95) passes condition 5 at threshold 95."""
        data = _valid_data(confidenceScore=95)
        gate = DataQualityGate.evaluate(data, min_confidence_score=95.0)
        assert gate.dataSemanticallyValid is True

    def test_score_94_with_threshold_95_fails_condition5(self) -> None:
        data = _valid_data(confidenceScore=94)
        gate = DataQualityGate.evaluate(data, min_confidence_score=95.0)
        assert gate.dataSemanticallyValid is False

    def test_high_equals_low_but_prices_valid(self) -> None:
        """A doji candle (open=high=low=close) is valid OHLCV."""
        price = 22_000.0
        data = _valid_data(open=price, high=price, low=price, close=price)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is True

    def test_float_volume_accepted(self) -> None:
        """volume as a float (e.g. from JSON) is accepted as long as >= 0."""
        data = _valid_data(volume=1000.5)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataTimestampValid is True

    def test_freshness_window_boundary_equal_is_fresh(self) -> None:
        """quoteAgeMs == freshnessFreshMs is exactly at the boundary → fresh."""
        data = _valid_data(quoteAgeMs=30_000, freshnessFreshMs=30_000)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataFresh is True

    def test_freshness_window_one_ms_over_is_stale(self) -> None:
        """quoteAgeMs = freshnessFreshMs + 1 → stale (just over boundary)."""
        data = _valid_data(quoteAgeMs=30_001, freshnessFreshMs=30_000)
        gate = DataQualityGate.evaluate(data)
        assert gate.dataFresh is False
