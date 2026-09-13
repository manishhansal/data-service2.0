"""
Property 9 — BLOCKED Score Blocks Signal Engine

Any score < 30 must set signalEngineAllowed = False with no exceptions.

Requirement: 7.11
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.engines.quality_engine import QualityEngine


class TestBlockedScoreBlocksSignalEngine:
    @given(st.integers(min_value=0, max_value=29))
    @settings(max_examples=30)
    def test_score_below_30_always_blocks(self, score: int) -> None:
        """is_signal_engine_allowed must return False for any score < 30."""
        assert not QualityEngine.is_signal_engine_allowed(score), (
            f"Score {score} should block signal engine (threshold is 30)"
        )

    @given(st.integers(min_value=0, max_value=29))
    @settings(max_examples=30)
    def test_grade_below_30_is_blocked(self, score: int) -> None:
        """grade_score must return 'BLOCKED' for score < 30."""
        grade = QualityEngine.grade_score(score)
        assert grade == "BLOCKED", f"Expected 'BLOCKED' for score {score}, got '{grade}'"

    def test_score_30_is_not_blocked(self) -> None:
        """Boundary: score == 30 must not be blocked by the score check alone."""
        assert QualityEngine.is_signal_engine_allowed(30)

    @given(st.integers(min_value=30, max_value=95))
    @settings(max_examples=66)
    def test_scores_30_to_95_not_blocked_by_score_alone(self, score: int) -> None:
        """Scores 30–95 are not blocked at the score level."""
        assert QualityEngine.is_signal_engine_allowed(score)
