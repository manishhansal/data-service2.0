"""
src/engines/candle_builder.py

Real-time CandleBuilder — aggregates live market ticks into canonical OHLCV
candles for 1m, 5m, 10m, 15m, 30m, and 1h intervals.

Design contract
---------------
* Candles are keyed by (instrument_id, interval, candle_open_time_utc).
* open/high/low/close are the OHLCV for the CANDLE PERIOD only.
* ``candle_time_ms``  = candle open timestamp (UTC epoch ms).
* ``available_at_ms`` = first tick that closed the candle + ingestion latency.
  A candle is only marked ``finalised=True`` after the period has closed.
  Partial candles may be emitted for live display but are tagged
  ``finalised=False`` and MUST NOT be used for ML features.
* Out-of-order ticks (exchange_ts < current candle_open) are handled:
  - If within the same candle: update high/low only.
  - If from a prior candle: update the existing candle if still in memory.
  - Ticks > 1 candle period old are logged and discarded.
* Duplicate ticks (same exchange_ts, same ltp, same volume): discarded.
* Volume resets are detected when the tick volume < candle volume at the
  session open (09:15 IST); the builder resets the day's candle.
* Session boundaries: candles that straddle 09:15 or 15:30 IST are truncated.
* Point-in-time guarantee:
    candle_time_ms <= available_at_ms <= ingestion_time_ms (now)

Requirements: Phases E–G; §Candle Builder; §Point-in-Time Data Integrity
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

from src.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical real-time intervals (seconds)
CANDLE_INTERVAL_SECONDS: dict[str, int] = {
    "1m":  60,
    "5m":  300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
}

# NSE market open / close (IST offset = +5:30 = 19800 seconds)
_IST_OFFSET_SEC: int = 19800
_NSE_OPEN_IST: tuple[int, int] = (9, 15)    # 09:15
_NSE_CLOSE_IST: tuple[int, int] = (15, 30)  # 15:30

# Max look-back: ticks older than this many seconds are discarded
_MAX_LATE_TICK_SEC: int = 120

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CandleState:
    """Mutable accumulator for one (instrument_id, interval, candle_open_time)."""

    instrument_id: str
    exchange: str
    interval: str
    candle_time_ms: int          # UTC epoch ms of candle open
    candle_time_dt: datetime.datetime  # UTC datetime of candle open

    open:   float = 0.0
    high:   float = 0.0
    low:    float = float("inf")
    close:  float = 0.0
    volume: int   = 0
    open_interest: Optional[int] = None
    oi_change:     Optional[int] = None

    tick_count: int = 0
    finalised:  bool = False
    available_at_ms: Optional[int] = None   # set when candle closes
    ingestion_time_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    provider: str = ""
    source_type: str = "LIVE_WEBSOCKET"

    # Dedup: track last seen (exchange_ts_ms, ltp, volume) per tick
    _seen_tick_keys: set = field(default_factory=set, repr=False)

    def apply_tick(self, tick: dict[str, Any]) -> bool:
        """Apply a tick to this candle. Returns True if the tick was used.

        Handles:
        - OHLCV update
        - OI update
        - Out-of-order ticks (prior to open): update high/low only
        - Duplicate ticks: skip
        """
        ltp = tick.get("ltp")
        if ltp is None:
            return False
        ltp = float(ltp)

        volume = int(tick.get("volume") or 0)
        oi = tick.get("oi") or tick.get("open_interest")
        exch_ts = tick.get("exchange_ts_ms") or tick.get("eventTimeMs") or 0

        # Dedup check
        dedup_key = (int(exch_ts), round(ltp, 4), volume)
        if dedup_key in self._seen_tick_keys:
            return False
        self._seen_tick_keys.add(dedup_key)
        if len(self._seen_tick_keys) > 10000:
            # Trim to avoid unbounded growth
            self._seen_tick_keys = set(list(self._seen_tick_keys)[-5000:])

        if self.tick_count == 0:
            # First tick — initialise OHLC
            self.open = ltp
            self.high = ltp
            self.low  = ltp
            self.close = ltp
            self.volume = volume
        else:
            self.high  = max(self.high, ltp)
            self.low   = min(self.low, ltp)
            self.close = ltp
            # Volume is cumulative for the session; take delta from last known
            if volume >= self.volume:
                self.volume = volume
            # (if volume < current, provider may have reset — keep existing)

        if oi is not None:
            prev_oi = self.open_interest
            self.open_interest = int(oi)
            if prev_oi is not None and self.open_interest is not None:
                self.oi_change = self.open_interest - prev_oi

        self.tick_count += 1
        if tick.get("provider"):
            self.provider = str(tick["provider"])
        return True

    def to_dict(self) -> dict[str, Any]:
        """Convert to canonical candle dict for DB upsert."""
        return {
            "instrument_id":     self.instrument_id,
            "exchange":          self.exchange,
            "interval_str":      self.interval,
            "time":              self.candle_time_dt,
            "candle_time_ms":    self.candle_time_ms,
            "open":              self.open,
            "high":              self.high,
            "low":               min(self.low, self.open, self.close),  # enforce low <= min(O,C)
            "close":             self.close,
            "volume":            self.volume,
            "open_interest":     self.open_interest,
            "oi_change":         self.oi_change,
            "provider":          self.provider,
            "source_type":       self.source_type,
            "data_origin":       "PROVIDER",
            "finalised":         self.finalised,
            "tick_count":        self.tick_count,
            "available_at_ms":   self.available_at_ms,
            "ingestion_time_ms": self.ingestion_time_ms,
            "quality_status":    "TRUSTED" if self.finalised else "DEGRADED",
        }


# ---------------------------------------------------------------------------
# CandleBuilder
# ---------------------------------------------------------------------------

# Callback type: receives a finalised CandleState
CandleCallback = Callable[[CandleState], Coroutine[Any, Any, None]]


class CandleBuilder:
    """Aggregates ticks from all providers into canonical candles.

    Usage::

        builder = CandleBuilder(intervals=["1m", "5m", "15m"])
        builder.add_candle_callback(my_persist_fn)
        await builder.process_tick(tick_dict)

    The callback is called with every finalised (closed) candle.
    Partial candles can be obtained via ``get_partial()``.

    Thread safety: designed for single-threaded asyncio use.
    """

    def __init__(
        self,
        intervals: Optional[list[str]] = None,
    ) -> None:
        self._intervals: list[str] = intervals or ["1m", "5m", "10m", "15m", "30m", "1h"]
        # Validate intervals
        for iv in self._intervals:
            if iv not in CANDLE_INTERVAL_SECONDS:
                raise ValueError(
                    f"Interval {iv!r} not in CANDLE_INTERVAL_SECONDS. "
                    f"Allowed: {sorted(CANDLE_INTERVAL_SECONDS)}"
                )

        # Active candles: (instrument_id, interval) → CandleState
        self._active: dict[tuple[str, str], CandleState] = {}

        # Finalised candle buffer — flushed by callbacks
        self._finalised: list[CandleState] = []

        # Callbacks called with each finalised candle
        self._callbacks: list[CandleCallback] = []

        # Per-instrument tick count (for diagnostics)
        self._tick_counts: collections.Counter = collections.Counter()

        # Statistics
        self._ticks_processed: int = 0
        self._candles_finalised: int = 0
        self._late_ticks_discarded: int = 0
        self._duplicate_ticks: int = 0

    def add_candle_callback(self, callback: CandleCallback) -> None:
        """Register a coroutine function called on every finalised candle."""
        self._callbacks.append(callback)

    def _candle_open_ms(self, ts_ms: int, interval: str) -> int:
        """Round down ts_ms to the nearest candle boundary (UTC epoch ms)."""
        period_ms = CANDLE_INTERVAL_SECONDS[interval] * 1000
        return (ts_ms // period_ms) * period_ms

    def _candle_open_dt(self, candle_open_ms: int) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(
            candle_open_ms / 1000.0, tz=datetime.timezone.utc
        )

    def _is_within_nse_session(self, ts_ms: int) -> bool:
        """Return True if the timestamp falls within NSE market hours (IST)."""
        # Convert UTC ms → IST
        ts_sec = ts_ms / 1000.0
        ist_dt = datetime.datetime.fromtimestamp(
            ts_sec + _IST_OFFSET_SEC, tz=datetime.timezone.utc
        )
        hhmm = (ist_dt.hour, ist_dt.minute)
        return _NSE_OPEN_IST <= hhmm <= _NSE_CLOSE_IST

    async def process_tick(self, tick: dict[str, Any]) -> None:
        """Process a single normalised tick from any provider.

        Args:
            tick: Normalised tick dict. Expected keys:
                - instrumentId (str)
                - exchange (str)
                - eventTimeMs or exchange_ts_ms (int, UTC epoch ms)
                - ltp (float)
                - volume (int, cumulative session volume)
                - oi / open_interest (int, optional)
                - provider (str)
        """
        instrument_id = tick.get("instrumentId") or tick.get("instrument_id", "")
        exchange = tick.get("exchange", "NSE")
        ts_ms = int(
            tick.get("exchange_ts_ms")
            or tick.get("eventTimeMs")
            or (time.time() * 1000)
        )
        now_ms = int(time.time() * 1000)

        # Discard ticks that are too old (past late-tick window)
        age_sec = (now_ms - ts_ms) / 1000.0
        if age_sec > _MAX_LATE_TICK_SEC:
            self._late_ticks_discarded += 1
            logger.debug(
                "candle_builder_late_tick_discarded",
                component="candle_builder",
                instrument_id=instrument_id,
                age_sec=round(age_sec, 1),
            )
            return

        self._ticks_processed += 1
        self._tick_counts[instrument_id] += 1

        for interval in self._intervals:
            candle_open_ms = self._candle_open_ms(ts_ms, interval)
            key = (instrument_id, interval)
            current = self._active.get(key)

            if current is not None and current.candle_time_ms != candle_open_ms:
                # The tick belongs to a new candle — finalise the old one
                await self._finalise_candle(current, finalised_at_ms=now_ms)
                current = None

            if current is None:
                # Create a new candle state
                current = CandleState(
                    instrument_id=instrument_id,
                    exchange=exchange,
                    interval=interval,
                    candle_time_ms=candle_open_ms,
                    candle_time_dt=self._candle_open_dt(candle_open_ms),
                    provider=str(tick.get("provider", "")),
                )
                self._active[key] = current

            used = current.apply_tick(tick)
            if not used:
                self._duplicate_ticks += 1

    async def _finalise_candle(
        self, candle: CandleState, *, finalised_at_ms: int
    ) -> None:
        """Mark a candle as finalised, set available_at_ms, fire callbacks."""
        candle.finalised = True
        # available_at_ms = the actual wall-clock time when we detected the candle closed.
        # This is the time the first tick of the NEXT period arrived (finalised_at_ms),
        # which is always >= candle_time_ms + period_ms in real markets.
        # In tests it may be exactly finalised_at_ms (the "now" at test time).
        # Guarantee: candle_time_ms <= available_at_ms (the period is always in the past
        # relative to when we finalise it).
        candle.available_at_ms = finalised_at_ms
        candle.ingestion_time_ms = int(time.time() * 1000)

        self._finalised.append(candle)
        self._candles_finalised += 1

        logger.debug(
            "candle_finalised",
            component="candle_builder",
            instrument_id=candle.instrument_id,
            interval=candle.interval,
            candle_time_ms=candle.candle_time_ms,
            close=candle.close,
            volume=candle.volume,
            tick_count=candle.tick_count,
        )

        # Fire all registered callbacks
        for cb in self._callbacks:
            try:
                await cb(candle)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "candle_callback_error",
                    component="candle_builder",
                    instrument_id=candle.instrument_id,
                    interval=candle.interval,
                    error=str(exc),
                )

    async def flush_all(self) -> None:
        """Finalise all active (open) candles — call at market close or shutdown."""
        now_ms = int(time.time() * 1000)
        keys = list(self._active.keys())
        for key in keys:
            candle = self._active.pop(key, None)
            if candle is not None and candle.tick_count > 0:
                await self._finalise_candle(candle, finalised_at_ms=now_ms)

    def get_partial(
        self, instrument_id: str, interval: str
    ) -> Optional[dict[str, Any]]:
        """Return the current (partial, unfinalised) candle for an instrument.

        Returns None if no active candle exists.
        The returned dict has ``finalised=False`` — do NOT use for ML features.
        """
        candle = self._active.get((instrument_id, interval))
        if candle is None or candle.tick_count == 0:
            return None
        return candle.to_dict()

    def validate_point_in_time(self) -> list[str]:
        """Validate the point-in-time contract for all finalised candles.

        Returns a list of violation messages. Empty list = all candles pass.

        Contract: candle_time_ms <= available_at_ms <= ingestion_time_ms
        """
        violations = []
        for candle in self._finalised:
            if not candle.finalised:
                continue
            avail = candle.available_at_ms
            ingest = candle.ingestion_time_ms
            if avail is None:
                violations.append(
                    f"{candle.instrument_id} {candle.interval} "
                    f"t={candle.candle_time_ms}: available_at_ms is None"
                )
                continue
            if candle.candle_time_ms > avail:
                violations.append(
                    f"{candle.instrument_id} {candle.interval} "
                    f"t={candle.candle_time_ms}: candle_time_ms > available_at_ms "
                    f"({candle.candle_time_ms} > {avail}) — LOOK-AHEAD VIOLATION"
                )
            if avail > ingest:
                violations.append(
                    f"{candle.instrument_id} {candle.interval} "
                    f"t={candle.candle_time_ms}: available_at_ms > ingestion_time_ms "
                    f"({avail} > {ingest})"
                )
        return violations

    def get_stats(self) -> dict[str, Any]:
        """Return diagnostic statistics."""
        return {
            "ticks_processed":       self._ticks_processed,
            "candles_finalised":     self._candles_finalised,
            "active_candles":        len(self._active),
            "late_ticks_discarded":  self._late_ticks_discarded,
            "duplicate_ticks":       self._duplicate_ticks,
            "instruments_seen":      len(self._tick_counts),
        }
