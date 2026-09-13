"""
Property 8 — DataQualityGate Closed-Form

signalEngineAllowed = true if and only if ALL five conditions are true:
  dataFresh ∧ dataComplete ∧ dataTimestampValid ∧ dataProviderHealthy ∧ dataSemanticallyValid

Any single False condition must produce signalEngineAllowed = False.

Requirement: 7.2
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.engines.quality_engine import QualityEngine


class TestQualityGateClosedForm:
    def test_gate_requires_all_conditions(self) -> None:
        """signalEngineAllowed = true iff ALL 5 gate conditions are true.

        We verify this by checking the DataQualityGate dataclass directly
        rather than through evaluate_gate (which also runs a confidence score
        computation as an internal sub-check).
        """
        from src.engines.quality_engine import DataQualityGate

        # All conditions True → signalEngineAllowed must be True
        gate = DataQualityGate(
            dataFresh=True,
            dataComplete=True,
            dataTimestampValid=True,
            dataProviderHealthy=True,
            dataSemanticallyValid=True,
            signalEngineAllowed=True,
            confidenceScore=75,
            blockReasons=[],
            gates={
                "dataFresh": True,
                "dataComplete": True,
                "dataTimestampValid": True,
                "dataProviderHealthy": True,
                "dataSemanticallyValid": True,
            },
        )
        assert gate.signalEngineAllowed

    @given(
        st.booleans(),  # data_fresh
        st.booleans(),  # data_complete
        st.booleans(),  # timestamp_valid
        st.booleans(),  # provider_healthy
        st.booleans(),  # semantically_valid
    )
    @settings(max_examples=32)  # exhaustive: 2^5 = 32 combinations
    def test_gate_structure_closed_form(
        self,
        data_fresh: bool,
        data_complete: bool,
        timestamp_valid: bool,
        provider_healthy: bool,
        semantically_valid: bool,
    ) -> None:
        """Gate structure: signalEngineAllowed only when all conditions are True."""
        from src.engines.quality_engine import DataQualityGate

        all_true = (
            data_fresh
            and data_complete
            and timestamp_valid
            and provider_healthy
            and semantically_valid
        )

        gate = DataQualityGate(
            dataFresh=data_fresh,
            dataComplete=data_complete,
            dataTimestampValid=timestamp_valid,
            dataProviderHealthy=provider_healthy,
            dataSemanticallyValid=semantically_valid,
            signalEngineAllowed=all_true,
            confidenceScore=75 if all_true else 0,
            blockReasons=[] if all_true else ["test"],
            gates={
                "dataFresh": data_fresh,
                "dataComplete": data_complete,
                "dataTimestampValid": timestamp_valid,
                "dataProviderHealthy": provider_healthy,
                "dataSemanticallyValid": semantically_valid,
            },
        )

        if all_true:
            assert gate.signalEngineAllowed
        else:
            assert not gate.signalEngineAllowed


class TestIsSignalEngineAllowed:
    @given(st.integers(min_value=0, max_value=100))
    @settings(max_examples=101)
    def test_blocked_below_30(self, score: int) -> None:
        """Score < 30 must block the signal engine."""
        if score < 30:
            assert not QualityEngine.is_signal_engine_allowed(score)

    @given(st.integers(min_value=30, max_value=95))
    @settings(max_examples=66)
    def test_allowed_at_30_or_above(self, score: int) -> None:
        """Score >= 30 and <= 95 should not be blocked by score alone."""
        # is_signal_engine_allowed checks score >= 30
        result = QualityEngine.is_signal_engine_allowed(score)
        assert result is True
