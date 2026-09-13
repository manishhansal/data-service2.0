"""
tests/unit/cache/test_l1_cache.py

Unit tests for src/cache/l1_cache.py.

Covers (Requirements 9.1, 9.8):
- Basic get / set / delete / clear / contains / size operations
- LRU eviction order when capacity is reached
- Statistics tracking: hits, misses, evictions
- Capacity boundary conditions (capacity=1, exact-capacity, over-capacity)
- Update of existing key (no eviction, MRU promotion)
- Async concurrent access safety
- CacheStats properties: total_requests, hit_rate
- create_l1_cache() factory function
"""

from __future__ import annotations

import asyncio

import pytest

from src.cache.l1_cache import CacheStats, L1Cache, create_l1_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cache(capacity: int = 10) -> L1Cache:
    """Create a small-capacity cache for testing."""
    return L1Cache(max_capacity=capacity)


# ---------------------------------------------------------------------------
# Construction and configuration
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_capacity(self):
        cache = L1Cache()
        assert cache.max_capacity == 10_000

    def test_custom_capacity(self):
        cache = L1Cache(max_capacity=42)
        assert cache.max_capacity == 42

    def test_capacity_one_is_valid(self):
        cache = L1Cache(max_capacity=1)
        assert cache.max_capacity == 1

    def test_zero_capacity_raises(self):
        with pytest.raises(ValueError, match="max_capacity"):
            L1Cache(max_capacity=0)

    def test_negative_capacity_raises(self):
        with pytest.raises(ValueError, match="max_capacity"):
            L1Cache(max_capacity=-5)

    async def test_initial_size_is_zero(self):
        cache = make_cache()
        assert await cache.size() == 0

    def test_initial_stats_are_zero(self):
        cache = make_cache()
        stats = cache.get_stats()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.evictions == 0


# ---------------------------------------------------------------------------
# Basic get / set
# ---------------------------------------------------------------------------


class TestGetSet:
    async def test_set_and_get_string_value(self):
        cache = make_cache()
        await cache.set("k", "hello")
        assert await cache.get("k") == "hello"

    async def test_get_missing_key_returns_none(self):
        cache = make_cache()
        result = await cache.get("nonexistent")
        assert result is None

    async def test_set_and_get_none_value(self):
        """None is a valid cache value — must be distinguishable from a miss."""
        cache = make_cache()
        await cache.set("k", None)
        # After set, key exists; get returns the stored None
        assert await cache.contains("k") is True
        assert await cache.get("k") is None

    async def test_set_and_get_dict_value(self):
        cache = make_cache()
        payload = {"price": 123.45, "oi": None}
        await cache.set("ohlcv:NIFTY:1m", payload)
        assert await cache.get("ohlcv:NIFTY:1m") == payload

    async def test_set_and_get_integer_value(self):
        cache = make_cache()
        await cache.set("count", 42)
        assert await cache.get("count") == 42

    async def test_update_existing_key(self):
        cache = make_cache()
        await cache.set("k", "old")
        await cache.set("k", "new")
        assert await cache.get("k") == "new"

    async def test_update_existing_key_does_not_change_size(self):
        cache = make_cache()
        await cache.set("k", "old")
        await cache.set("k", "new")
        assert await cache.size() == 1

    async def test_multiple_keys_stored_independently(self):
        cache = make_cache()
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.set("c", 3)
        assert await cache.get("a") == 1
        assert await cache.get("b") == 2
        assert await cache.get("c") == 3

    async def test_size_increments_on_new_keys(self):
        cache = make_cache()
        await cache.set("x", 1)
        assert await cache.size() == 1
        await cache.set("y", 2)
        assert await cache.size() == 2


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_existing_key_returns_true(self):
        cache = make_cache()
        await cache.set("k", "v")
        result = await cache.delete("k")
        assert result is True

    async def test_delete_existing_key_removes_it(self):
        cache = make_cache()
        await cache.set("k", "v")
        await cache.delete("k")
        assert await cache.get("k") is None

    async def test_delete_missing_key_returns_false(self):
        cache = make_cache()
        result = await cache.delete("ghost")
        assert result is False

    async def test_delete_decrements_size(self):
        cache = make_cache()
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.delete("a")
        assert await cache.size() == 1

    async def test_delete_then_set_same_key(self):
        cache = make_cache()
        await cache.set("k", "original")
        await cache.delete("k")
        await cache.set("k", "reinserted")
        assert await cache.get("k") == "reinserted"


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


class TestClear:
    async def test_clear_empties_cache(self):
        cache = make_cache()
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.clear()
        assert await cache.size() == 0

    async def test_clear_makes_keys_inaccessible(self):
        cache = make_cache()
        await cache.set("a", 1)
        await cache.clear()
        assert await cache.get("a") is None

    async def test_clear_preserves_stats(self):
        cache = make_cache()
        await cache.set("a", 1)
        await cache.get("a")        # hit
        await cache.get("missing")  # miss
        await cache.clear()
        stats = cache.get_stats()
        assert stats.hits == 1
        assert stats.misses == 1

    async def test_set_after_clear_works(self):
        cache = make_cache()
        await cache.set("k", "before")
        await cache.clear()
        await cache.set("k", "after")
        assert await cache.get("k") == "after"


# ---------------------------------------------------------------------------
# Contains
# ---------------------------------------------------------------------------


class TestContains:
    async def test_contains_present_key(self):
        cache = make_cache()
        await cache.set("k", "v")
        assert await cache.contains("k") is True

    async def test_contains_absent_key(self):
        cache = make_cache()
        assert await cache.contains("ghost") is False

    async def test_contains_after_delete(self):
        cache = make_cache()
        await cache.set("k", "v")
        await cache.delete("k")
        assert await cache.contains("k") is False

    async def test_contains_does_not_affect_hit_stats(self):
        cache = make_cache()
        await cache.set("k", "v")
        await cache.contains("k")
        stats = cache.get_stats()
        assert stats.hits == 0
        assert stats.misses == 0


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


class TestLRUEviction:
    async def test_evicts_lru_entry_when_at_capacity(self):
        """When at capacity, inserting a new key evicts the LRU key."""
        cache = make_cache(capacity=3)
        await cache.set("a", 1)   # LRU: a
        await cache.set("b", 2)   # LRU: a, then b
        await cache.set("c", 3)   # LRU: a, then b, then c
        await cache.set("d", 4)   # capacity exceeded — "a" is evicted
        assert await cache.get("a") is None  # evicted
        assert await cache.get("b") == 2
        assert await cache.get("c") == 3
        assert await cache.get("d") == 4

    async def test_get_promotes_to_mru(self):
        """Accessing a key moves it to MRU so it is not the next to be evicted."""
        cache = make_cache(capacity=3)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.set("c", 3)
        # Access "a" — now "b" is the LRU
        await cache.get("a")
        await cache.set("d", 4)   # "b" should be evicted (LRU), not "a"
        assert await cache.get("b") is None  # evicted
        assert await cache.get("a") == 1
        assert await cache.get("c") == 3
        assert await cache.get("d") == 4

    async def test_update_existing_key_promotes_to_mru(self):
        """Updating an existing key must promote it to MRU."""
        cache = make_cache(capacity=3)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.set("c", 3)
        # Update "a" — now "b" is LRU
        await cache.set("a", 99)
        await cache.set("d", 4)   # "b" evicted (LRU)
        assert await cache.get("b") is None
        assert await cache.get("a") == 99

    async def test_capacity_one_evicts_on_each_insert(self):
        """With capacity=1, every new distinct key evicts the previous entry."""
        cache = make_cache(capacity=1)
        await cache.set("first", 1)
        await cache.set("second", 2)
        assert await cache.get("first") is None
        assert await cache.get("second") == 2

    async def test_capacity_one_update_no_eviction(self):
        """Updating the only key in a capacity=1 cache does not increment evictions."""
        cache = make_cache(capacity=1)
        await cache.set("k", "old")
        await cache.set("k", "new")
        stats = cache.get_stats()
        assert stats.evictions == 0
        assert await cache.get("k") == "new"

    async def test_no_eviction_before_capacity_reached(self):
        cache = make_cache(capacity=5)
        for i in range(5):
            await cache.set(str(i), i)
        stats = cache.get_stats()
        assert stats.evictions == 0
        assert await cache.size() == 5

    async def test_eviction_count_matches_excess_inserts(self):
        """Inserting N entries beyond capacity should produce exactly N evictions."""
        capacity = 4
        cache = make_cache(capacity=capacity)
        extra = 3
        for i in range(capacity + extra):
            await cache.set(str(i), i)
        stats = cache.get_stats()
        assert stats.evictions == extra
        assert await cache.size() == capacity


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


class TestStats:
    async def test_hit_increments_hits(self):
        cache = make_cache()
        await cache.set("k", "v")
        await cache.get("k")
        assert cache.get_stats().hits == 1

    async def test_miss_increments_misses(self):
        cache = make_cache()
        await cache.get("ghost")
        assert cache.get_stats().misses == 1

    async def test_eviction_increments_evictions(self):
        cache = make_cache(capacity=1)
        await cache.set("a", 1)
        await cache.set("b", 2)  # evicts "a"
        assert cache.get_stats().evictions == 1

    async def test_multiple_operations_accumulate_stats(self):
        cache = make_cache(capacity=2)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.get("a")      # hit
        await cache.get("ghost")  # miss
        await cache.set("c", 3)   # evicts "b"
        stats = cache.get_stats()
        assert stats.hits == 1
        assert stats.misses == 1
        assert stats.evictions == 1

    def test_stats_snapshot_is_independent(self):
        """get_stats() returns a new object — mutations don't affect the cache."""
        cache = make_cache()
        stats = cache.get_stats()
        stats.hits = 999          # mutate the snapshot
        assert cache.get_stats().hits == 0  # cache stats unchanged

    async def test_total_requests_is_hits_plus_misses(self):
        cache = make_cache()
        await cache.set("k", "v")
        await cache.get("k")      # hit
        await cache.get("ghost")  # miss
        stats = cache.get_stats()
        assert stats.total_requests == 2
        assert stats.total_requests == stats.hits + stats.misses

    async def test_hit_rate_all_hits(self):
        cache = make_cache()
        await cache.set("k", "v")
        await cache.get("k")
        stats = cache.get_stats()
        assert stats.hit_rate == pytest.approx(1.0)

    async def test_hit_rate_all_misses(self):
        cache = make_cache()
        await cache.get("ghost")
        stats = cache.get_stats()
        assert stats.hit_rate == pytest.approx(0.0)

    async def test_hit_rate_mixed(self):
        cache = make_cache()
        await cache.set("k", "v")
        await cache.get("k")      # hit
        await cache.get("ghost")  # miss
        stats = cache.get_stats()
        assert stats.hit_rate == pytest.approx(0.5)

    def test_hit_rate_zero_requests_is_zero(self):
        stats = CacheStats()
        assert stats.hit_rate == 0.0

    def test_reset_stats(self):
        cache = make_cache()
        cache._stats.hits = 5
        cache._stats.misses = 3
        cache._stats.evictions = 2
        cache.reset_stats()
        stats = cache.get_stats()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.evictions == 0


# ---------------------------------------------------------------------------
# Concurrent async access
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    async def test_concurrent_sets_do_not_corrupt_state(self):
        """Many concurrent set() calls must not leave the cache in an invalid state."""
        cache = make_cache(capacity=50)
        async def write(key: str, value: int) -> None:
            await cache.set(key, value)

        tasks = [write(f"key:{i}", i) for i in range(100)]
        await asyncio.gather(*tasks)
        # Cache size must not exceed capacity after concurrent inserts
        final_size = await cache.size()
        assert 0 < final_size <= 50

    async def test_concurrent_gets_return_consistent_values(self):
        """Concurrent readers must not see corrupted values."""
        cache = make_cache(capacity=10)
        for i in range(5):
            await cache.set(f"k{i}", i * 10)

        async def read(key: str) -> object:
            return await cache.get(key)

        tasks = [read(f"k{i % 5}") for i in range(50)]
        results = await asyncio.gather(*tasks)
        # All results must be one of the expected values or None (evicted)
        valid = {i * 10 for i in range(5)} | {None}
        for r in results:
            assert r in valid

    async def test_concurrent_mixed_operations_no_exception(self):
        """Mixed concurrent get/set/delete/clear must not raise."""
        cache = make_cache(capacity=5)

        async def writer(i: int) -> None:
            await cache.set(f"k{i}", i)

        async def reader(i: int) -> None:
            await cache.get(f"k{i}")

        async def remover(i: int) -> None:
            await cache.delete(f"k{i}")

        tasks = (
            [writer(i) for i in range(20)]
            + [reader(i) for i in range(20)]
            + [remover(i) for i in range(10)]
        )
        # Must complete without exceptions
        await asyncio.gather(*tasks)

    async def test_concurrent_gets_and_sets_preserve_lru_invariant(self):
        """After concurrent writes that exceed capacity, size must equal capacity."""
        capacity = 10
        cache = make_cache(capacity=capacity)
        await asyncio.gather(*[cache.set(f"k{i}", i) for i in range(capacity * 3)])
        assert await cache.size() == capacity


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    async def test_empty_string_key(self):
        cache = make_cache()
        await cache.set("", "empty_key_value")
        assert await cache.get("") == "empty_key_value"

    async def test_long_key(self):
        cache = make_cache()
        long_key = "mds:quote:angel_one:NSE:NIFTY:_:_:_" * 10
        await cache.set(long_key, {"ltp": 22000.0})
        assert await cache.get(long_key) == {"ltp": 22000.0}

    async def test_false_value_is_stored(self):
        """False is a valid value — must not be treated as a miss."""
        cache = make_cache()
        await cache.set("flag", False)
        assert await cache.get("flag") is False
        assert await cache.contains("flag") is True

    async def test_zero_value_is_stored(self):
        """0 is a valid value — must not be treated as a miss."""
        cache = make_cache()
        await cache.set("counter", 0)
        assert await cache.get("counter") == 0

    async def test_exactly_at_capacity_no_eviction(self):
        capacity = 3
        cache = make_cache(capacity=capacity)
        for i in range(capacity):
            await cache.set(f"k{i}", i)
        assert await cache.size() == capacity
        assert cache.get_stats().evictions == 0

    async def test_one_over_capacity_one_eviction(self):
        capacity = 3
        cache = make_cache(capacity=capacity)
        for i in range(capacity + 1):
            await cache.set(f"k{i}", i)
        assert await cache.size() == capacity
        assert cache.get_stats().evictions == 1


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestFactory:
    def test_create_l1_cache_default_capacity(self):
        cache = create_l1_cache()
        assert cache.max_capacity == 10_000

    def test_create_l1_cache_custom_capacity(self):
        cache = create_l1_cache(max_capacity=500)
        assert cache.max_capacity == 500

    def test_create_l1_cache_returns_l1cache_instance(self):
        cache = create_l1_cache()
        assert isinstance(cache, L1Cache)

    async def test_cache_created_by_factory_is_functional(self):
        cache = create_l1_cache(max_capacity=5)
        await cache.set("key", "value")
        assert await cache.get("key") == "value"
