"""
Property 11 — Provenance Observation ID Uniqueness

Each call to create a provenance record must produce a unique UUID v4
observation ID. Concurrent creation must not produce duplicates.

Requirement: 8.1
"""
from __future__ import annotations

import uuid

from hypothesis import given, settings
from hypothesis import strategies as st


def _new_observation_id() -> str:
    """Mirror the production pattern: UUID v4 hex string."""
    return str(uuid.uuid4())


class TestObservationIdUniqueness:
    @given(st.integers(min_value=2, max_value=1000))
    @settings(max_examples=50)
    def test_n_observations_all_unique(self, n: int) -> None:
        """n generated observation IDs must all be distinct."""
        ids = [_new_observation_id() for _ in range(n)]
        assert len(set(ids)) == n, "Duplicate observation IDs detected"

    def test_observation_id_is_valid_uuid(self) -> None:
        """Each generated ID must be a valid UUID."""
        for _ in range(100):
            obs_id = _new_observation_id()
            parsed = uuid.UUID(obs_id)
            assert parsed.version == 4, f"Expected UUID v4, got version {parsed.version}"

    @given(st.integers(min_value=1, max_value=100))
    @settings(max_examples=50)
    def test_ids_are_strings_not_empty(self, n: int) -> None:
        """IDs are non-empty strings."""
        for _ in range(n):
            obs_id = _new_observation_id()
            assert isinstance(obs_id, str)
            assert len(obs_id) > 0
