"""
Property 4 — Deduplication Hash Stability

The dedup hash must be deterministic and stable: the same logical record always
produces the same hash regardless of call order or timing.

Requirement: 3.6, 17.4
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.core.validators.dedup import compute_dedup_hash

instrument_id_st = st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")))
source_st = st.sampled_from(["angel_one", "upstox", "scrapling", "yahoo", "jugaad", "openchart"])
ltp_st = st.floats(min_value=0.01, max_value=1_000_000.0, allow_nan=False, allow_infinity=False)
volume_st = st.integers(min_value=0, max_value=1_000_000_000)
timestamp_st = st.integers(min_value=1_000_000_000_000, max_value=2_000_000_000_000)  # ms


class TestDedupHashStability:
    @given(instrument_id_st, timestamp_st, source_st, ltp_st, volume_st)
    @settings(max_examples=100)
    def test_hash_is_deterministic(
        self,
        instrument_id: str,
        event_time_ms: int,
        source: str,
        ltp: float,
        volume: int,
    ) -> None:
        """Same inputs always produce the same hash."""
        h1 = compute_dedup_hash(instrument_id, event_time_ms, source, ltp, volume)
        h2 = compute_dedup_hash(instrument_id, event_time_ms, source, ltp, volume)
        assert h1 == h2

    @given(instrument_id_st, timestamp_st, source_st, ltp_st, volume_st)
    @settings(max_examples=100)
    def test_hash_is_hex_string(
        self,
        instrument_id: str,
        event_time_ms: int,
        source: str,
        ltp: float,
        volume: int,
    ) -> None:
        """Hash output is always a hex string."""
        h = compute_dedup_hash(instrument_id, event_time_ms, source, ltp, volume)
        assert isinstance(h, str)
        assert len(h) > 0
        int(h, 16)  # must be valid hex

    @given(
        instrument_id_st,
        timestamp_st,
        source_st,
        ltp_st,
        volume_st,
        # Second distinct LTP for collision check
        st.floats(min_value=0.01, max_value=1_000_000.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=100)
    def test_different_ltp_different_hash(
        self,
        instrument_id: str,
        event_time_ms: int,
        source: str,
        ltp1: float,
        volume: int,
        ltp2: float,
    ) -> None:
        """Different LTP values produce different hashes (collision resistance)."""
        if ltp1 == ltp2:
            return  # identical input — skip; not a collision
        # Also skip if the :.10g serialisation produces identical strings
        # (e.g. 0.01 vs 0.010000000000000002 — same price within float precision)
        if f"{ltp1:.10g}" == f"{ltp2:.10g}":
            return  # same serialised value — hash collision is expected and correct
        h1 = compute_dedup_hash(instrument_id, event_time_ms, source, ltp1, volume)
        h2 = compute_dedup_hash(instrument_id, event_time_ms, source, ltp2, volume)
        assert h1 != h2
