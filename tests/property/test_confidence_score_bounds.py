"""
Property 5 — DataConfidenceScore Bounds

The confidence score must always be in [0, 95] — never negative, never 100,
never > 95. The 95 cap reflects inherent market-data uncertainty.

Requirement: 7.1
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.engines.quality_engine import QualityEngine


completeness_st = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)
bool_st = st.booleans()
agreement_st = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)

FRESHNESS_STATUSES = ["LIVE", "NEAR_LIVE", "STALE", "VERY_STALE", "UNKNOWN"]


class TestConfidenceScoreBounds:
    @given(
        st.sampled_from(FRESHNESS_STATUSES),
        completeness_st,
        bool_st,
        bool_st,
        agreement_st,
        bool_st,
    )
    @settings(max_examples=200)
    def test_score_always_in_0_to_95(
        self,
        freshness: str,
        completeness_pct: float,
        provider_healthy: bool,
        timestamp_valid: bool,
        cross_source_agreement: float,
        sequence_ok: bool,
    ) -> None:
        """Score is always in [0, 95]."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification=freshness,
            completeness_percent=completeness_pct,
            provider_healthy=provider_healthy,
            timestamp_valid=timestamp_valid,
            cross_source_agreement=cross_source_agreement,
            sequence_integrity_ok=sequence_ok,
        )
        assert isinstance(score, int)
        assert 0 <= score <= 95, f"Score {score} out of [0, 95] bounds"

    @given(
        st.sampled_from(FRESHNESS_STATUSES),
        completeness_st,
        bool_st,
        bool_st,
        agreement_st,
        bool_st,
    )
    @settings(max_examples=100)
    def test_score_never_100(
        self,
        freshness: str,
        completeness_pct: float,
        provider_healthy: bool,
        timestamp_valid: bool,
        cross_source_agreement: float,
        sequence_ok: bool,
    ) -> None:
        """Score never reaches 100 — max is 95."""
        score = QualityEngine.compute_confidence_from_inputs(
            freshness_classification=freshness,
            completeness_percent=completeness_pct,
            provider_healthy=provider_healthy,
            timestamp_valid=timestamp_valid,
            cross_source_agreement=cross_source_agreement,
            sequence_integrity_ok=sequence_ok,
        )
        assert score != 100, "Score must never be 100 (95 is the maximum)"
        assert score <= 95
