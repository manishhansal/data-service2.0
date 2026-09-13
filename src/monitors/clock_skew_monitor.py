"""
Clock-Skew Monitor for DATA-SERVICE 2.0.

Detects excessive time drift between the service's local system clock and
incoming market-data event timestamps (Requirement 18.8).

Design
------
Every tick or candle received from a provider carries an ``eventTimeMs``
field — the exchange-assigned event timestamp in UTC epoch milliseconds.
The monitor records a (local_time_ms, event_time_ms) pair each time
``record()`` is called and maintains a sliding window of the most recent
``window_size_sec`` seconds of samples.

``get_skew_stats()`` computes statistics over the current window:
    - ``medianSkewMs``  — median of |local - event| values (primary signal)
    - ``maxSkewMs``     — maximum |local - event| in the window
    - ``minSkewMs``     — minimum |local - event| in the window
    - ``isAcceptable``  — True when medianSkewMs ≤ threshold_ms (default 500)

``is_skew_acceptable()`` returns True when the median absolute skew is below
the given threshold (default 500 ms, per Requirement 18.8).

Thread-safety
-------------
The internal deque is appended and pruned under a ``threading.Lock`` so the
monitor can safely be shared across concurrent async tasks (which may run on
different threads in some event-loop configurations).

Usage
-----
    from src.monitors.clock_skew_monitor import ClockSkewMonitor

    monitor = ClockSkewMonitor(window_size_sec=60)

    # Call once per received tick/candle:
    monitor.record(event_time_ms=tick.eventTimeMs)

    stats = monitor.get_skew_stats()
    if not stats.isAcceptable:
        logger.warning("clock_skew_warning",
                       measuredSkewMs=stats.medianSkewMs)

Public API
----------
ClockSkewStats          — Pydantic v2 result model
ClockSkewMonitor        — main monitor class
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from typing import Deque, Tuple

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLD_MS: int = 500  # Requirement 18.8


class ClockSkewStats(BaseModel):
    """Snapshot of clock-skew statistics over the current sliding window.

    Attributes:
        sampleCount:   Number of samples in the current window.
        medianSkewMs:  Median absolute skew (|local_time - event_time|) in ms.
        maxSkewMs:     Maximum absolute skew observed in the window.
        minSkewMs:     Minimum absolute skew observed in the window.
        windowSizeMs:  Configured window size in milliseconds.
        isAcceptable:  True when medianSkewMs ≤ threshold_ms.
    """

    sampleCount: int = Field(..., ge=0, description="Number of samples in window")
    medianSkewMs: float = Field(
        ..., ge=0.0, description="Median absolute skew (ms)"
    )
    maxSkewMs: float = Field(
        ..., ge=0.0, description="Maximum absolute skew in window (ms)"
    )
    minSkewMs: float = Field(
        ..., ge=0.0, description="Minimum absolute skew in window (ms)"
    )
    windowSizeMs: int = Field(
        ..., gt=0, description="Configured sliding-window size (ms)"
    )
    isAcceptable: bool = Field(
        ..., description="True when medianSkewMs ≤ threshold_ms"
    )


# ---------------------------------------------------------------------------
# Monitor implementation
# ---------------------------------------------------------------------------

# Each sample is (local_time_ms: int, event_time_ms: int).
_Sample = Tuple[int, int]


class ClockSkewMonitor:
    """Tracks sliding-window clock-skew between local system time and
    provider event timestamps.

    Args:
        window_size_sec: Length of the sliding window in seconds.  Samples
                         older than this are discarded on the next ``record``
                         or ``get_skew_stats`` call.  Default: 60 seconds.
    """

    def __init__(self, window_size_sec: int = 60) -> None:
        if window_size_sec <= 0:
            raise ValueError(
                f"window_size_sec must be a positive integer, got {window_size_sec}"
            )
        self._window_size_sec: int = window_size_sec
        self._window_size_ms: int = window_size_sec * 1_000
        # Deque of (local_time_ms, event_time_ms) tuples, ordered by
        # insertion time.  We prune from the left when the oldest entry falls
        # outside the window.
        self._samples: Deque[_Sample] = deque()
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def record(self, event_time_ms: int) -> None:
        """Record a new (local_time, event_time) sample.

        Call this once for every tick or candle received from any provider.

        Args:
            event_time_ms: Exchange/provider event timestamp in UTC epoch
                           milliseconds.
        """
        local_time_ms = int(time.time() * 1_000)
        with self._lock:
            self._samples.append((local_time_ms, event_time_ms))
            self._prune_expired(local_time_ms)

    def get_skew_stats(
        self,
        threshold_ms: int = _DEFAULT_THRESHOLD_MS,
    ) -> ClockSkewStats:
        """Return clock-skew statistics for the current sliding window.

        Args:
            threshold_ms: Acceptable skew limit in milliseconds.  Used to
                          compute ``isAcceptable``.  Default: 500 ms.

        Returns:
            A :class:`ClockSkewStats` snapshot.  When the window is empty
            all numeric stats are ``0.0`` and ``isAcceptable`` is ``True``
            (no evidence of skew).
        """
        if threshold_ms <= 0:
            raise ValueError(
                f"threshold_ms must be a positive integer, got {threshold_ms}"
            )

        now_ms = int(time.time() * 1_000)
        with self._lock:
            self._prune_expired(now_ms)
            # Take a local copy so we don't hold the lock during computation.
            samples = list(self._samples)

        if not samples:
            return ClockSkewStats(
                sampleCount=0,
                medianSkewMs=0.0,
                maxSkewMs=0.0,
                minSkewMs=0.0,
                windowSizeMs=self._window_size_ms,
                isAcceptable=True,
            )

        absolute_skews = [abs(local - event) for local, event in samples]
        median_skew = statistics.median(absolute_skews)
        max_skew = max(absolute_skews)
        min_skew = min(absolute_skews)

        return ClockSkewStats(
            sampleCount=len(absolute_skews),
            medianSkewMs=float(median_skew),
            maxSkewMs=float(max_skew),
            minSkewMs=float(min_skew),
            windowSizeMs=self._window_size_ms,
            isAcceptable=median_skew <= threshold_ms,
        )

    def is_skew_acceptable(self, threshold_ms: int = _DEFAULT_THRESHOLD_MS) -> bool:
        """Return True when the median absolute skew is below *threshold_ms*.

        This is a convenience wrapper around ``get_skew_stats().isAcceptable``.
        An empty window (no samples yet) returns ``True`` — absence of
        evidence is not evidence of a problem.

        Args:
            threshold_ms: Acceptable skew limit in milliseconds.  Default: 500.

        Returns:
            ``True`` if the current median absolute skew ≤ threshold_ms,
            ``False`` otherwise.
        """
        return self.get_skew_stats(threshold_ms=threshold_ms).isAcceptable

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune_expired(self, now_ms: int) -> None:
        """Remove samples outside the sliding window.

        Must be called with ``self._lock`` held.

        Args:
            now_ms: Current local time in UTC epoch milliseconds.
        """
        cutoff_ms = now_ms - self._window_size_ms
        # Samples are ordered by insertion → prune from the left.
        while self._samples and self._samples[0][0] < cutoff_ms:
            self._samples.popleft()
