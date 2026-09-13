"""
Unit tests for src/core/validators/dedup.py — pipeline step 6.

Covers:
- compute_dedup_hash: determinism (same inputs → same hash)
- compute_dedup_hash: different inputs → different hash
- compute_dedup_hash: output is exactly 32 hex characters
- compute_dedup_hash: output is lowercase hex
- DedupStore: first occurrence is not a duplicate
- DedupStore: second occurrence is detected as duplicate
- DedupStore: different hashes are not duplicates of each other
- DedupStore.clear() resets state
- mark_duplicate_in_record: sets isDuplicate correctly
- mark_duplicate_in_record: returns the same dict object (in-place mutation)

Requirements: 3.6, 17.4 — Property 4 (deduplication hash stability)
"""

from __future__ import annotations

import pytest

from src.core.validators.dedup import (
    DedupStore,
    compute_dedup_hash,
    mark_duplicate_in_record,
)


# ---------------------------------------------------------------------------
# compute_dedup_hash
# ---------------------------------------------------------------------------


class TestComputeDedupHash:

    def test_returns_32_char_hex_string(self):
        h = compute_dedup_hash("NSE:NIFTY:IDX", 1_705_300_000_000, "angel_one", 22150.50, 1_000_000)
        assert isinstance(h, str)
        assert len(h) == 32

    def test_output_is_lowercase_hex(self):
        h = compute_dedup_hash("NSE:NIFTY:IDX", 1_705_300_000_000, "angel_one", 22150.50, 1_000_000)
        assert h == h.lower()
        assert all(c in "0123456789abcdef" for c in h)

    def test_determinism_same_inputs_same_hash(self):
        """Property 4: same inputs always produce the same hash."""
        args = ("NSE:RELIANCE:EQ", 1_705_300_000_123, "upstox", 2800.75, 50_000)
        h1 = compute_dedup_hash(*args)
        h2 = compute_dedup_hash(*args)
        assert h1 == h2

    def test_determinism_called_many_times(self):
        """Hash is stable across 100 invocations."""
        args = ("NSE:BANKNIFTY:IDX", 1_705_300_000_000, "scrapling", 45000.0, 2_000_000)
        first = compute_dedup_hash(*args)
        for _ in range(99):
            assert compute_dedup_hash(*args) == first

    def test_different_instrument_id_produces_different_hash(self):
        h1 = compute_dedup_hash("NSE:NIFTY:IDX", 1000, "src", 100.0, 10)
        h2 = compute_dedup_hash("NSE:BANKNIFTY:IDX", 1000, "src", 100.0, 10)
        assert h1 != h2

    def test_different_event_time_ms_produces_different_hash(self):
        h1 = compute_dedup_hash("NSE:NIFTY:IDX", 1000, "src", 100.0, 10)
        h2 = compute_dedup_hash("NSE:NIFTY:IDX", 1001, "src", 100.0, 10)
        assert h1 != h2

    def test_different_source_produces_different_hash(self):
        h1 = compute_dedup_hash("NSE:NIFTY:IDX", 1000, "angel_one", 100.0, 10)
        h2 = compute_dedup_hash("NSE:NIFTY:IDX", 1000, "upstox", 100.0, 10)
        assert h1 != h2

    def test_different_ltp_produces_different_hash(self):
        h1 = compute_dedup_hash("NSE:NIFTY:IDX", 1000, "src", 100.0, 10)
        h2 = compute_dedup_hash("NSE:NIFTY:IDX", 1000, "src", 100.1, 10)
        assert h1 != h2

    def test_different_volume_produces_different_hash(self):
        h1 = compute_dedup_hash("NSE:NIFTY:IDX", 1000, "src", 100.0, 10)
        h2 = compute_dedup_hash("NSE:NIFTY:IDX", 1000, "src", 100.0, 11)
        assert h1 != h2

    def test_zero_values_produce_valid_hash(self):
        h = compute_dedup_hash("X", 0, "", 0.0, 0)
        assert len(h) == 32

    def test_large_values_produce_valid_hash(self):
        h = compute_dedup_hash(
            "CRYPTO:BTC-USDT:SPOT",
            9_999_999_999_999,
            "binance",
            99_999.99,
            1_000_000_000,
        )
        assert len(h) == 32

    def test_hash_prefix_of_sha256(self):
        """Verify the hash is the first 32 chars of the SHA-256 digest."""
        import hashlib
        instrument_id = "NSE:NIFTY:IDX"
        event_time_ms = 1_705_300_000_000
        source = "angel_one"
        ltp = 22150.50
        volume = 1_000_000
        ltp_str = f"{ltp:.10g}"
        raw = f"{instrument_id}:{event_time_ms}:{source}:{ltp_str}:{volume}"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
        assert compute_dedup_hash(instrument_id, event_time_ms, source, ltp, volume) == expected


# ---------------------------------------------------------------------------
# DedupStore
# ---------------------------------------------------------------------------


class TestDedupStore:

    @pytest.mark.asyncio
    async def test_first_occurrence_not_duplicate(self):
        store = DedupStore()
        h = "abcdef1234567890abcdef1234567890"
        is_dup, recorded = await store.check_and_record(h)
        assert is_dup is False
        assert recorded is True

    @pytest.mark.asyncio
    async def test_second_occurrence_is_duplicate(self):
        store = DedupStore()
        h = "abcdef1234567890abcdef1234567890"
        await store.check_and_record(h)
        is_dup, recorded = await store.check_and_record(h)
        assert is_dup is True
        assert recorded is False

    @pytest.mark.asyncio
    async def test_different_hashes_not_duplicates_of_each_other(self):
        store = DedupStore()
        h1 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
        h2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2"
        await store.check_and_record(h1)
        is_dup, _ = await store.check_and_record(h2)
        assert is_dup is False

    @pytest.mark.asyncio
    async def test_clear_resets_state(self):
        store = DedupStore()
        h = "12345678901234567890123456789012"
        await store.check_and_record(h)
        store.clear()
        is_dup, recorded = await store.check_and_record(h)
        assert is_dup is False
        assert recorded is True

    @pytest.mark.asyncio
    async def test_size_increases_with_new_hashes(self):
        store = DedupStore()
        hashes = [f"{i:032x}" for i in range(5)]
        for h in hashes:
            await store.check_and_record(h)
        assert store.size == 5

    @pytest.mark.asyncio
    async def test_size_unchanged_on_duplicate(self):
        store = DedupStore()
        h = "00000000000000000000000000000001"
        await store.check_and_record(h)
        await store.check_and_record(h)
        assert store.size == 1

    @pytest.mark.asyncio
    async def test_clear_sets_size_to_zero(self):
        store = DedupStore()
        h = "ffffffffffffffffffffffffffffffff"
        await store.check_and_record(h)
        store.clear()
        assert store.size == 0


# ---------------------------------------------------------------------------
# mark_duplicate_in_record
# ---------------------------------------------------------------------------


class TestMarkDuplicateInRecord:

    def test_marks_duplicate_true(self):
        rec = {"instrumentId": "NSE:NIFTY:IDX", "ltp": 22000.0}
        result = mark_duplicate_in_record(rec, True)
        assert result["isDuplicate"] is True

    def test_marks_duplicate_false(self):
        rec = {"instrumentId": "NSE:NIFTY:IDX", "ltp": 22000.0}
        result = mark_duplicate_in_record(rec, False)
        assert result["isDuplicate"] is False

    def test_returns_same_dict_object(self):
        """Mutation is in-place; the same dict reference is returned."""
        rec = {"instrumentId": "X"}
        result = mark_duplicate_in_record(rec, True)
        assert result is rec

    def test_overwrites_existing_is_duplicate_field(self):
        rec = {"isDuplicate": False}
        mark_duplicate_in_record(rec, True)
        assert rec["isDuplicate"] is True

    def test_other_fields_unchanged(self):
        rec = {"instrumentId": "NSE:NIFTY:IDX", "ltp": 22150.0, "volume": 100}
        mark_duplicate_in_record(rec, True)
        assert rec["instrumentId"] == "NSE:NIFTY:IDX"
        assert rec["ltp"] == 22150.0
        assert rec["volume"] == 100
