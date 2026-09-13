"""
tests/unit/engines/test_strategy_overrides.py

Unit tests for QualityEngine.apply_strategy_overrides (task 9.6) and the
StrategyQualityProfile Pydantic v2 model.

Covers:
  - StrategyQualityProfile model structure and field validation
  - BLOCKED floor (score < 30) always blocks regardless of profile
  - minConfidenceScore: advisory reason added when score in [30, profile.min)
  - minConfidenceScore below 30 is clamped to 30 (BLOCKED floor is preserved)
  - requireFreshData=True + freshness failure forces signalEngineAllowed=False
  - requireFreshData=False allows the stale gate failure to pass through
  - Overrides never relax a global gate failure (not related to freshness)
  - Base signalEngineAllowed is preserved when no override condition fires
  - Returned QualityClassification has correct grade, score, and reasons

Requirements: 7.8, 7.11
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from src.engines.quality_engine import (
    DataQualityGate,
    GateResult,
    QualityClassification,
    QualityEngine,
    StrategyQualityProfile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_engine = QualityEngine()
_NOW_MS = int(time.time() * 1000)
_RECENT_MS = _NOW_MS - 5_000        # 5 seconds ago — always fresh


def _passing_gate(score: int = 80) -> GateResult:
    """GateResult where all five conditions passed."""
    return GateResult(passed=True, failed_conditions=[], score=float(score))


def _failing_gate_freshness(score: int = 80) -> GateResult:
    """GateResult where dataFresh condition failed."""
    # Build a real gate so that gate.dataFresh is correctly set to False
    data: dict[str, Any] = {
        "symbol": "NIFTY",
        "timestamp": _RECENT_MS,
        "open": 22_000.0,
        "high": 22_200.0,
        "low": 21_900.0,
        "close": 22_100.0,
        "volume": 1_000_000,
        "eventTimeMs": _RECENT_MS,
        "providerAvailable": True,
        "confidenceScore": score,
        # Force freshness to fail: 60s old but window is 1s
        "quoteAgeMs": 60_000,
        "freshnessFreshMs": 1_000,
    }
    return DataQualityGate.check(data, min_confidence_score=float(score))


def _failing_gate_completeness(score: int = 80) -> GateResult:
    """GateResult where dataComplete condition failed (symbol missing)."""
    data: dict[str, Any] = {
        # "symbol" intentionally omitted
        "timestamp": _RECENT_MS,
        "open": 22_000.0,
        "high": 22_200.0,
        "low": 21_900.0,
        "close": 22_100.0,
        "volume": 1_000_000,
        "eventTimeMs": _RECENT_MS,
        "providerAvailable": True,
        "confidenceScore": score,
    }
    return DataQualityGate.check(data, min_confidence_score=float(score))


def _default_profile(**overrides: Any) -> StrategyQualityProfile:
    base = {
        "strategyId": "test-strategy",
        "minConfidenceScore": 60,
        "requireFreshData": True,
        "allowedFreshnessStatuses": ["FRESH", "AGING"],
    }
    base.update(overrides)
    return StrategyQualityProfile(**base)


# ---------------------------------------------------------------------------
# StrategyQualityProfile model
# ---------------------------------------------------------------------------


class TestStrategyQualityProfileModel:
    """Pydantic v2 model validation for StrategyQualityProfile."""

    def test_minimal_construction(self) -> None:
        profile = StrategyQualityProfile(strategyId="my-strategy")
        assert profile.strategyId == "my-strategy"
        assert profile.minConfidenceScore == 60
        assert profile.requireFreshData is True
        assert profile.allowedFreshnessStatuses == ["FRESH", "AGING"]
        assert profile.maxOIVariancePercent is None
        assert profile.minLiquidityVolume is None

    def test_full_construction(self) -> None:
        profile = StrategyQualityProfile(
            strategyId="full-strategy",
            minConfidenceScore=75,
            requireFreshData=False,
            allowedFreshnessStatuses=["FRESH"],
            maxOIVariancePercent=5.0,
            minLiquidityVolume=100_000.0,
        )
        assert profile.minConfidenceScore == 75
        assert profile.requireFreshData is False
        assert profile.allowedFreshnessStatuses == ["FRESH"]
        assert profile.maxOIVariancePercent == 5.0
        assert profile.minLiquidityVolume == 100_000.0

    def test_frozen_immutable(self) -> None:
        profile = _default_profile()
        with pytest.raises(Exception):
            profile.minConfidenceScore = 99  # type: ignore[misc]

    def test_min_confidence_score_lower_bound(self) -> None:
        """minConfidenceScore=0 is valid at the model level."""
        profile = StrategyQualityProfile(strategyId="s", minConfidenceScore=0)
        assert profile.minConfidenceScore == 0

    def test_min_confidence_score_upper_bound(self) -> None:
        """minConfidenceScore=95 is valid at the model level."""
        profile = StrategyQualityProfile(strategyId="s", minConfidenceScore=95)
        assert profile.minConfidenceScore == 95

    def test_min_confidence_score_above_95_rejected(self) -> None:
        with pytest.raises(Exception):
            StrategyQualityProfile(strategyId="s", minConfidenceScore=96)

    def test_min_confidence_score_below_0_rejected(self) -> None:
        with pytest.raises(Exception):
            StrategyQualityProfile(strategyId="s", minConfidenceScore=-1)

    def test_max_oi_variance_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            StrategyQualityProfile(strategyId="s", maxOIVariancePercent=-1.0)

    def test_min_liquidity_volume_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            StrategyQualityProfile(strategyId="s", minLiquidityVolume=-1.0)

    def test_allowed_freshness_statuses_default(self) -> None:
        profile = StrategyQualityProfile(strategyId="s")
        assert "FRESH" in profile.allowedFreshnessStatuses

    def test_strategy_id_is_required(self) -> None:
        with pytest.raises(Exception):
            StrategyQualityProfile()  # type: ignore[call-arg]

    def test_returns_quality_classification_instance(self) -> None:
        profile = _default_profile()
        gate = _passing_gate(80)
        result = _engine.apply_strategy_overrides(80, gate, profile)
        assert isinstance(result, QualityClassification)


# ---------------------------------------------------------------------------
# BLOCKED floor — always enforced (Requirement 7.11)
# ---------------------------------------------------------------------------


class TestBlockedFloor:
    """Score < 30 always blocks regardless of any profile setting."""

    @pytest.mark.parametrize("score", [0, 1, 15, 29])
    def test_blocked_score_always_false(self, score: int) -> None:
        """apply_strategy_overrides must return signalEngineAllowed=False for score < 30."""
        profile = StrategyQualityProfile(
            strategyId="permissive",
            minConfidenceScore=0,      # most permissive possible
            requireFreshData=False,
        )
        gate = _passing_gate(score)
        result = _engine.apply_strategy_overrides(score, gate, profile)
        assert result.signalEngineAllowed is False, (
            f"BLOCKED invariant violated: score={score} should never allow signal engine"
        )

    def test_blocked_score_grade_is_blocked(self) -> None:
        profile = _default_profile()
        gate = _passing_gate(20)
        result = _engine.apply_strategy_overrides(20, gate, profile)
        assert result.grade == "BLOCKED"

    def test_blocked_score_not_affected_by_require_fresh_false(self) -> None:
        profile = StrategyQualityProfile(
            strategyId="s",
            requireFreshData=False,
            minConfidenceScore=0,
        )
        gate = _passing_gate(0)
        result = _engine.apply_strategy_overrides(0, gate, profile)
        assert result.signalEngineAllowed is False

    def test_score_exactly_29_is_blocked(self) -> None:
        profile = _default_profile(minConfidenceScore=0, requireFreshData=False)
        gate = _passing_gate(29)
        result = _engine.apply_strategy_overrides(29, gate, profile)
        assert result.signalEngineAllowed is False

    def test_score_exactly_30_with_permissive_profile_allows(self) -> None:
        """Score=30 is the exact boundary — should be allowed with a permissive profile."""
        profile = StrategyQualityProfile(
            strategyId="s",
            minConfidenceScore=30,
            requireFreshData=False,
        )
        gate = _passing_gate(30)
        result = _engine.apply_strategy_overrides(30, gate, profile)
        assert result.signalEngineAllowed is True


# ---------------------------------------------------------------------------
# minConfidenceScore advisory
# ---------------------------------------------------------------------------


class TestMinConfidenceScoreAdvisory:
    """Strategy minConfidenceScore adds a reason but does not block by itself."""

    def test_score_below_profile_min_adds_reason(self) -> None:
        """Score in [30, profile.minConfidenceScore) → reason added."""
        profile = _default_profile(minConfidenceScore=80)
        gate = _passing_gate(60)   # base gate passes (score >= 30)
        result = _engine.apply_strategy_overrides(60, gate, profile)
        assert any("minConfidenceScore" in r or "test-strategy" in r for r in result.reasons)

    def test_score_below_profile_min_does_not_block_alone(self) -> None:
        """Score in [30, profile.min) with all gates passing → signal still allowed.

        The strategy advisory is informational; only freshness failures with
        requireFreshData=True actively block.
        """
        profile = _default_profile(minConfidenceScore=90)
        gate = _passing_gate(50)   # base passes (score=50 >= 30, all gates pass)
        result = _engine.apply_strategy_overrides(50, gate, profile)
        # The advisory should be in reasons but signal is still allowed
        assert result.signalEngineAllowed is True
        assert any("test-strategy" in r for r in result.reasons)

    def test_score_at_profile_min_no_extra_reason(self) -> None:
        """Score exactly at profile.minConfidenceScore — no advisory added."""
        profile = _default_profile(minConfidenceScore=70)
        gate = _passing_gate(70)
        result = _engine.apply_strategy_overrides(70, gate, profile)
        # Should not add a strategy reason about confidence score
        assert not any("minConfidenceScore" in r for r in result.reasons)

    def test_score_above_profile_min_no_extra_reason(self) -> None:
        """Score above profile.minConfidenceScore — no advisory added."""
        profile = _default_profile(minConfidenceScore=60)
        gate = _passing_gate(85)
        result = _engine.apply_strategy_overrides(85, gate, profile)
        assert not any("minConfidenceScore" in r for r in result.reasons)

    def test_advisory_reason_mentions_strategy_id(self) -> None:
        """The advisory reason must reference the strategy ID for traceability."""
        profile = _default_profile(strategyId="my-scalper", minConfidenceScore=80)
        gate = _passing_gate(50)
        result = _engine.apply_strategy_overrides(50, gate, profile)
        assert any("my-scalper" in r for r in result.reasons)

    def test_advisory_reason_mentions_scores(self) -> None:
        """The advisory reason must mention both the actual and required score."""
        profile = _default_profile(strategyId="s", minConfidenceScore=80)
        gate = _passing_gate(50)
        result = _engine.apply_strategy_overrides(50, gate, profile)
        combined = " ".join(result.reasons)
        assert "80" in combined   # required score
        assert "50" in combined   # actual score

    def test_profile_min_below_30_clamped_to_30(self) -> None:
        """minConfidenceScore values below 30 are treated as 30 at runtime.

        Even if the model allowed 0 at field level, the BLOCKED floor (30)
        must never be bypassed via a profile setting.
        """
        profile = StrategyQualityProfile(
            strategyId="edge",
            minConfidenceScore=0,   # min allowed by the model
            requireFreshData=False,
        )
        gate = _passing_gate(40)
        result = _engine.apply_strategy_overrides(40, gate, profile)
        # score=40 >= 30 (effective min after clamping) → no advisory reason
        assert not any("minConfidenceScore" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# requireFreshData + freshness gate interaction
# ---------------------------------------------------------------------------


class TestRequireFreshData:
    """requireFreshData interacts with the gate's freshness condition."""

    def test_require_fresh_true_and_freshness_fails_blocks(self) -> None:
        """requireFreshData=True + gate freshness failed → signalEngineAllowed=False."""
        profile = _default_profile(requireFreshData=True)
        gate = _failing_gate_freshness(score=80)
        result = _engine.apply_strategy_overrides(80, gate, profile)
        assert result.signalEngineAllowed is False

    def test_require_fresh_true_and_freshness_fails_adds_reason(self) -> None:
        """A strategy-specific reason must appear when freshness forces a block."""
        profile = _default_profile(
            strategyId="fresh-required",
            requireFreshData=True,
        )
        gate = _failing_gate_freshness(score=80)
        result = _engine.apply_strategy_overrides(80, gate, profile)
        assert any("requireFreshData" in r or "fresh-required" in r for r in result.reasons)

    def test_require_fresh_false_and_freshness_fails_does_not_block(self) -> None:
        """requireFreshData=False → freshness failure does NOT force signalEngineAllowed=False.

        The base gate might still block for other reasons, but the strategy
        override does not add an additional block.
        """
        profile = _default_profile(requireFreshData=False)
        # Build a gate where freshness failed but everything else passes
        # We need to check: does the strategy override not add an extra block?
        gate = _failing_gate_freshness(score=80)
        # The base gate already has passed=False (because freshness failed).
        # With requireFreshData=False, the override should not add another reason.
        result_with = _engine.apply_strategy_overrides(80, gate, profile)
        profile_require_fresh = _default_profile(requireFreshData=True)
        result_without = _engine.apply_strategy_overrides(80, gate, profile_require_fresh)

        # Both are blocked because base gate.passed=False for other reasons when
        # freshness fails. But with requireFreshData=False, no *strategy-specific*
        # freshness reason is added.
        fresh_reasons_with = [
            r for r in result_with.reasons if "requireFreshData" in r
        ]
        fresh_reasons_without = [
            r for r in result_without.reasons if "requireFreshData" in r
        ]
        # requireFreshData=False → no extra strategy reason
        assert len(fresh_reasons_with) == 0
        # requireFreshData=True → strategy reason added
        assert len(fresh_reasons_without) >= 1

    def test_require_fresh_true_and_freshness_passes_no_extra_block(self) -> None:
        """requireFreshData=True + freshness passes → no extra reason added."""
        profile = _default_profile(requireFreshData=True)
        gate = _passing_gate(80)   # all conditions pass, including freshness
        result = _engine.apply_strategy_overrides(80, gate, profile)
        # requireFreshData=True but data is fresh → no reason should be added
        assert not any("requireFreshData" in r for r in result.reasons)

    def test_require_fresh_true_freshness_fails_reason_contains_strategy_id(self) -> None:
        profile = _default_profile(
            strategyId="expiry-strategy",
            requireFreshData=True,
        )
        gate = _failing_gate_freshness(score=80)
        result = _engine.apply_strategy_overrides(80, gate, profile)
        assert any("expiry-strategy" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Overrides never relax other global gate failures
# ---------------------------------------------------------------------------


class TestNoRelaxationOfGlobalFailures:
    """Strategy overrides cannot relax non-freshness global gate failures."""

    def test_completeness_failure_not_relaxed_by_profile(self) -> None:
        """If the base gate failed completeness, the result stays blocked."""
        profile = StrategyQualityProfile(
            strategyId="s",
            minConfidenceScore=30,
            requireFreshData=False,   # even with fresh not required
        )
        gate = _failing_gate_completeness(score=80)
        result = _engine.apply_strategy_overrides(80, gate, profile)
        assert result.signalEngineAllowed is False

    def test_provider_failure_not_relaxed_by_profile(self) -> None:
        """Provider availability failure in gate is preserved."""
        profile = StrategyQualityProfile(
            strategyId="s",
            minConfidenceScore=30,
            requireFreshData=False,
        )
        # Simulate a gate that failed provider availability
        gate = GateResult(
            passed=False,
            failed_conditions=["dataProviderHealthy=False: no available source"],
            score=80.0,
        )
        result = _engine.apply_strategy_overrides(80, gate, profile)
        assert result.signalEngineAllowed is False

    def test_base_gate_failure_reasons_preserved(self) -> None:
        """Reasons from the base gate are always present in the override result."""
        profile = _default_profile()
        base_reason = "dataComplete=False: required fields missing: symbol"
        gate = GateResult(
            passed=False,
            failed_conditions=[base_reason],
            score=80.0,
        )
        result = _engine.apply_strategy_overrides(80, gate, profile)
        assert base_reason in result.reasons


# ---------------------------------------------------------------------------
# Grade and score are preserved
# ---------------------------------------------------------------------------


class TestGradeAndScorePreserved:
    """grade and score in the result always match the input score."""

    @pytest.mark.parametrize("score,expected_grade", [
        (0, "BLOCKED"),
        (29, "BLOCKED"),
        (30, "LOW"),
        (49, "LOW"),
        (50, "MEDIUM"),
        (79, "MEDIUM"),
        (80, "HIGH"),
        (95, "HIGH"),
    ])
    def test_grade_matches_score(self, score: int, expected_grade: str) -> None:
        profile = _default_profile(minConfidenceScore=0, requireFreshData=False)
        gate = _passing_gate(score)
        result = _engine.apply_strategy_overrides(score, gate, profile)
        assert result.grade == expected_grade, (
            f"score={score} → expected grade={expected_grade}, got {result.grade}"
        )

    @pytest.mark.parametrize("score", [0, 30, 50, 80, 95])
    def test_score_field_unchanged(self, score: int) -> None:
        profile = _default_profile()
        gate = _passing_gate(score)
        result = _engine.apply_strategy_overrides(score, gate, profile)
        assert result.score == score


# ---------------------------------------------------------------------------
# No-op path — result unchanged when no override fires
# ---------------------------------------------------------------------------


class TestNoOpPath:
    """When no override condition fires, the result matches the base classification."""

    def test_high_score_passing_gate_no_overrides_matches_base(self) -> None:
        profile = _default_profile(minConfidenceScore=60)
        gate = _passing_gate(85)
        result = _engine.apply_strategy_overrides(85, gate, profile)
        base = _engine.classify_quality(85, gate)
        assert result.signalEngineAllowed == base.signalEngineAllowed
        assert result.grade == base.grade
        assert result.score == base.score

    def test_empty_reasons_when_no_overrides_for_high_score(self) -> None:
        profile = _default_profile(minConfidenceScore=60)
        gate = _passing_gate(90)
        result = _engine.apply_strategy_overrides(90, gate, profile)
        assert result.reasons == []


# ---------------------------------------------------------------------------
# Interaction — minConfidenceScore advisory combined with requireFreshData block
# ---------------------------------------------------------------------------


class TestCombinedOverrides:
    """Both minConfidenceScore advisory and requireFreshData block can fire together."""

    def test_both_override_conditions_fire(self) -> None:
        """Score below strategy min AND freshness failed → both reasons in output."""
        profile = _default_profile(
            strategyId="strict-strategy",
            minConfidenceScore=90,
            requireFreshData=True,
        )
        # Score = 50: below profile min (90) but >= 30
        # Freshness gate fails
        gate = _failing_gate_freshness(score=50)
        result = _engine.apply_strategy_overrides(50, gate, profile)

        assert result.signalEngineAllowed is False

        # Should have at least the freshness strategy reason
        strategy_reasons = [r for r in result.reasons if "strict-strategy" in r]
        assert len(strategy_reasons) >= 1

    def test_min_confidence_advisory_does_not_double_block(self) -> None:
        """When score >= 30 and all gates pass, only freshness can actively block.

        The minConfidenceScore advisory must not flip signalEngineAllowed to False.
        """
        profile = _default_profile(minConfidenceScore=90, requireFreshData=False)
        gate = _passing_gate(50)   # all pass, score=50 in [30, 90)
        result = _engine.apply_strategy_overrides(50, gate, profile)
        # Advisory reason present but signal still allowed
        assert result.signalEngineAllowed is True
        assert any("strict" in r or "test-strategy" in r or "50" in r for r in result.reasons)
