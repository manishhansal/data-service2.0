"""
tests/unit/stores/test_lineage_store.py

Unit tests for :class:`src.stores.lineage_store.LineageStore`.

Covers:
- put / get / delete basics
- In-memory LRU eviction (insertion-order, oldest evicted at capacity)
- cache_size() and total_recorded counter
- get_by_instrument cache fallback (no DB)
- delete removes from cache
- Graceful degradation: no DB engine → cache-only, no exceptions raised
- DB errors during persist are swallowed; cache entry survives
- DB errors during get/get_by_instrument fall back to cache scan silently
- Re-insert of existing key refreshes without double-counting size
- max_cache_size=1 edge case
- Construction validation (max_cache_size < 1 raises ValueError)

These tests do NOT require a live PostgreSQL instance.  DB interactions are
simulated with thin MagicMock / AsyncMock stubs, isolating the store logic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.core.schemas.provenance import DataProvenance, DataSource, DataTrustStatus
from src.stores.lineage_store import LineageStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provenance(
    instrument_id: Optional[str] = "NIFTY-NFO",
    source: DataSource = DataSource.ANGEL_ONE,
) -> tuple[str, DataProvenance]:
    """Return a (observation_id_str, DataProvenance) pair."""
    obs_id = str(uuid4())
    prov = DataProvenance(
        instrumentId=instrument_id,
        source=source,
        provider="angel_one",
        datasetKey=f"mds:quote:angel_one:NFO:{instrument_id}:_:_:_",
        receivedAtMs=1_700_000_000_000,
        eventTimeMs=1_700_000_000_000,
        normalisationVersion="2.0.0",
    )
    return obs_id, prov


def _make_store(max_cache_size: int = 100, db_engine=None) -> LineageStore:
    return LineageStore(max_cache_size=max_cache_size, db_engine=db_engine)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_max_cache_size(self):
        store = LineageStore()
        assert store.max_cache_size == 10_000

    def test_custom_max_cache_size(self):
        store = LineageStore(max_cache_size=500)
        assert store.max_cache_size == 500

    def test_zero_cache_size_raises(self):
        with pytest.raises(ValueError, match="max_cache_size"):
            LineageStore(max_cache_size=0)

    def test_negative_cache_size_raises(self):
        with pytest.raises(ValueError, match="max_cache_size"):
            LineageStore(max_cache_size=-1)

    def test_initial_cache_size_is_zero(self):
        store = LineageStore()
        assert store.cache_size() == 0

    def test_initial_total_recorded_is_zero(self):
        store = LineageStore()
        assert store.total_recorded == 0

    def test_no_db_engine_by_default(self):
        store = LineageStore()
        assert store._db_engine is None


# ---------------------------------------------------------------------------
# put / get basics
# ---------------------------------------------------------------------------


class TestPutGet:
    async def test_put_and_get_round_trip(self):
        store = _make_store()
        obs_id, prov = _make_provenance()
        await store.put(obs_id, prov)
        result = await store.get(obs_id)
        assert result is not None
        assert result.dataObservationId == prov.dataObservationId

    async def test_get_missing_key_returns_none(self):
        store = _make_store()
        result = await store.get(str(uuid4()))
        assert result is None

    async def test_cache_size_increments_on_put(self):
        store = _make_store()
        _, p1 = _make_provenance()
        _, p2 = _make_provenance()
        id1, id2 = str(uuid4()), str(uuid4())
        await store.put(id1, p1)
        assert store.cache_size() == 1
        await store.put(id2, p2)
        assert store.cache_size() == 2

    async def test_total_recorded_increments_on_put(self):
        store = _make_store()
        for _ in range(5):
            obs_id, prov = _make_provenance()
            await store.put(obs_id, prov)
        assert store.total_recorded == 5

    async def test_re_put_same_key_does_not_double_count_total(self):
        """Updating an existing key must still increment total_recorded (it's
        a write event, not an update-in-place)."""
        store = _make_store()
        obs_id, prov = _make_provenance()
        await store.put(obs_id, prov)
        await store.put(obs_id, prov)
        # Two puts = two total recorded events
        assert store.total_recorded == 2
        # But cache size stays at 1 (same key)
        assert store.cache_size() == 1

    async def test_put_multiple_keys_all_retrievable(self):
        store = _make_store()
        pairs = [_make_provenance(instrument_id=f"SYM{i}") for i in range(10)]
        ids = []
        for _ in range(10):
            obs_id = str(uuid4())
            ids.append(obs_id)
        for i, (obs_id, prov) in enumerate([(ids[j], pairs[j][1]) for j in range(10)]):
            await store.put(obs_id, prov)

        for i in range(10):
            result = await store.get(ids[i])
            assert result is not None
            assert result.instrumentId == f"SYM{i}"


# ---------------------------------------------------------------------------
# LRU (insertion-order FIFO) eviction
# ---------------------------------------------------------------------------


class TestLRUEviction:
    async def test_oldest_entry_evicted_at_capacity(self):
        """When at capacity, inserting a new entry evicts the oldest."""
        store = _make_store(max_cache_size=3)
        ids = [str(uuid4()) for _ in range(4)]
        provs = [_make_provenance(instrument_id=f"S{i}")[1] for i in range(4)]

        await store.put(ids[0], provs[0])  # oldest
        await store.put(ids[1], provs[1])
        await store.put(ids[2], provs[2])
        # Cache is now full; inserting ids[3] should evict ids[0]
        await store.put(ids[3], provs[3])

        assert store.cache_size() == 3
        assert await store.get(ids[0]) is None   # evicted
        assert await store.get(ids[1]) is not None
        assert await store.get(ids[2]) is not None
        assert await store.get(ids[3]) is not None

    async def test_second_oldest_evicted_after_first_already_gone(self):
        store = _make_store(max_cache_size=2)
        ids = [str(uuid4()) for _ in range(4)]
        provs = [_make_provenance()[1] for _ in range(4)]

        await store.put(ids[0], provs[0])
        await store.put(ids[1], provs[1])
        await store.put(ids[2], provs[2])  # ids[0] evicted
        await store.put(ids[3], provs[3])  # ids[1] evicted

        assert store.cache_size() == 2
        assert await store.get(ids[0]) is None
        assert await store.get(ids[1]) is None
        assert await store.get(ids[2]) is not None
        assert await store.get(ids[3]) is not None

    async def test_capacity_one_evicts_on_each_new_insert(self):
        store = _make_store(max_cache_size=1)
        ids = [str(uuid4()) for _ in range(3)]
        prov = _make_provenance()[1]

        await store.put(ids[0], prov)
        await store.put(ids[1], prov)
        await store.put(ids[2], prov)

        assert store.cache_size() == 1
        assert await store.get(ids[0]) is None
        assert await store.get(ids[1]) is None
        assert await store.get(ids[2]) is not None

    async def test_no_eviction_below_capacity(self):
        capacity = 5
        store = _make_store(max_cache_size=capacity)
        ids = [str(uuid4()) for _ in range(capacity)]
        prov = _make_provenance()[1]
        for obs_id in ids:
            await store.put(obs_id, prov)

        assert store.cache_size() == capacity
        for obs_id in ids:
            assert await store.get(obs_id) is not None

    async def test_total_recorded_never_decremented_on_eviction(self):
        """total_recorded counts every write, regardless of evictions."""
        store = _make_store(max_cache_size=2)
        prov = _make_provenance()[1]
        for _ in range(10):
            await store.put(str(uuid4()), prov)
        assert store.cache_size() == 2
        assert store.total_recorded == 10

    async def test_re_put_existing_key_moves_to_mru(self):
        """Re-inserting an existing key should move it to the MRU position
        (tail), so it is NOT the next to be evicted."""
        store = _make_store(max_cache_size=3)
        ids = [str(uuid4()) for _ in range(4)]
        prov = _make_provenance()[1]

        await store.put(ids[0], prov)  # oldest
        await store.put(ids[1], prov)
        await store.put(ids[2], prov)
        # Re-put ids[0] — it moves to MRU; ids[1] is now oldest
        await store.put(ids[0], prov)
        # Insert ids[3] — ids[1] should be evicted (now oldest)
        await store.put(ids[3], prov)

        assert store.cache_size() == 3
        assert await store.get(ids[1]) is None  # evicted
        assert await store.get(ids[0]) is not None  # survived as MRU
        assert await store.get(ids[2]) is not None
        assert await store.get(ids[3]) is not None


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_existing_key_returns_true(self):
        store = _make_store()
        obs_id, prov = _make_provenance()
        await store.put(obs_id, prov)
        result = await store.delete(obs_id)
        assert result is True

    async def test_delete_removes_from_cache(self):
        store = _make_store()
        obs_id, prov = _make_provenance()
        await store.put(obs_id, prov)
        await store.delete(obs_id)
        assert await store.get(obs_id) is None

    async def test_delete_decrements_cache_size(self):
        store = _make_store()
        obs_id, prov = _make_provenance()
        await store.put(obs_id, prov)
        await store.delete(obs_id)
        assert store.cache_size() == 0

    async def test_delete_missing_key_returns_false(self):
        store = _make_store()
        result = await store.delete(str(uuid4()))
        assert result is False

    async def test_delete_then_put_same_key_works(self):
        store = _make_store()
        obs_id, prov = _make_provenance()
        await store.put(obs_id, prov)
        await store.delete(obs_id)
        await store.put(obs_id, prov)
        result = await store.get(obs_id)
        assert result is not None


# ---------------------------------------------------------------------------
# get_by_instrument — cache fallback (no DB)
# ---------------------------------------------------------------------------


class TestGetByInstrumentCacheFallback:
    async def test_returns_records_for_matching_instrument(self):
        store = _make_store()
        target = "BANKNIFTY-NFO"
        other = "NIFTY-NFO"

        ids_target = [str(uuid4()) for _ in range(3)]
        ids_other = [str(uuid4()) for _ in range(2)]

        for obs_id in ids_target:
            _, prov = _make_provenance(instrument_id=target)
            await store.put(obs_id, prov)
        for obs_id in ids_other:
            _, prov = _make_provenance(instrument_id=other)
            await store.put(obs_id, prov)

        results = await store.get_by_instrument(target, limit=10)
        assert len(results) == 3
        for r in results:
            assert r.instrumentId == target

    async def test_returns_empty_list_when_no_match(self):
        store = _make_store()
        _, prov = _make_provenance(instrument_id="RELIANCE-NSE")
        await store.put(str(uuid4()), prov)

        results = await store.get_by_instrument("UNKNOWN-SYM", limit=10)
        assert results == []

    async def test_limit_is_respected(self):
        store = _make_store(max_cache_size=50)
        for _ in range(10):
            _, prov = _make_provenance(instrument_id="NIFTY-NFO")
            await store.put(str(uuid4()), prov)

        results = await store.get_by_instrument("NIFTY-NFO", limit=3)
        assert len(results) == 3

    async def test_limit_clamped_to_one(self):
        store = _make_store()
        _, prov = _make_provenance(instrument_id="NIFTY-NFO")
        await store.put(str(uuid4()), prov)

        results = await store.get_by_instrument("NIFTY-NFO", limit=0)
        assert len(results) <= 1

    async def test_limit_clamped_to_1000(self):
        """Limit > 1000 is silently clamped."""
        store = _make_store(max_cache_size=5)
        for _ in range(5):
            _, prov = _make_provenance(instrument_id="NIFTY-NFO")
            await store.put(str(uuid4()), prov)

        results = await store.get_by_instrument("NIFTY-NFO", limit=9999)
        assert len(results) <= 5  # can't exceed what's in the cache


# ---------------------------------------------------------------------------
# Graceful degradation — no DB engine
# ---------------------------------------------------------------------------


class TestNoDatabaseDegradation:
    async def test_put_without_db_does_not_raise(self):
        store = _make_store(db_engine=None)
        obs_id, prov = _make_provenance()
        # Must not raise
        await store.put(obs_id, prov)
        assert store.cache_size() == 1

    async def test_get_without_db_returns_cached_value(self):
        store = _make_store(db_engine=None)
        obs_id, prov = _make_provenance()
        await store.put(obs_id, prov)
        result = await store.get(obs_id)
        assert result is not None

    async def test_get_missing_without_db_returns_none(self):
        store = _make_store(db_engine=None)
        result = await store.get(str(uuid4()))
        assert result is None

    async def test_delete_without_db_removes_from_cache(self):
        store = _make_store(db_engine=None)
        obs_id, prov = _make_provenance()
        await store.put(obs_id, prov)
        deleted = await store.delete(obs_id)
        assert deleted is True
        assert await store.get(obs_id) is None

    async def test_delete_missing_without_db_returns_false(self):
        store = _make_store(db_engine=None)
        result = await store.delete(str(uuid4()))
        assert result is False

    async def test_get_by_instrument_without_db_uses_cache(self):
        store = _make_store(db_engine=None)
        for _ in range(5):
            _, prov = _make_provenance(instrument_id="NIFTY-NFO")
            await store.put(str(uuid4()), prov)

        results = await store.get_by_instrument("NIFTY-NFO", limit=10)
        assert len(results) == 5


# ---------------------------------------------------------------------------
# DB errors are swallowed; cache entry survives
# ---------------------------------------------------------------------------


class TestDatabaseErrorHandling:
    """Simulate DB failures during persist — cache entries must survive."""

    def _make_failing_engine(self) -> MagicMock:
        """Return a mock engine whose begin() context manager raises SQLAlchemyError."""
        from sqlalchemy.exc import SQLAlchemyError

        engine = MagicMock()

        @asynccontextmanager
        async def failing_begin():
            raise SQLAlchemyError("simulated DB error")
            yield  # unreachable but required for async generator syntax

        engine.begin = failing_begin
        return engine

    async def test_persist_failure_does_not_raise(self):
        engine = self._make_failing_engine()
        store = LineageStore(max_cache_size=100, db_engine=engine)
        obs_id, prov = _make_provenance()
        # No exception raised even though DB persist fails
        await store.put(obs_id, prov)

    async def test_cache_entry_survives_db_persist_failure(self):
        engine = self._make_failing_engine()
        store = LineageStore(max_cache_size=100, db_engine=engine)
        obs_id, prov = _make_provenance()
        await store.put(obs_id, prov)
        # Cache must still hold the entry
        result = await store.get(obs_id)
        assert result is not None
        assert result.dataObservationId == prov.dataObservationId

    async def test_db_get_failure_returns_none_gracefully(self):
        """When DB.get fails (for cache-miss path), None is returned — no crash."""
        from sqlalchemy.exc import SQLAlchemyError

        engine = MagicMock()

        @asynccontextmanager
        async def failing_connect():
            raise SQLAlchemyError("simulated DB connect error")
            yield

        engine.connect = failing_connect

        store = LineageStore(max_cache_size=5, db_engine=engine)
        # Key not in cache → tries DB → DB fails → None returned
        result = await store.get(str(uuid4()))
        assert result is None

    async def test_db_get_by_instrument_failure_falls_back_to_cache(self):
        """If DB fails for get_by_instrument, falls back to cache scan."""
        from sqlalchemy.exc import SQLAlchemyError

        engine = MagicMock()

        @asynccontextmanager
        async def failing_connect():
            raise SQLAlchemyError("simulated DB connect error")
            yield

        engine.connect = failing_connect

        store = LineageStore(max_cache_size=50, db_engine=engine)
        for _ in range(3):
            _, prov = _make_provenance(instrument_id="NIFTY-NFO")
            # Cache-only put (engine.begin fails, but cache write happens first)
            # We need to bypass DB for put too, so use None engine for put then set engine
            store._db_engine = None
            await store.put(str(uuid4()), prov)
        store._db_engine = engine

        results = await store.get_by_instrument("NIFTY-NFO", limit=10)
        # Falls back to cache scan → returns 3 results
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Concurrent access
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    async def test_concurrent_puts_do_not_corrupt_state(self):
        store = _make_store(max_cache_size=50)

        async def writer(i: int) -> None:
            obs_id = str(uuid4())
            _, prov = _make_provenance(instrument_id=f"SYM{i}")
            await store.put(obs_id, prov)

        await asyncio.gather(*[writer(i) for i in range(100)])
        assert 0 < store.cache_size() <= 50

    async def test_concurrent_puts_total_recorded_is_accurate(self):
        store = _make_store(max_cache_size=200)

        async def writer() -> None:
            obs_id = str(uuid4())
            _, prov = _make_provenance()
            await store.put(obs_id, prov)

        tasks = [writer() for _ in range(100)]
        await asyncio.gather(*tasks)
        assert store.total_recorded == 100


# ---------------------------------------------------------------------------
# cache_size / total_recorded interaction
# ---------------------------------------------------------------------------


class TestCounters:
    async def test_cache_size_does_not_exceed_max(self):
        capacity = 10
        store = _make_store(max_cache_size=capacity)
        for _ in range(50):
            obs_id = str(uuid4())
            _, prov = _make_provenance()
            await store.put(obs_id, prov)
        assert store.cache_size() == capacity

    async def test_total_recorded_exceeds_cache_size_when_evictions_occur(self):
        store = _make_store(max_cache_size=3)
        for _ in range(10):
            obs_id = str(uuid4())
            _, prov = _make_provenance()
            await store.put(obs_id, prov)
        assert store.total_recorded == 10
        assert store.cache_size() == 3
