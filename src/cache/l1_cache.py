"""
L1 in-process LRU cache.

Implements an O(1) LRU cache backed by ``collections.OrderedDict`` with an
``asyncio.Lock`` for safe concurrent access in the async application.

Design constraints (Requirements 9.1, 9.8):
- Max capacity configurable via ``CACHE_L1_MAX_ENTRIES`` (default 10,000)
- On capacity reached: evict the least-recently-used entry before inserting
- Access latency target ≤ 1ms at p99 — achieved via O(1) OrderedDict ops
- Thread-safe in an async context via asyncio.Lock (no blocking I/O inside
  the lock, so the p99 target is not compromised by contention)
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class CacheStats:
    """Running statistics for an L1CacheInstance."""

    hits: int = field(default=0)
    misses: int = field(default=0)
    evictions: int = field(default=0)

    @property
    def total_requests(self) -> int:
        """Total get() calls (hits + misses)."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Fraction of get() calls that were cache hits; 0.0 when no requests."""
        if self.total_requests == 0:
            return 0.0
        return self.hits / self.total_requests


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class L1Cache:
    """Async-safe in-process LRU cache.

    Uses ``collections.OrderedDict`` for O(1) get/set/delete with LRU
    ordering.  An ``asyncio.Lock`` serialises mutations so concurrent
    coroutines cannot corrupt the internal state.

    Args:
        max_capacity: Maximum number of entries before LRU eviction kicks in.
            Must be ≥ 1.  Defaults to 10,000 (``CACHE_L1_MAX_ENTRIES``).

    Example::

        cache = L1Cache(max_capacity=100)
        await cache.set("key", "value")
        value = await cache.get("key")   # returns "value"
        stats = cache.get_stats()
    """

    def __init__(self, max_capacity: int = 10_000) -> None:
        if max_capacity < 1:
            raise ValueError(f"max_capacity must be >= 1, got {max_capacity}")
        self._max_capacity = max_capacity
        # OrderedDict preserves insertion/access order — last item is MRU,
        # first item is LRU.
        self._store: OrderedDict[str, Any] = OrderedDict()
        self._lock = asyncio.Lock()
        self._stats = CacheStats()

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Optional[Any]:
        """Return the cached value for *key*, or ``None`` on a miss.

        On a hit the entry is moved to the MRU position so the LRU ordering
        stays correct.
        """
        async with self._lock:
            if key not in self._store:
                self._stats.misses += 1
                return None
            # Move to end (MRU position)
            self._store.move_to_end(key)
            self._stats.hits += 1
            return self._store[key]

    async def set(self, key: str, value: Any) -> None:
        """Insert or update *key* with *value*.

        If *key* already exists, the value is updated and the entry is
        promoted to MRU.  If the cache is at capacity and *key* is new, the
        LRU entry is evicted first.
        """
        async with self._lock:
            if key in self._store:
                # Update existing entry and promote to MRU
                self._store.move_to_end(key)
                self._store[key] = value
            else:
                # Evict LRU when at capacity
                if len(self._store) >= self._max_capacity:
                    self._store.popitem(last=False)  # remove oldest (LRU)
                    self._stats.evictions += 1
                self._store[key] = value

    async def delete(self, key: str) -> bool:
        """Remove *key* from the cache.

        Returns:
            ``True`` if the key existed and was removed; ``False`` otherwise.
        """
        async with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    async def clear(self) -> None:
        """Remove all entries from the cache (stats are preserved)."""
        async with self._lock:
            self._store.clear()

    async def contains(self, key: str) -> bool:
        """Return ``True`` if *key* is present in the cache.

        Does NOT update LRU ordering (read-only membership check).
        """
        async with self._lock:
            return key in self._store

    async def size(self) -> int:
        """Return the current number of entries in the cache."""
        async with self._lock:
            return len(self._store)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> CacheStats:
        """Return a snapshot of the current cache statistics.

        Note: The returned object reflects the state at the time of the call;
        it is not a live view.
        """
        return CacheStats(
            hits=self._stats.hits,
            misses=self._stats.misses,
            evictions=self._stats.evictions,
        )

    def reset_stats(self) -> None:
        """Reset all statistics counters to zero (useful in tests)."""
        self._stats = CacheStats()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def max_capacity(self) -> int:
        """The configured maximum number of entries."""
        return self._max_capacity


# ---------------------------------------------------------------------------
# Module-level singleton factory
# ---------------------------------------------------------------------------


def create_l1_cache(max_capacity: int = 10_000) -> L1Cache:
    """Factory function that creates an ``L1Cache`` with the given capacity.

    Designed to be called once at application startup, typically reading
    ``settings.cache_l1_max_entries``::

        from src.core.settings import get_settings
        from src.cache.l1_cache import create_l1_cache

        l1 = create_l1_cache(get_settings().cache_l1_max_entries)
    """
    return L1Cache(max_capacity=max_capacity)
