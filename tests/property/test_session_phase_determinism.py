"""
Property 6 — NSE Session Phase Determinism

For the same UTC timestamp, get_phase() must always return the same SessionPhase.
The phase classification is pure and deterministic — no side effects, no randomness.

Requirement: 12.2
"""
from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from src.engines.market_session import MarketSessionEngine, SessionPhase


# Epoch seconds: 2023-01-01 to 2027-01-01 (covers market + non-market hours)
EPOCH_MIN = 1_672_531_200  # 2023-01-01 00:00:00 UTC
EPOCH_MAX = 1_798_761_600  # 2027-01-01 00:00:00 UTC


def make_engine() -> MarketSessionEngine:
    return MarketSessionEngine()


class TestSessionPhaseDeterminism:
    @given(st.integers(min_value=EPOCH_MIN, max_value=EPOCH_MAX))
    @settings(max_examples=200)
    def test_same_timestamp_same_phase(self, epoch_sec: int) -> None:
        """The same UTC timestamp always maps to the same session phase."""
        engine = make_engine()
        dt = datetime.fromtimestamp(epoch_sec, tz=timezone.utc)
        phase1 = engine.get_current_phase(dt)
        phase2 = engine.get_current_phase(dt)
        assert phase1 == phase2, (
            f"Non-deterministic phase for {dt}: {phase1} vs {phase2}"
        )

    @given(st.integers(min_value=EPOCH_MIN, max_value=EPOCH_MAX))
    @settings(max_examples=200)
    def test_phase_is_valid_enum_value(self, epoch_sec: int) -> None:
        """Every timestamp maps to a valid SessionPhase enum value."""
        engine = make_engine()
        dt = datetime.fromtimestamp(epoch_sec, tz=timezone.utc)
        phase = engine.get_current_phase(dt)
        assert isinstance(phase, SessionPhase), (
            f"Expected SessionPhase, got {type(phase)}"
        )
        valid_phases = set(SessionPhase)
        assert phase in valid_phases

    def test_all_six_phases_accessible(self) -> None:
        """All 6 session phases are reachable (sanity check for the enum)."""
        expected = {"PRE_OPEN", "PRE_OPEN_CALL_AUCTION", "REGULAR", "POST_MARKET", "CLOSED", "MUHURAT"}
        actual = {p.value for p in SessionPhase}
        assert expected == actual, f"Missing session phases: {expected - actual}"
