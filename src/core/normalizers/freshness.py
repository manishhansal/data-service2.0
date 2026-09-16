"""
src/core/normalizers/freshness.py

Freshness classifier for live market data in DATA-SERVICE 2.0.

Determines the freshness state of any data object based on:
  - source_timestamp  — when the exchange generated the data
  - received_at       — when the data service received it from the provider
  - normalized_at     — when normalization completed
  - current_time      — evaluation time (defaults to now)

Freshness states (Phase 15):
  LIVE            — data is within expected latency for market-open session
  LIVE_DELAYED    — data is slightly old but still within acceptable range
  STALE           — data is old; should not be used for order decisions
  NO_DATA         — no data has ever been received for this instrument
  PROVIDER_ERROR  — data was received but marked invalid by quality engine
  DATA_SERVICE_UNAVAILABLE — the service cannot determine data age

Market-open thresholds (configurable, sensible defaults):
  LIVE:         age <= 5 seconds
  LIVE_DELAYED: age <= 60 seconds
  STALE:        age > 60 seconds

Latency fields added to every data object:
  source_timestamp:    ISO-8601 UTC when exchange generated the data
  received_at:         ISO-8601 UTC when data-service received it
  normalized_at:       ISO-8601 UTC when normalization completed
  latency_ms:          received_at - source_timestamp (provider delivery latency)
  age_ms:              now - source_timestamp (total staleness)
  freshness_state:     one of FreshnessState values

Requirements: Phase 15
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Any, Optional

from src.observability.logging import get_logger

logger = get_logger(__name__)


class FreshnessState(str, Enum):
    """Freshness state of a live market data object."""

    LIVE                    = "LIVE"
    LIVE_DELAYED            = "LIVE_DELAYED"
    STALE                   = "STALE"
    NO_DATA                 = "NO_DATA"
    PROVIDER_ERROR          = "PROVIDER_ERROR"
    DATA_SERVICE_UNAVAILABLE = "DATA_SERVICE_UNAVAILABLE"


# Default thresholds (seconds)
_THRESHOLD_LIVE_SEC: float = 5.0
_THRESHOLD_DELAYED_SEC: float = 60.0

# Market hours: 09:15–15:30 IST = 03:45–10:00 UTC
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_MARKET_OPEN_IST  = datetime.time(9, 15)
_MARKET_CLOSE_IST = datetime.time(15, 30)

# Derivatives session closes at 15:30; equity auction until ~16:00
_CAS_CLOSE_IST = datetime.time(16, 0)


def _is_market_open(dt_utc: Optional[datetime.datetime] = None) -> bool:
    """Return True if NSE regular session is currently open."""
    if dt_utc is None:
        dt_utc = datetime.datetime.now(datetime.timezone.utc)
    dt_ist = dt_utc.astimezone(_IST)
    # Weekday check (Monday=0, Friday=4)
    if dt_ist.weekday() > 4:
        return False
    return _MARKET_OPEN_IST <= dt_ist.time() <= _MARKET_CLOSE_IST


def _parse_iso(ts_str: Optional[str]) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 string to an aware datetime."""
    if ts_str is None:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class FreshnessClassifier:
    """Classifies live data freshness and attaches latency metadata.

    Usage:
        classifier = FreshnessClassifier()
        enriched = classifier.classify(data_dict)

        # Or apply to many objects
        for tick in ticks:
            tick_enriched = classifier.classify(tick)
    """

    def __init__(
        self,
        live_threshold_sec: float = _THRESHOLD_LIVE_SEC,
        delayed_threshold_sec: float = _THRESHOLD_DELAYED_SEC,
    ) -> None:
        self._live_threshold_sec = live_threshold_sec
        self._delayed_threshold_sec = delayed_threshold_sec

    def classify(
        self,
        data: dict[str, Any],
        now_utc: Optional[datetime.datetime] = None,
    ) -> dict[str, Any]:
        """Compute freshness state and attach latency fields to data dict.

        Modifies the dict in-place and returns it.

        Args:
            data:    Data dict that may contain sourceTimestamp and/or receivedAt.
            now_utc: Evaluation time (defaults to now). Injected for testing.

        Returns:
            The same dict with freshness fields added.
        """
        if now_utc is None:
            now_utc = datetime.datetime.now(datetime.timezone.utc)

        normalized_at = now_utc.isoformat()

        # Parse timestamps
        source_ts = _parse_iso(
            data.get("sourceTimestamp") or data.get("source_timestamp") or
            data.get("exchangeTimestamp") or data.get("exchange_timestamp")
        )
        received_ts = _parse_iso(
            data.get("receivedAt") or data.get("received_at")
        )

        # Compute latency
        latency_ms: Optional[int] = None
        if source_ts is not None and received_ts is not None:
            delta = received_ts - source_ts
            latency_ms = max(0, int(delta.total_seconds() * 1000))

        # Compute age from source_timestamp (primary) or received_at (fallback)
        age_ms: Optional[int] = None
        reference_ts = source_ts or received_ts
        if reference_ts is not None:
            delta = now_utc - reference_ts
            age_ms = max(0, int(delta.total_seconds() * 1000))

        # Classify freshness
        freshness_state = self._classify_state(data, age_ms)

        data["normalizedAt"] = normalized_at
        data["latencyMs"] = latency_ms
        data["ageMs"] = age_ms
        data["freshnessState"] = freshness_state.value

        return data

    def _classify_state(
        self,
        data: dict[str, Any],
        age_ms: Optional[int],
    ) -> FreshnessState:
        """Determine freshness state from age and data quality flags."""
        # If quality is explicitly bad
        quality_status = data.get("qualityStatus") or data.get("quality_status")
        if quality_status in ("QUARANTINED", "BLOCKED", "POOR_QUALITY"):
            return FreshnessState.PROVIDER_ERROR

        if age_ms is None:
            # No timestamp at all — cannot assess age
            ltp = data.get("ltp")
            if ltp is None:
                return FreshnessState.NO_DATA
            return FreshnessState.DATA_SERVICE_UNAVAILABLE

        age_sec = age_ms / 1000.0

        if age_sec <= self._live_threshold_sec:
            return FreshnessState.LIVE
        elif age_sec <= self._delayed_threshold_sec:
            return FreshnessState.LIVE_DELAYED
        else:
            return FreshnessState.STALE

    def attach_freshness(
        self,
        data: dict[str, Any],
        source_timestamp: Optional[str],
        received_at: Optional[str],
        now_utc: Optional[datetime.datetime] = None,
    ) -> dict[str, Any]:
        """Attach freshness metadata to a data dict with explicit timestamps.

        Alternative to classify() when timestamps are passed separately
        rather than embedded in the data dict.

        Args:
            data:             Target data dict.
            source_timestamp: Exchange-side timestamp (ISO-8601 UTC string).
            received_at:      Data-service received timestamp (ISO-8601 UTC).
            now_utc:          Evaluation time (defaults to now).

        Returns:
            The same dict with freshness fields attached.
        """
        data["sourceTimestamp"] = source_timestamp
        data["receivedAt"] = received_at
        return self.classify(data, now_utc=now_utc)
