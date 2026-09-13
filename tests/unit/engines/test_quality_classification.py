"""
tests/unit/engines/test_quality_classification.py

Unit tests for QualityEngine.classify_quality (task 9.4).

Covers:
  - QualityClassification is a Pydantic v2 model with the correct fields
  - Grade mapping mirrors grade_score():
      score ≥ 80 → HIGH
      50 ≤ score < 80 → MEDIUM
      30 ≤ score < 50 → LOW
      score < 30 → BLOCKED
  - signalEngineAllowed is True only when score ≥ 30 AND gate_result.passed=True
  - signalEngineAllowed is always False when score < 30 (BLOCKED — Req 7.11)
  - signalEngineAllowed is False when gate_result.passed=False regardless of score
  - reasons list is empty for HIGH when no gate conditions failed
  - reasons list is non-empty for MEDIUM, LOW, BLOCKED
  - failed_conditions from gate_result are included in reasons
  - Boundary scores produce correct grades

Requirements: 7.1, 7.2, 7.5, 7.11
"""

from __future__ import annotations

import time

import pytest

from src.engines.quality_engine import (
    DataQualityGate,
    GateResult,
    QualityClassification,
    QualityEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_engine = QualityEngine()
_NOW_MS = int(time.time() * 1000)
_RECENT_MS = _NOW_MS - 5_000  # 5 seconds ago — always within freshness window


def _passing_gate(score: int = 80) -> GateResult:
    """Return a GateResult that has passed all five conditions."""
    return GateResult(passed=True, failed_conditions=[], score=float(score))


def _failing_gate(score: int = 40, reasons: list[str] | None = None) -> GateResult:
    """Return a GateResult that has failed at least one condition."""
    if reasons is None:
        reasons = ["dataFresh=False: quoteAgeMs=60000ms exceeds freshnessFreshMs=30000ms"]
    return GateResult(passed=False, failed_conditions=reasons, score=float(score))


# ---------------------------------------------------------------------------
# QualityClassification model
# ---------------------------------------------------------------------------


class TestQualityClassificationModel:
    """Pydantic v2 model structure tests."""

    def test_has_required_fields(self) -> None:
        """QualityClassification must expose grade, signalEngineAllowed, score, reasons."""
        qc = QualityClassification(
            grade="HIGH",
            signalEngineAllowed=True,
            score=85,
            reasons=[],
        )
        assert qc.grade == "HIGH"
        assert qc.signalEngineAllowed is True
        assert qc.score == 85
        assert qc.reasons == []

    def test_frozen_immutable(self) -> None:
        """QualityClassification must be immutable (frozen=True)."""
        qc = QualityClassification(
            grade="HIGH", signalEngineAllowed=True, score=85, reasons=[]
        )
        with pytest.raises(Exception):
            qc.grade = "LOW"  # type: ignore[misc]

    def test_score_lower_bound(self) -> None:
        """score=0 is valid."""
        qc = QualityClassification(
            grade="BLOCKED", signalEngineAllowed=False, score=0, reasons=["blocked"]
        )
        assert qc.score == 0

    def test_score_upper_bound(self) -> None:
        """score=95 is valid."""
        qc = QualityClassification(
            grade="HIGH", signalEngineAllowed=True, score=95, reasons=[]
        )
        assert qc.score == 95

    def test_score_below_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            QualityClassification(
                grade="BLOCKED", signalEngineAllowed=False, score=-1, reasons=[]
            )

    def test_score_above_95_rejected(self) -> None:
        with pytest.raises(Exception):
            QualityClassification(
                grade="HIGH", signalEngineAllowed=True, score=96, reasons=[]
            )

    def test_reasons_defaults_to_empty_list(self) -> None:
        """reasons field defaults to an empty list when not supplied."""
        qc = QualityClassification(
            grade="HIGH", signalEngineAllowed=True, score=82
        )
        assert qc.reasons == []


# ---------------------------------------------------------------------------
# Grade mapping — mirrors grade_score()
# ---------------------------------------------------------------------------


class TestGradeMapping:
    """classify_quality grade must match grade_score() for every score value."""

    @pytest.mark.parametrize("score", [0, 1, 15, 29])
    def test_blocked_grade_below_30(self, score: int) -> None:
        """Scores below 30 must yield grade='BLOCKED'."""
        qc = _engine.classify_quality(score, _passing_gate(score))
        assert qc.grade == "BLOCKED", f"score={score} should be BLOCKED"

    @pytest.mark.parametrize("score", [30, 35, 49])
    def test_low_grade_30_to_49(self, score: int) -> None:
        """Scores in [30, 49] must yield grade='LOW'."""
        qc = _engine.classify_quality(score, _passing_gate(score))
        assert qc.grade == "LOW", f"score={score} should be LOW"

    @pytest.mark.parametrize("score", [50, 65, 79])
    def test_medium_grade_50_to_79(self, score: int) -> None:
        """Scores in [50, 79] must yield grade='MEDIUM'."""
        qc = _engine.classify_quality(score, _passing_gate(score))
        assert qc.grade == "MEDIUM", f"score={score} should be MEDIUM"

    @pytest.mark.parametrize("score", [80, 90, 95])
    def test_high_grade_80_and_above(self, score: int) -> None:
        """Scores in [80, 95] must yield grade='HIGH'."""
        qc = _engine.classify_quality(score, _passing_gate(score))
        assert qc.grade == "HIGH", f"score={score} should be HIGH"

    def test_boundary_score_29_is_blocked(self) -> None:
        qc = _engine.classify_quality(29, _passing_gate(29))
        assert qc.grade == "BLOCKED"

    def test_boundary_score_30_is_low(self) -> None:
        qc = _engine.classify_quality(30, _passing_gate(30))
        assert qc.grade == "LOW"

    def test_boundary_score_49_is_low(self) -> None:
        qc = _engine.classify_quality(49, _passing_gate(49))
        assert qc.grade == "LOW"

    def test_boundary_score_50_is_medium(self) -> None:
        qc = _engine.classify_quality(50, _passing_gate(50))
        assert qc.grade == "MEDIUM"

    def test_boundary_score_79_is_medium(self) -> None:
        qc = _engine.classify_quality(79, _passing_gate(79))
        assert qc.grade == "MEDIUM"

    def test_boundary_score_80_is_high(self) -> None:
        qc = _engine.classify_quality(80, _passing_gate(80))
        assert qc.grade == "HIGH"

    def test_grade_matches_grade_score_helper(self) -> None:
        """classify_quality grade must equal QualityEngine.grade_score for all values."""
        for score in range(0, 96):
            qc = _engine.classify_quality(score, _passing_gate(score))
            assert qc.grade == QualityEngine.grade_score(score), (
                f"grade mismatch at score={score}: "
                f"classify_quality returned '{qc.grade}', "
                f"grade_score returned '{QualityEngine.grade_score(score)}'"
            )


# ---------------------------------------------------------------------------
# signalEngineAllowed — closed-form correctness (Requirements 7.2, 7.11)
# ---------------------------------------------------------------------------


class TestSignalEngineAllowed:
    """signalEngineAllowed must be True iff score ≥ 30 AND gate_result.passed=True."""

    def test_high_score_passing_gate_allows(self) -> None:
        """score=85, gate.passed=True → signalEngineAllowed=True."""
        qc = _engine.classify_quality(85, _passing_gate(85))
        assert qc.signalEngineAllowed is True

    def test_medium_score_passing_gate_allows(self) -> None:
        """score=65, gate.passed=True → signalEngineAllowed=True."""
        qc = _engine.classify_quality(65, _passing_gate(65))
        assert qc.signalEngineAllowed is True

    def test_low_score_passing_gate_allows(self) -> None:
        """score=40, gate.passed=True → signalEngineAllowed=True (score ≥ 30)."""
        qc = _engine.classify_quality(40, _passing_gate(40))
        assert qc.signalEngineAllowed is True

    def test_blocked_score_passing_gate_still_blocks(self) -> None:
        """score=20, gate.passed=True → signalEngineAllowed=False (BLOCKED overrides).

        Requirement 7.11: DataConfidenceScore < 30 blocks signal engine with NO exceptions.
        """
        qc = _engine.classify_quality(20, _passing_gate(20))
        assert qc.signalEngineAllowed is False

    def test_blocked_score_always_false_no_exceptions(self) -> None:
        """Any score < 30 must produce signalEngineAllowed=False with a passing gate."""
        for score in range(0, 30):
            qc = _engine.classify_quality(score, _passing_gate(score))
            assert qc.signalEngineAllowed is False, (
                f"BLOCKED invariant violated: score={score} should not allow signal engine"
            )

    def test_high_score_failing_gate_blocks(self) -> None:
        """score=85, gate.passed=False → signalEngineAllowed=False."""
        qc = _engine.classify_quality(85, _failing_gate(85))
        assert qc.signalEngineAllowed is False

    def test_medium_score_failing_gate_blocks(self) -> None:
        """score=65, gate.passed=False → signalEngineAllowed=False."""
        qc = _engine.classify_quality(65, _failing_gate(65))
        assert qc.signalEngineAllowed is False

    def test_low_score_failing_gate_blocks(self) -> None:
        """score=40, gate.passed=False → signalEngineAllowed=False."""
        qc = _engine.classify_quality(40, _failing_gate(40))
        assert qc.signalEngineAllowed is False

    def test_zero_score_failing_gate_blocks(self) -> None:
        """Worst case: score=0, gate.passed=False → signalEngineAllowed=False."""
        qc = _engine.classify_quality(0, _failing_gate(0))
        assert qc.signalEngineAllowed is False

    def test_score_exactly_30_passing_gate_allows(self) -> None:
        """score=30 is the exact boundary — must be allowed when gate passes."""
        qc = _engine.classify_quality(30, _passing_gate(30))
        assert qc.signalEngineAllowed is True

    def test_score_exactly_29_blocked_even_with_passing_gate(self) -> None:
        """score=29 is one below the BLOCKED threshold — must always block."""
        qc = _engine.classify_quality(29, _passing_gate(29))
        assert qc.signalEngineAllowed is False

    def test_score_95_passing_gate_allows(self) -> None:
        """Perfect score with perfect gate → allowed."""
        qc = _engine.classify_quality(95, _passing_gate(95))
        assert qc.signalEngineAllowed is True


# ---------------------------------------------------------------------------
# reasons list contents
# ---------------------------------------------------------------------------


class TestReasons:
    """reasons list must reflect grade and any failed gate conditions."""

    def test_high_passing_gate_has_empty_reasons(self) -> None:
        """HIGH grade with all gate conditions passed → reasons=[]."""
        qc = _engine.classify_quality(85, _passing_gate(85))
        assert qc.reasons == []

    def test_medium_passing_gate_has_grade_reason(self) -> None:
        """MEDIUM grade → reasons must be non-empty (grade explanation added)."""
        qc = _engine.classify_quality(65, _passing_gate(65))
        assert len(qc.reasons) >= 1
        assert any("MEDIUM" in r or "65" in r for r in qc.reasons)

    def test_low_passing_gate_has_grade_reason(self) -> None:
        """LOW grade → reasons must be non-empty (grade explanation added)."""
        qc = _engine.classify_quality(40, _passing_gate(40))
        assert len(qc.reasons) >= 1
        assert any("LOW" in r or "40" in r for r in qc.reasons)

    def test_blocked_passing_gate_has_grade_reason(self) -> None:
        """BLOCKED grade → reasons must be non-empty (grade explanation added)."""
        qc = _engine.classify_quality(20, _passing_gate(20))
        assert len(qc.reasons) >= 1
        assert any("BLOCKED" in r or "20" in r for r in qc.reasons)

    def test_failing_gate_conditions_included_in_reasons(self) -> None:
        """Gate failed_conditions must appear in classify_quality reasons."""
        failed_reason = "dataFresh=False: quoteAgeMs=60000ms exceeds freshnessFreshMs=10000ms"
        gate = _failing_gate(score=75, reasons=[failed_reason])
        qc = _engine.classify_quality(75, gate)
        assert failed_reason in qc.reasons

    def test_multiple_failing_conditions_all_included(self) -> None:
        """All failed_conditions from the gate must appear in reasons."""
        reasons_in = [
            "dataFresh=False: stale",
            "dataComplete=False: required fields missing: volume",
        ]
        gate = _failing_gate(score=50, reasons=reasons_in)
        qc = _engine.classify_quality(50, gate)
        for r in reasons_in:
            assert r in qc.reasons, f"Expected reason '{r}' in {qc.reasons}"

    def test_high_with_failing_gate_includes_gate_reasons(self) -> None:
        """HIGH score but failing gate → gate reasons appear, signalEngineAllowed=False."""
        failed_reason = "dataProviderHealthy=False: no available source"
        gate = _failing_gate(score=85, reasons=[failed_reason])
        qc = _engine.classify_quality(85, gate)
        assert qc.signalEngineAllowed is False
        assert failed_reason in qc.reasons

    def test_blocked_reasons_mention_score_threshold(self) -> None:
        """BLOCKED reasons should reference the score or threshold for diagnostics."""
        qc = _engine.classify_quality(15, _passing_gate(15))
        combined = " ".join(qc.reasons)
        # Either the score value or 'BLOCKED' or '30' should appear somewhere
        assert any(keyword in combined for keyword in ["BLOCKED", "15", "30"])

    def test_score_field_matches_input(self) -> None:
        """QualityClassification.score must equal the input score."""
        for score in [0, 29, 30, 50, 80, 95]:
            qc = _engine.classify_quality(score, _passing_gate(score))
            assert qc.score == score


# ---------------------------------------------------------------------------
# Integration with DataQualityGate.check()
# ---------------------------------------------------------------------------


class TestIntegrationWithGate:
    """classify_quality integrated with real GateResult from DataQualityGate.check()."""

    def _make_data(self, **overrides: object) -> dict:
        base = {
            "symbol": "NIFTY",
            "timestamp": _RECENT_MS,
            "open": 22_000.0,
            "high": 22_200.0,
            "low": 21_900.0,
            "close": 22_100.0,
            "volume": 1_000_000,
            "eventTimeMs": _RECENT_MS,
            "providerAvailable": True,
            "confidenceScore": 85,
        }
        base.update(overrides)
        return base

    def test_valid_high_confidence_data_is_high_grade(self) -> None:
        data = self._make_data(confidenceScore=85)
        gate_result = DataQualityGate.check(data)
        qc = _engine.classify_quality(85, gate_result)
        assert qc.grade == "HIGH"
        assert qc.signalEngineAllowed is True
        assert qc.reasons == []

    def test_medium_confidence_data_is_medium_grade(self) -> None:
        data = self._make_data(confidenceScore=65)
        gate_result = DataQualityGate.check(data, min_confidence_score=30.0)
        qc = _engine.classify_quality(65, gate_result)
        assert qc.grade == "MEDIUM"

    def test_missing_field_failing_gate_blocks_signal_engine(self) -> None:
        """Missing required field → gate fails → signalEngineAllowed=False."""
        data = self._make_data(confidenceScore=80)
        data.pop("symbol")  # removes completeness
        gate_result = DataQualityGate.check(data)
        # Pass a high score — gate failure should still block
        qc = _engine.classify_quality(80, gate_result)
        assert qc.signalEngineAllowed is False
        assert len(qc.reasons) >= 1

    def test_blocked_confidence_score_is_blocked_grade(self) -> None:
        data = self._make_data(confidenceScore=20)
        gate_result = DataQualityGate.check(data)
        qc = _engine.classify_quality(20, gate_result)
        assert qc.grade == "BLOCKED"
        assert qc.signalEngineAllowed is False

    def test_stale_data_includes_freshness_reason(self) -> None:
        """Stale data → gate fails freshness → reason appears in classify_quality."""
        data = self._make_data(
            quoteAgeMs=60_000,
            freshnessFreshMs=10_000,
            confidenceScore=80,
        )
        gate_result = DataQualityGate.check(data)
        qc = _engine.classify_quality(80, gate_result)
        assert qc.signalEngineAllowed is False
        assert any("dataFresh" in r for r in qc.reasons)

    def test_returns_quality_classification_instance(self) -> None:
        data = self._make_data()
        gate_result = DataQualityGate.check(data)
        qc = _engine.classify_quality(85, gate_result)
        assert isinstance(qc, QualityClassification)
