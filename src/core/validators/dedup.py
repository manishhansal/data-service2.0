"""
Duplicate detection — pipeline step 6.

Computes a deterministic deduplication hash from the five-tuple
``(instrumentId, eventTimeMs, source, ltp, volume)`` and maintains a
rolling 24-hour window to detect duplicate observations.

Key rules (Requirements 3.6, 17.4)
-------------------------------------
- Deduplication ID formula:  ``SHA-256(instrumentId:eventTimeMs:source:ltp:volume)[:32]``
  where ``ltp`` and ``volume`` are represented as their canonical string forms.
- Same inputs **always** produce the same hash (Property 4 in design doc).
- Duplicate observations are published to the Event Bus with
  ``isDuplicate: True`` rather than silently dropped, to preserve the audit
  trail.
- Duplicates are NOT persisted to L3.
- The 24-hour rolling window is implemented with an in-memory set in this
  module.  Redis-backed persistence is added in a later task.

Design choice for ``ltp`` serialisation
-----------------------------------------
``ltp`` is formatted as a fixed-precision decimal string (``f"{ltp:.10g}"``).
This ensures that floats that differ only in representation noise (e.g.
``1000.0`` vs ``1000.0000000001``) are NOT treated as duplicates of each
other — the serialisation is deterministic for values that are genuinely equal.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "compute_dedup_hash",
    "mark_duplicate_in_record",
    "DedupStore",
]

# ---------------------------------------------------------------------------
# Hash computation
# ---------------------------------------------------------------------------

_HASH_LEN: int = 32  # hex chars (128 bits of SHA-256)


def compute_dedup_hash(
    instrument_id: str,
    event_time_ms: int,
    source: str,
    ltp: float,
    volume: int,
) -> str:
    """Compute a deterministic 32-character hex deduplication hash.

    Formula: ``SHA-256("<instrumentId>:<eventTimeMs>:<source>:<ltp>:<volume>")[:32]``

    ``ltp`` is serialised with ``:.10g`` formatting so that the same logical
    price always yields the same string, regardless of minor float-arithmetic
    noise in the caller.

    Parameters
    ----------
    instrument_id: Canonical instrument identifier, e.g. ``"NSE:NIFTY:IDX"``.
    event_time_ms: Exchange event timestamp in UTC milliseconds.
    source:        Provider identifier, e.g. ``"angel_one"``.
    ltp:           Last traded price as a float.
    volume:        Traded volume as an integer.

    Returns
    -------
    32-character lowercase hex string.

    Property 4 guarantee
    --------------------
    This function is pure and side-effect-free.  Given identical arguments it
    always returns the identical string.
    """
    # Serialise ltp consistently: use a precision that preserves value identity
    # without introducing floating-point noise.
    ltp_str = f"{ltp:.10g}"
    raw = f"{instrument_id}:{event_time_ms}:{source}:{ltp_str}:{volume}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:_HASH_LEN]


# ---------------------------------------------------------------------------
# Record mutation helper
# ---------------------------------------------------------------------------


def mark_duplicate_in_record(record: dict, is_duplicate: bool) -> dict:
    """Set the ``isDuplicate`` flag on a record dict.

    Parameters
    ----------
    record:       The dataset dict to update (mutated in place).
    is_duplicate: Whether this record is a duplicate.

    Returns
    -------
    The (mutated) record.
    """
    record["isDuplicate"] = is_duplicate
    return record


# ---------------------------------------------------------------------------
# DedupStore — in-memory rolling-window store
# ---------------------------------------------------------------------------


@dataclass
class _DedupEntry:
    """Internal entry: hash + insertion monotonic time."""

    dedup_hash: str
    recorded_at: float  # time.monotonic() value


class DedupStore:
    """In-memory 24-hour rolling-window deduplication store.

    Thread safety
    -------------
    Uses an ``asyncio.Lock`` to guard the internal set/list structures so
    that concurrent async tasks do not corrupt state.  The class is designed
    for use within a single event loop.

    Redis integration
    -----------------
    This implementation stores data only in process memory.  A future task
    will replace / extend this with Redis-backed state so that state survives
    restarts and is shared across replicas.
    """

    def __init__(self) -> None:
        # Maps dedup_hash → recorded_at (monotonic seconds)
        self._seen: dict[str, float] = {}
        # Insertion-ordered list for efficient TTL eviction
        self._order: list[_DedupEntry] = []
        self._lock: asyncio.Lock = asyncio.Lock()

    async def check_and_record(
        self,
        dedup_hash: str,
        window_hours: int = 24,
    ) -> tuple[bool, bool]:
        """Check if a hash has been seen within the rolling window.

        Parameters
        ----------
        dedup_hash:   The 32-char hex hash to check.
        window_hours: Rolling window duration in hours (default 24).

        Returns
        -------
        ``(is_duplicate, recorded)``
        - ``is_duplicate``: ``True`` when the hash was seen within the window.
        - ``recorded``:     ``True`` when the hash was newly recorded.
        """
        window_sec = window_hours * 3600.0
        now = time.monotonic()

        async with self._lock:
            # Evict expired entries first.
            self._evict_expired(now, window_sec)

            if dedup_hash in self._seen:
                # Already recorded within the window — duplicate.
                return True, False

            # New hash — record it.
            self._seen[dedup_hash] = now
            self._order.append(_DedupEntry(dedup_hash=dedup_hash, recorded_at=now))
            return False, True

    def clear(self) -> None:
        """Remove all entries from the store.  Useful for test isolation."""
        self._seen.clear()
        self._order.clear()

    @property
    def size(self) -> int:
        """Current number of hashes in the store."""
        return len(self._seen)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _evict_expired(self, now: float, window_sec: float) -> None:
        """Remove entries older than ``window_sec`` from the store.

        Iterates from the oldest entry (front of ``_order``) and removes
        until all remaining entries are within the window.
        """
        cutoff = now - window_sec
        while self._order and self._order[0].recorded_at < cutoff:
            expired = self._order.pop(0)
            self._seen.pop(expired.dedup_hash, None)
