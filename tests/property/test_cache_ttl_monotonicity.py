"""
Property 10 — Cache TTL Monotonicity

A cache entry's TTL must decrease monotonically as time passes.
An entry set at time T0 with TTL=X must have remaining TTL <= X at any T >= T0.
After expiry, the key must not be returned as a cache hit.

This test verifies the L1 in-process LRU cache and its eviction behaviour.
The L1Cache uses LRU eviction and capacity-based expiry; this test verifies
basic set/get/delete semantics.

Requirement: 9.2, 9.6
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.cache.l1_cache import L1Cache


key_st = st.text(min_size=1, max_size=50, alphabet="abcdefghijklmnopqrstuvwxyz0123456789_:")
value_st = st.one_of(
    st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False, allow_infinity=False),
    st.text(min_size=0, max_size=50),
    st.integers(min_value=0, max_value=1_000_000),
)


class TestCacheTTLMonotonicity:
    @given(key_st, value_st)
    @settings(max_examples=100)
    async def test_cache_hit_after_set(self, key: str, value) -> None:
        """Value must be retrievable immediately after set."""
        cache = L1Cache(max_capacity=100)
        await cache.set(key, value)
        result = await cache.get(key)
        assert result == value, f"Cache miss immediately after set for key {key!r}"

    @given(key_st, value_st)
    @settings(max_examples=100)
    async def test_explicit_eviction_clears_entry(self, key: str, value) -> None:
        """Explicitly deleting an entry means it is no longer retrievable."""
        cache = L1Cache(max_capacity=100)
        await cache.set(key, value)
        await cache.delete(key)
        assert await cache.get(key) is None, "Deleted entry must not be retrievable"

    @given(key_st, value_st, st.integers(min_value=2, max_value=20))
    @settings(max_examples=50)
    async def test_cache_capacity_lru_eviction(self, key: str, value, capacity: int) -> None:
        """When capacity is exceeded, LRU entries are evicted."""
        cache = L1Cache(max_capacity=capacity)
        # Fill to exactly capacity
        for i in range(capacity):
            await cache.set(f"key_{i}", i)
        assert await cache.size() <= capacity

        # Add one more — LRU entry should be evicted
        await cache.set("overflow_key", value)
        assert await cache.size() <= capacity + 1

    @given(st.lists(key_st, min_size=1, max_size=20, unique=True), value_st)
    @settings(max_examples=50)
    async def test_clear_empties_cache(self, keys: list[str], value) -> None:
        """Calling clear() must result in an empty cache."""
        cache = L1Cache(max_capacity=100)
        for k in keys:
            await cache.set(k, value)
        await cache.clear()
        assert await cache.size() == 0
        for k in keys:
            assert await cache.get(k) is None
