"""
Gap detection — pipeline step 7.

Detects missing candles in a time-series by comparing the received ``time``
values against the expected monotonically-increasing sequence for the given
interval.

Key rules (Requirement 17.5)
------------------------------
- A gap occurs when consecutive candle timestamps are further apart than one
  interval step.
- Each gap emits a ``DataGapEvent`` with ``gapStartMs``, ``gapEndMs``,
  ``expectedCount``, ``actualCount``, and ``severity``.
- Severity classification:
    - ``LOW``    — 1 missing candle  (``expectedCount − actualCount == 1``)
    - ``MEDIUM`` — 2–5 missing candles
    - ``HIGH``   — > 5 missing candles

Design notes
------------
- The ``time`` field in each candle is the **candle open time** in UTC epoch
  *seconds* (consistent with the ``OHLCVCandle.time`` field in the design).
- The function also accepts epoch *milliseconds* — values ≥ 1e10 are treated
  as milliseconds and divided by 1000 before gap arithmetic.
- ``1M`` (calendar month) interval uses 30 days as an approximation
  (2 592 000 seconds).  Because calendar months vary in length, gap counts
  for ``1M`` are approximate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "INTERVAL_SECONDS",
    "DataGapEvent",
    "detect_candle_gaps",
    "classify_gap_severity",
]

# ---------------------------------------------------------------------------
# Canonical interval → seconds mapping
# ---------------------------------------------------------------------------

INTERVAL_SECONDS: dict[str, int] = {
    "1m":  60,
    "5m":  300,
    "10m": 600,
    "15m": 900,
    "30m": 1_800,
    "1h":  3_600,
    "1d":  86_400,
    "1w":  604_800,
    "1M":  2_592_000,  # 30-day approximation
}

# Threshold: candle times with this value or greater are treated as epoch ms.
_EPOCH_MS_THRESHOLD: float = 1e10


# ---------------------------------------------------------------------------
# DataGapEvent
# ---------------------------------------------------------------------------


@dataclass
class DataGapEvent:
    """Describes a detected gap in a candle sequence.

    Attributes
    ----------
    gapStartMs:    UTC epoch **milliseconds** of the first *missing* candle.
    gapEndMs:      UTC epoch **milliseconds** of the last *missing* candle.
    expectedCount: How many candles should have appeared in this range.
    actualCount:   How many candles were actually present (always 0 for a
                   contiguous gap; the field is kept for future partial-gap
                   support).
    severity:      ``"LOW"`` / ``"MEDIUM"`` / ``"HIGH"`` per spec rules.
    intervalStr:   The interval string this gap was detected for.
    """

    gapStartMs: int
    gapEndMs: int
    expectedCount: int
    actualCount: int
    severity: str
    intervalStr: str


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def classify_gap_severity(missing_count: int) -> str:
    """Return the gap severity string for a given missing-candle count.

    Rules (Requirement 17.5)
    -------------------------
    - ``missing_count == 1``  → ``"LOW"``
    - ``2 ≤ missing_count ≤ 5`` → ``"MEDIUM"``
    - ``missing_count > 5`` → ``"HIGH"``

    Parameters
    ----------
    missing_count: Number of candles missing in the gap (must be ≥ 1).

    Returns
    -------
    ``"LOW"``, ``"MEDIUM"``, or ``"HIGH"``.

    Raises
    ------
    ValueError: If ``missing_count`` is less than 1.
    """
    if missing_count < 1:
        raise ValueError(
            f"missing_count must be ≥ 1, got {missing_count}"
        )
    if missing_count == 1:
        return "LOW"
    if missing_count <= 5:
        return "MEDIUM"
    return "HIGH"


def detect_candle_gaps(
    candles: list[dict],
    interval: str,
) -> list[DataGapEvent]:
    """Detect gaps in a candle sequence for a given interval.

    The function scans consecutive pairs of candles and checks whether the
    time difference equals exactly one interval step.  When the gap between
    two consecutive candles is larger than expected, the missing candles are
    counted and a ``DataGapEvent`` is emitted.

    Parameters
    ----------
    candles:
        List of candle dicts, each containing a ``"time"`` field.  Candles
        must be ordered by ascending time.  The ``"time"`` value is either:
        - epoch *seconds* (``int`` or ``float`` < 1e10), or
        - epoch *milliseconds* (``int`` or ``float`` ≥ 1e10).
    interval:
        One of the canonical Indian-market intervals defined in
        ``INTERVAL_SECONDS``.  Note: ``"3m"`` is not in the mapping and
        will raise ``ValueError`` if passed.

    Returns
    -------
    List of ``DataGapEvent`` objects, one per detected gap.  Returns an empty
    list when the sequence is complete or when fewer than two candles are
    provided.

    Raises
    ------
    ValueError:
        - If ``interval`` is not in ``INTERVAL_SECONDS`` (e.g. ``"3m"``).
        - If a candle dict does not contain a ``"time"`` field.
    """
    if interval not in INTERVAL_SECONDS:
        raise ValueError(
            f"Unsupported interval {interval!r}. "
            f"Supported: {sorted(INTERVAL_SECONDS)}"
        )

    if len(candles) < 2:
        return []

    interval_sec = INTERVAL_SECONDS[interval]
    gap_events: list[DataGapEvent] = []

    # Normalise all times to epoch *seconds* for arithmetic.
    times_sec: list[float] = []
    for idx, candle in enumerate(candles):
        if "time" not in candle:
            raise ValueError(
                f"Candle at index {idx} is missing the 'time' field: {candle!r}"
            )
        t = candle["time"]
        if t >= _EPOCH_MS_THRESHOLD:
            t = t / 1000.0
        times_sec.append(float(t))

    for i in range(len(times_sec) - 1):
        t_current = times_sec[i]
        t_next = times_sec[i + 1]
        diff = t_next - t_current

        # Allow a small tolerance (1 second) for sub-second timestamp noise.
        if diff <= interval_sec + 1:
            continue  # No gap — consecutive candles.

        # Number of missing candles between t_current and t_next.
        # Expected next candle time is t_current + interval_sec.
        # Each missing candle occupies one interval_sec slot.
        missing: int = _count_missing(diff, interval_sec)
        if missing < 1:
            continue

        severity = classify_gap_severity(missing)

        # Gap start: the timestamp of the first *missing* candle.
        gap_start_sec = t_current + interval_sec
        # Gap end: the timestamp of the last *missing* candle.
        gap_end_sec = t_current + missing * interval_sec

        gap_events.append(
            DataGapEvent(
                gapStartMs=int(gap_start_sec * 1000),
                gapEndMs=int(gap_end_sec * 1000),
                expectedCount=missing,
                actualCount=0,
                severity=severity,
                intervalStr=interval,
            )
        )

    return gap_events


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _count_missing(diff_sec: float, interval_sec: int) -> int:
    """Calculate the number of missing candles given a time difference.

    For a time gap of ``diff_sec`` seconds and an interval of
    ``interval_sec``, the expected number of candles between two present
    candles (exclusive of both endpoints) is::

        floor(diff_sec / interval_sec) - 1

    Examples
    --------
    diff=120s, interval=60s → floor(2.0) - 1 = 1 missing candle
    diff=300s, interval=60s → floor(5.0) - 1 = 4 missing candles
    diff= 60s, interval=60s → floor(1.0) - 1 = 0 missing (no gap)
    """
    import math
    # Use round to handle floating-point edge cases near exact multiples.
    ratio = diff_sec / interval_sec
    slots = int(math.floor(ratio + 1e-9))
    return max(0, slots - 1)
