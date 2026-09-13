"""
tests/unit/engines/test_confidence_score.py

Unit tests for QualityEngine.compute_confidence_score,
QualityEngine.grade_score, and QualityEngine.compute_confidence_from_inputs.

Covers:
- Score is always in [0, 95] — never negative, never 100 (Property 5 / Req 7.1)
- FRESH + complete + healthy + valid timestamp + full agreement → high score
- EXPIRED freshness → low score
- Broken sequence integrity triggers 20% penalty
- score < 30 → grade BLOCKED; is_signal_engine_allowed returns False
- score ≥ 80 → grade HIGH
- Score is capped at 95 — never exceeds 95 regardless of perfect inputs
- grade_score covers all four bands (BLOCKED, LOW, MEDIUM, HIGH)
- compute_confidence_from_inputs correctly maps enum-style inputs
- Unknown freshness classification falls back to UNKNOWN score (5)

Requirements: 7.1, 9.1
"""

from __future__ import annotations

import pytest

from src.engines.quality_engine import QualityEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_engine = QualityEngine()


# ---------------------------------------------------------------------------
# compute_confidence_score — boundary / invariant tests
# ---------------------------------------------------------------------------


class TestComputeConfidenceScore:
    """Tests for the raw compute_confidence_score static method."""

    def test_perfect_inputs_capped_at_95(self) -> None:
        """Maximum theoretical score (35+25+20+10+10=100) must be capped at 95."""
        score = QualityEngine.compute_confidence_score(
            freshness_score=35,
            completeness_score=25,
            provider_health_score=20,
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity=True,
        )
        assert score == 95, "Perfect inputs must yield the capped maximum of 95"

    def test_score_never_exceeds_95(self) -> None:
        """Score must never exceed 95 under any input combination."""
        score = QualityEngine.compute_confidence_score(
            freshness_score=35,
            completeness_score=25,
            provider_health_score=20,
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity=True,
        )
        assert score <= 95

    def test_zero_inputs_yields_zero(self) -> None:
        """All zero/False inputs must yield a score of 0."""
        score = QualityEngine.compute_confidence_score(
            freshness_score=0,
            completeness_score=0,
            provider_health_score=0,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity=True,
        )
        assert score == 0

    def test_score_never_negative(self) -> None:
        """Score must never be negative, even when sequence penalty is applied."""
        score = QualityEngine.compute_confidence_score(
            freshness_score=0,
            completeness_score=0,
            provider_health_score=0,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity=False,  # 20% penalty on 0 = still 0
        )
        assert score >= 0

    def test_timestamp_valid_adds_10(self) -> None:
        """timestamp_valid=True must add 10 compared to timestamp_valid=False."""
        base_kwargs = dict(
            freshness_score=20,
            completeness_score=10,
            provider_health_score=10,
            cross_source_agreement=0.5,
            sequence_integrity=True,
        )
        score_valid = QualityEngine.compute_confidence_score(timestamp_valid=True, **base_kwargs)
        score_invalid = QualityEngine.compute_confidence_score(timestamp_valid=False, **base_kwargs)
        assert score_valid - score_invalid == 10

    def test_timestamp_invalid_adds_0(self) -> None:
        """timestamp_valid=False must contribute 0 to the timestamp component."""
        score = QualityEngine.compute_confidence_score(
            freshness_score=0,
            completeness_score=0,
            provider_health_score=0,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity=True,
        )
        assert score == 0

    def test_agreement_score_scaling(self) -> None:
        """cross_source_agreement=0.7 must contribute int(0.7 * 10) = 7."""
        score = QualityEngine.compute_confidence_score(
            freshness_score=0,
            completeness_score=0,
            provider_health_score=0,
            timestamp_valid=False,
            cross_source_agreement=0.7,
            sequence_integrity=True,
        )
        assert score == 7

    def test_agreement_score_full(self) -> None:
        """cross_source_agreement=1.0 must contribute 10."""
        score = QualityEngine.compute_confidence_score(
            freshness_score=0,
            completeness_score=0,
            provider_health_score=0,
            timestamp_valid=False,
            cross_source_agreement=1.0,
            sequence_integrity=True,
        )
        assert score == 10

    def test_sequence_integrity_penalty_applied(self) -> None:
        """Broken sequence integrity must apply int(score * 0.8) penalty."""
        # Raw sum = 35 + 25 + 20 + 10 + 10 = 100 → capped to 95 before penalty?
        # No: penalty is applied BEFORE capping per spec.
        # 100 * 0.8 = 80 → then capped to min(80, 95) = 80.
        score = QualityEngine.compute_confidence_score(
            freshness_score=35,
            completeness_score=25,
            provider_health_score=20,
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity=False,
        )
        expected = min(int(100 * 0.8), 95)  # int(80) = 80
        assert score == expected, f"Expected {expected}, got {score}"

    def test_sequence_integrity_penalty_on_mid_score(self) -> None:
        """20% penalty must be applied to a mid-range score before capping."""
        # Raw = 35 + 15 + 10 + 10 + 5 = 75; with penalty: int(75 * 0.8) = 60
        score = QualityEngine.compute_confidence_score(
            freshness_score=35,
            completeness_score=15,
            provider_health_score=10,
            timestamp_valid=True,
            cross_source_agreement=0.5,
            sequence_integrity=False,
        )
        expected = int(75 * 0.8)  # 60
        assert score == expected

    def test_sequence_integrity_ok_no_penalty(self) -> None:
        """When sequence_integrity=True, no penalty must be applied."""
        score_ok = QualityEngine.compute_confidence_score(
            freshness_score=35,
            completeness_score=25,
            provider_health_score=20,
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity=True,
        )
        score_broken = QualityEngine.compute_confidence_score(
            freshness_score=35,
            completeness_score=25,
            provider_health_score=20,
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity=False,
        )
        assert score_ok > score_broken

    def test_fresh_complete_healthy_yields_high_score(self) -> None:
        """FRESH + full completeness + healthy provider + valid ts + full agreement
        must yield a score in the HIGH band (≥ 80)."""
        score = QualityEngine.compute_confidence_score(
            freshness_score=35,   # FRESH
            completeness_score=25,  # 100% completeness
            provider_health_score=20,  # healthy
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity=True,
        )
        assert score >= 80, f"Expected HIGH score (≥80) for perfect inputs, got {score}"

    def test_expired_freshness_yields_low_score(self) -> None:
        """EXPIRED freshness (score 0) with otherwise mediocre inputs must yield
        a score below 80 (not HIGH)."""
        score = QualityEngine.compute_confidence_score(
            freshness_score=0,   # EXPIRED
            completeness_score=10,
            provider_health_score=10,
            timestamp_valid=True,
            cross_source_agreement=0.5,
            sequence_integrity=True,
        )
        assert score < 80


# ---------------------------------------------------------------------------
# grade_score
# ---------------------------------------------------------------------------


class TestGradeScore:
    """Tests for the grade_score static method."""

    @pytest.mark.parametrize("score", [0, 1, 15, 29])
    def test_grade_blocked(self, score: int) -> None:
        """Scores below 30 must return BLOCKED."""
        assert QualityEngine.grade_score(score) == "BLOCKED"

    @pytest.mark.parametrize("score", [30, 35, 49])
    def test_grade_low(self, score: int) -> None:
        """Scores in [30, 49] must return LOW."""
        assert QualityEngine.grade_score(score) == "LOW"

    @pytest.mark.parametrize("score", [50, 60, 79])
    def test_grade_medium(self, score: int) -> None:
        """Scores in [50, 79] must return MEDIUM."""
        assert QualityEngine.grade_score(score) == "MEDIUM"

    @pytest.mark.parametrize("score", [80, 90, 95])
    def test_grade_high(self, score: int) -> None:
        """Scores in [80, 95] must return HIGH."""
        assert QualityEngine.grade_score(score) == "HIGH"

    def test_grade_boundary_30_is_low_not_blocked(self) -> None:
        """Score of exactly 30 is LOW, not BLOCKED."""
        assert QualityEngine.grade_score(30) == "LOW"

    def test_grade_boundary_50_is_medium_not_low(self) -> None:
        """Score of exactly 50 is MEDIUM, not LOW."""
        assert QualityEngine.grade_score(50) == "MEDIUM"

    def test_grade_boundary_80_is_high_not_medium(self) -> None:
        """Score of exactly 80 is HIGH, not MEDIUM."""
        assert QualityEngine.grade_score(80) == "HIGH"


# ---------------------------------------------------------------------------
# is_signal_engine_allowed
# ---------------------------------------------------------------------------


class TestIsSignalEngineAllowed:
    """Tests for the is_signal_engine_allowed helper (Requirement 7.11)."""

    @pytest.mark.parametrize("score", [0, 1, 20, 29])
    def test_blocked_scores_disallow_signal_engine(self, score: int) -> None:
        """Any score below 30 must return False — no exceptions (Requirement 7.11)."""
        assert QualityEngine.is_signal_engine_allowed(score) is False

    @pytest.mark.parametrize("score", [30, 50, 80, 95])
    def test_non_blocked_scores_allow_signal_engine(self, score: int) -> None:
        """Scores of 30 and above must return True."""
        assert QualityEngine.is_signal_engine_allowed(score) is True

    def test_score_29_disallows(self) -> None:
        """Score 29 (one below threshold) must be blocked."""
        assert QualityEngine.is_signal_engine_allowed(29) is False

    def test_score_30_allows(self) -> None:
        """Score 30 (exactly at threshold) must be allowed."""
        assert QualityEngine.is_signal_engine_allowed(30) is True

    def test_perfect_score_allows(self) -> None:
        """Max score 95 must allow signal engine."""
        assert QualityEngine.is_signal_engine_allowed(95) is True

    def test_zero_score_disallows(self) -> None:
        """Zero score (worst case) must block signal engine."""
        assert QualityEngine.is_signal_engine_allowed(0) is False


# ---------------------------------------------------------------------------
# compute_confidence_from_inputs — high-level convenience method
# ---------------------------------------------------------------------------


class TestComputeConfidenceFromInputs:
    """Tests for the higher-level compute_confidence_from_inputs method."""

    def test_fresh_maps_to_35(self) -> None:
        """freshness_classification='FRESH' must contribute 35 to the score."""
        # Isolate freshness component: all other inputs are zero/False
        score_fresh = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="FRESH",
            completeness_percent=0.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score_fresh == 35

    def test_aging_maps_to_25(self) -> None:
        """freshness_classification='AGING' must contribute 25."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="AGING",
            completeness_percent=0.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score == 25

    def test_stale_maps_to_10(self) -> None:
        """freshness_classification='STALE' must contribute 10."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="STALE",
            completeness_percent=0.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score == 10

    def test_expired_maps_to_0(self) -> None:
        """freshness_classification='EXPIRED' must contribute 0."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="EXPIRED",
            completeness_percent=0.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score == 0

    def test_unknown_maps_to_5(self) -> None:
        """freshness_classification='UNKNOWN' must contribute 5."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="UNKNOWN",
            completeness_percent=0.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score == 5

    def test_unrecognised_freshness_falls_back_to_unknown(self) -> None:
        """Unrecognised freshness strings must fall back to UNKNOWN score (5)."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="INVALID_VALUE",
            completeness_percent=0.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score == 5  # same as UNKNOWN

    def test_provider_healthy_true_adds_20(self) -> None:
        """provider_healthy=True must contribute 20 to provider_score."""
        score_healthy = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="EXPIRED",
            completeness_percent=0.0,
            provider_healthy=True,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score_healthy == 20

    def test_provider_healthy_false_adds_0(self) -> None:
        """provider_healthy=False must contribute 0 to provider_score."""
        score_unhealthy = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="EXPIRED",
            completeness_percent=0.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score_unhealthy == 0

    def test_completeness_100_percent_maps_to_25(self) -> None:
        """completeness_percent=100 must yield completeness_score=25."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="EXPIRED",
            completeness_percent=100.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score == 25

    def test_completeness_50_percent_maps_to_12(self) -> None:
        """completeness_percent=50 must yield completeness_score=int(50*0.25)=12."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="EXPIRED",
            completeness_percent=50.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score == 12

    def test_all_best_inputs_capped_at_95(self) -> None:
        """Maximum inputs via the high-level method must also cap at 95."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="FRESH",
            completeness_percent=100.0,
            provider_healthy=True,
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity_ok=True,
        )
        assert score == 95

    def test_expired_freshness_yields_low_score(self) -> None:
        """EXPIRED freshness must yield a low score even with mediocre other inputs."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="EXPIRED",
            completeness_percent=40.0,
            provider_healthy=True,
            timestamp_valid=True,
            cross_source_agreement=0.5,
            sequence_integrity_ok=True,
        )
        # 0 + int(40*0.25) + 20 + 10 + 5 = 0+10+20+10+5 = 45
        assert score == 45
        assert score < 80  # not HIGH band

    def test_sequence_integrity_false_applies_penalty(self) -> None:
        """sequence_integrity_ok=False must apply 20% penalty."""
        score_ok = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="FRESH",
            completeness_percent=100.0,
            provider_healthy=True,
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity_ok=True,
        )
        score_broken = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="FRESH",
            completeness_percent=100.0,
            provider_healthy=True,
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity_ok=False,
        )
        assert score_broken < score_ok

    def test_score_output_always_in_range(self) -> None:
        """All representative input combinations must produce scores in [0, 95]."""
        test_cases = [
            ("FRESH", 100.0, True, True, 1.0, True),
            ("EXPIRED", 0.0, False, False, 0.0, False),
            ("STALE", 50.0, False, True, 0.3, False),
            ("AGING", 75.0, True, False, 0.8, True),
            ("UNKNOWN", 0.0, True, True, 0.0, True),
        ]
        for freshness, completeness, healthy, ts_valid, agreement, seq_ok in test_cases:
            score = QualityEngine.compute_confidence_from_inputs(
                freshness_classification=freshness,
                completeness_percent=completeness,
                provider_healthy=healthy,
                timestamp_valid=ts_valid,
                cross_source_agreement=agreement,
                sequence_integrity_ok=seq_ok,
            )
            assert 0 <= score <= 95, (
                f"Score {score} out of [0, 95] for inputs: "
                f"freshness={freshness}, completeness={completeness}, "
                f"healthy={healthy}, ts_valid={ts_valid}, "
                f"agreement={agreement}, seq_ok={seq_ok}"
            )

    def test_blocked_score_disallows_signal_engine(self) -> None:
        """A score < 30 from compute_confidence_from_inputs must block signal engine."""
        # EXPIRED(0) + 0% completeness + unhealthy(0) + invalid ts(0) + 0 agreement = 0
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="EXPIRED",
            completeness_percent=0.0,
            provider_healthy=False,
            timestamp_valid=False,
            cross_source_agreement=0.0,
            sequence_integrity_ok=True,
        )
        assert score < 30
        assert QualityEngine.is_signal_engine_allowed(score) is False

    def test_high_score_allows_signal_engine(self) -> None:
        """A score ≥ 80 from compute_confidence_from_inputs must allow signal engine."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification="FRESH",
            completeness_percent=100.0,
            provider_healthy=True,
            timestamp_valid=True,
            cross_source_agreement=1.0,
            sequence_integrity_ok=True,
        )
        assert score >= 80
        assert QualityEngine.is_signal_engine_allowed(score) is True
