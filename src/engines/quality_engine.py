"""
src/engines/quality_engine.py

Quality Engine — computes DataConfidenceScore and DataQualityGate for each
market data dataset.

This module implements:
  - task 9.1: DataConfidenceScore formula
  - task 9.2: DataQualityGate with five conditions + GateResult + evaluate_gate

Score formula (non-negotiable, Requirements 7.1):
    score = freshness_score     (0–35, weight 35%)
          + completeness_score  (0–25, weight 25%)
          + provider_score      (0–20, weight 20%)
          + timestamp_score     (0–10, weight 10%)
          + agreement_score     (0–10, weight 10%)

Hard invariants:
    - Score is always capped at 95 — never 100 (inherent market-data uncertainty)
    - Score is always ≥ 0 — never negative
    - DataConfidenceScore < 30 → grade BLOCKED → signalEngineAllowed=False (no exceptions)
    - Broken sequence integrity triggers a 20% penalty: score = int(score * 0.8)

DataQualityGate five conditions (Requirements 7.2, 7.9):
    1. dataFresh              — data timestamp is within freshness threshold
    2. dataComplete           — no required fields are null/missing
    3. dataTimestampValid     — OHLCV consistency + timestamp in valid range
    4. dataProviderHealthy    — at least one source is marked available
    5. dataSemanticallyValid  — DataConfidenceScore >= minimum threshold

signalEngineAllowed = True iff ALL five conditions are True.
When signalEngineAllowed is False, blockReasons lists which conditions failed.

Freshness score mapping (for compute_confidence_from_inputs):
    FRESH   → 35
    AGING   → 25
    STALE   → 10
    EXPIRED →  0
    UNKNOWN →  5

Requirements: 7.1, 7.2, 7.9, 7.11
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Final, NamedTuple, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Required OHLCV fields that must be present and non-null for completeness.
# These are the fields checked by the DataQualityGate's completeness condition.
_REQUIRED_OHLCV_FIELDS: Final[tuple[str, ...]] = (
    "symbol",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

# Default minimum confidence score for the gate (requirement 7.2)
_DEFAULT_MIN_CONFIDENCE_SCORE: Final[float] = 60.0

# Maximum age of a timestamp (ms) before it is considered out of range.
# 30 days in milliseconds — any timestamp older than this or in the far future
# (more than 1 minute ahead) is flagged as invalid.
_TIMESTAMP_MAX_AGE_MS: Final[int] = 30 * 24 * 60 * 60 * 1000   # 30 days
_TIMESTAMP_FUTURE_TOLERANCE_MS: Final[int] = 60_000              # 1 minute ahead

# Score ceiling — never reaches 100 to reflect inherent market-data uncertainty
_SCORE_MAX: Final[int] = 95

# Grade thresholds (inclusive lower bound)
_GRADE_BLOCKED_THRESHOLD: Final[int] = 30   # score < 30 → BLOCKED
_GRADE_LOW_THRESHOLD: Final[int] = 50       # 30 ≤ score < 50 → LOW
_GRADE_MEDIUM_THRESHOLD: Final[int] = 80    # 50 ≤ score < 80 → MEDIUM
# score ≥ 80 → HIGH

# Sequence-integrity penalty factor
_SEQUENCE_PENALTY_FACTOR: Final[float] = 0.8

# Freshness classification → score mapping
_FRESHNESS_SCORE: Final[dict[str, int]] = {
    "FRESH": 35,
    "AGING": 25,
    "STALE": 10,
    "EXPIRED": 0,
    "UNKNOWN": 5,
}

# Maximum provider health score component
_PROVIDER_HEALTH_SCORE_MAX: Final[int] = 20

# Maximum individual component ranges (for documentation / validation)
_FRESHNESS_MAX: Final[int] = 35
_COMPLETENESS_MAX: Final[int] = 25
_TIMESTAMP_SCORE_TRUE: Final[int] = 10
_TIMESTAMP_SCORE_FALSE: Final[int] = 0
_AGREEMENT_SCORE_MAX: Final[int] = 10


# ---------------------------------------------------------------------------
# FreshnessStatus enum
# ---------------------------------------------------------------------------


class FreshnessStatus(str, Enum):
    """Freshness classification for a market data observation.

    FRESH   — data is within the acceptable freshness window for its tier/interval.
    AGING   — data is past the FRESH window but within an intermediate zone
              (used by live-quote scoring to produce a mid-range score of 25).
              Not produced by the interval-based candle classifier.
    STALE   — data is past the FRESH/AGING window but has not yet expired.
              Still usable with reduced confidence.
    EXPIRED — data has exceeded the maximum staleness threshold.  The
              ``DataConfidenceScore`` freshness component is 0 for expired data.
    UNKNOWN — age cannot be determined (e.g. timestamp is None).
    """

    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Internal threshold containers
# ---------------------------------------------------------------------------


class _LiveTierThresholds(NamedTuple):
    """Freshness thresholds in seconds for a single instrument tier.

    fresh_sec  — age ≤ this value → FRESH
    stale_sec  — fresh_sec < age ≤ this value → STALE (no AGING band for live quotes)
    """

    fresh_sec: float
    stale_sec: float


class _IntervalThresholds(NamedTuple):
    """Freshness thresholds in seconds for a candle interval.

    fresh_sec  — age ≤ this value → FRESH
    stale_sec  — fresh_sec < age ≤ this value → STALE
    expired_sec — age > this value → EXPIRED
    """

    fresh_sec: float
    stale_sec: float
    expired_sec: float


# ---------------------------------------------------------------------------
# Live-quote freshness constants (Requirements 7.3 and 7.4)
# ---------------------------------------------------------------------------

# REGULAR session thresholds (seconds)
_LIVE_THRESHOLDS_REGULAR: Final[dict[str, _LiveTierThresholds]] = {
    "INDEX":     _LiveTierThresholds(fresh_sec=10.0, stale_sec=60.0),
    "FO_LIQUID": _LiveTierThresholds(fresh_sec=10.0, stale_sec=60.0),
    "FO_NORMAL": _LiveTierThresholds(fresh_sec=15.0, stale_sec=90.0),
    "EQUITY":    _LiveTierThresholds(fresh_sec=30.0, stale_sec=120.0),
}

# Non-REGULAR session thresholds (seconds)
_LIVE_THRESHOLDS_NON_REGULAR: Final[dict[str, _LiveTierThresholds]] = {
    "INDEX":     _LiveTierThresholds(fresh_sec=60.0,  stale_sec=300.0),
    "FO_LIQUID": _LiveTierThresholds(fresh_sec=60.0,  stale_sec=300.0),
    "FO_NORMAL": _LiveTierThresholds(fresh_sec=90.0,  stale_sec=450.0),
    "EQUITY":    _LiveTierThresholds(fresh_sec=120.0, stale_sec=600.0),
}

# ---------------------------------------------------------------------------
# Interval-based candle freshness constants (Task 9.3)
# ---------------------------------------------------------------------------
# India baseline thresholds (seconds).
# The 3m interval is permanently banned for Indian market data.
_INDIA_INTERVAL_THRESHOLDS: Final[dict[str, _IntervalThresholds]] = {
    "1m":  _IntervalThresholds(fresh_sec=120.0,   stale_sec=600.0,    expired_sec=600.0),
    "5m":  _IntervalThresholds(fresh_sec=420.0,   stale_sec=1800.0,   expired_sec=1800.0),
    "10m": _IntervalThresholds(fresh_sec=600.0,   stale_sec=3000.0,   expired_sec=3000.0),
    "15m": _IntervalThresholds(fresh_sec=1200.0,  stale_sec=3600.0,   expired_sec=3600.0),
    "30m": _IntervalThresholds(fresh_sec=2400.0,  stale_sec=5400.0,   expired_sec=5400.0),
    "1h":  _IntervalThresholds(fresh_sec=4200.0,  stale_sec=10800.0,  expired_sec=10800.0),
    "1d":  _IntervalThresholds(fresh_sec=93600.0, stale_sec=172800.0, expired_sec=172800.0),
    "1w":  _IntervalThresholds(fresh_sec=604800.0, stale_sec=1209600.0, expired_sec=1209600.0),
    "1M":  _IntervalThresholds(fresh_sec=2592000.0, stale_sec=5184000.0, expired_sec=5184000.0),
}

# Crypto multiplier: 1.5× wider windows because crypto trades 24/7.
_CRYPTO_MULTIPLIER: Final[float] = 1.5

# Crypto additionally supports these intervals that India does not.
# The 3m crypto exception is explicitly documented here.
_CRYPTO_EXTRA_INTERVALS: Final[dict[str, _IntervalThresholds]] = {
    "3m":  _IntervalThresholds(fresh_sec=180.0, stale_sec=900.0,  expired_sec=900.0),
    "2h":  _IntervalThresholds(fresh_sec=7200.0, stale_sec=21600.0, expired_sec=21600.0),
    "4h":  _IntervalThresholds(fresh_sec=14400.0, stale_sec=43200.0, expired_sec=43200.0),
    "6h":  _IntervalThresholds(fresh_sec=21600.0, stale_sec=64800.0, expired_sec=64800.0),
    "8h":  _IntervalThresholds(fresh_sec=28800.0, stale_sec=86400.0, expired_sec=86400.0),
    "12h": _IntervalThresholds(fresh_sec=43200.0, stale_sec=129600.0, expired_sec=129600.0),
}


def _build_crypto_thresholds() -> dict[str, _IntervalThresholds]:
    """Build the crypto interval threshold map from the India baseline × 1.5."""
    result: dict[str, _IntervalThresholds] = {}
    for interval, t in _INDIA_INTERVAL_THRESHOLDS.items():
        result[interval] = _IntervalThresholds(
            fresh_sec=t.fresh_sec * _CRYPTO_MULTIPLIER,
            stale_sec=t.stale_sec * _CRYPTO_MULTIPLIER,
            expired_sec=t.expired_sec * _CRYPTO_MULTIPLIER,
        )
    result.update(_CRYPTO_EXTRA_INTERVALS)
    return result


_CRYPTO_INTERVAL_THRESHOLDS: Final[dict[str, _IntervalThresholds]] = _build_crypto_thresholds()


# ---------------------------------------------------------------------------
# FreshnessClassifier
# ---------------------------------------------------------------------------


class FreshnessClassifier:
    """Classifies the freshness of a market data observation.

    This class covers two complementary freshness models:

    **1. Instrument-tier freshness (live quotes)**
    Used by the DataQualityGate for real-time streaming data.
    Thresholds depend on the instrument tier (INDEX / FO_LIQUID / FO_NORMAL / EQUITY)
    and the current market session (REGULAR vs. non-REGULAR).
    Results: FRESH, STALE, EXPIRED, or UNKNOWN (no AGING band).

    Call: ``classify_live(event_time_ms, instrument_tier, is_regular_session)``

    **2. Interval-based freshness (candle / historical data)**
    Used to assess whether a candle dataset is still fresh enough to serve.
    Thresholds depend on candle interval and market vertical ("india" or "crypto").
    Results: FRESH, STALE, EXPIRED, or UNKNOWN.

    Call: ``classify(timestamp, market, interval)``

    Both models also expose ``age_seconds(timestamp)`` to compute the age of any
    UTC-aware (or naive UTC) datetime from now.

    Constraints:
    - ``interval="3m"`` with ``market="india"`` raises ``ValueError`` (permanently banned).
    - Timestamps with ``tzinfo=None`` are assumed to be UTC (no IST interpretation).
    - All ``FreshnessClassifier`` instances are stateless and thread-safe.

    Requirements: 7.3, 7.4 (live-quote model); Task 9.3 (interval-based model).
    """

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    @staticmethod
    def age_seconds(timestamp: datetime) -> float:
        """Return the age of *timestamp* in seconds relative to UTC now.

        Args:
            timestamp: A ``datetime`` object.  If ``tzinfo`` is ``None``, it is
                treated as UTC.  If it has a timezone, it is converted to UTC
                before computing the delta.

        Returns:
            Non-negative float age in seconds.  If *timestamp* is in the
            future (within clock-skew tolerance), returns ``0.0``.
        """
        now_utc = datetime.now(tz=timezone.utc)
        # Treat naive datetimes as UTC
        if timestamp.tzinfo is None:
            ts_utc = timestamp.replace(tzinfo=timezone.utc)
        else:
            ts_utc = timestamp.astimezone(timezone.utc)
        delta = now_utc - ts_utc
        return max(0.0, delta.total_seconds())

    @staticmethod
    def age_seconds_from_ms(event_time_ms: int) -> float:
        """Return the age of a UTC-epoch-millisecond timestamp in seconds.

        Args:
            event_time_ms: UTC epoch time in milliseconds (integer).

        Returns:
            Non-negative float age in seconds.
        """
        now_ms = time.time() * 1000.0
        delta_ms = now_ms - event_time_ms
        return max(0.0, delta_ms / 1000.0)

    # ------------------------------------------------------------------
    # Live-quote freshness (instrument tier + session phase)
    # Requirements 7.3 and 7.4
    # ------------------------------------------------------------------

    @staticmethod
    def classify_live(
        event_time_ms: int,
        instrument_tier: str,
        is_regular_session: bool,
    ) -> FreshnessStatus:
        """Classify freshness of a live quote by instrument tier and session phase.

        This implements the thresholds from Requirements 7.3 and 7.4:

        *REGULAR session:*
          - INDEX / FO_LIQUID: ≤ 10s → FRESH
          - FO_NORMAL:         ≤ 15s → FRESH
          - EQUITY:            ≤ 30s → FRESH

        *Non-REGULAR session (extended windows):*
          - INDEX / FO_LIQUID: ≤ 60s → FRESH
          - FO_NORMAL:         ≤ 90s → FRESH
          - EQUITY:            ≤ 120s → FRESH

        Ages within the stale window are classified STALE; beyond it, EXPIRED.

        Args:
            event_time_ms: Exchange event timestamp in UTC epoch milliseconds.
            instrument_tier: One of ``"INDEX"``, ``"FO_LIQUID"``, ``"FO_NORMAL"``,
                ``"EQUITY"``.  Unknown tiers fall back to ``EQUITY`` thresholds
                with a logged warning.
            is_regular_session: ``True`` when the current NSE session phase is
                ``REGULAR``; ``False`` for PRE_OPEN, POST_MARKET, CLOSED, etc.

        Returns:
            A :class:`FreshnessStatus` value.
        """
        tier = instrument_tier.upper()
        thresholds_map = (
            _LIVE_THRESHOLDS_REGULAR if is_regular_session else _LIVE_THRESHOLDS_NON_REGULAR
        )
        if tier not in thresholds_map:
            logger.warning(
                "Unknown instrument_tier for freshness classification; falling back to EQUITY",
                extra={"instrument_tier": instrument_tier},
            )
            tier = "EQUITY"

        thresholds = thresholds_map[tier]
        age = FreshnessClassifier.age_seconds_from_ms(event_time_ms)

        if age <= thresholds.fresh_sec:
            return FreshnessStatus.FRESH
        if age <= thresholds.stale_sec:
            return FreshnessStatus.STALE
        return FreshnessStatus.EXPIRED

    # ------------------------------------------------------------------
    # Interval-based candle freshness (market + interval)
    # Task 9.3
    # ------------------------------------------------------------------

    @staticmethod
    def classify(
        timestamp: datetime,
        market: str,
        interval: str,
    ) -> FreshnessStatus:
        """Classify freshness of a candle dataset by market vertical and interval.

        India thresholds (seconds):
          - 1m:  FRESH ≤ 2 min,  STALE ≤ 10 min, EXPIRED > 10 min
          - 5m:  FRESH ≤ 7 min,  STALE ≤ 30 min, EXPIRED > 30 min
          - 10m: FRESH ≤ 10 min, STALE ≤ 50 min, EXPIRED > 50 min
          - 15m: FRESH ≤ 20 min, STALE ≤ 60 min, EXPIRED > 60 min
          - 30m: FRESH ≤ 40 min, STALE ≤ 90 min, EXPIRED > 90 min
          - 1h:  FRESH ≤ 70 min, STALE ≤ 3 h,    EXPIRED > 3 h
          - 1d:  FRESH ≤ 26 h,   STALE ≤ 48 h,   EXPIRED > 48 h
          - 1w:  FRESH ≤ 7 days, STALE ≤ 14 days, EXPIRED > 14 days
          - 1M:  FRESH ≤ 30 days, STALE ≤ 60 days, EXPIRED > 60 days

        Crypto thresholds = India thresholds × 1.5 (crypto trades 24/7).
        Crypto additionally supports 3m, 2h, 4h, 6h, 8h, 12h intervals.

        The ``3m`` interval is **permanently banned for Indian market data**;
        passing ``interval="3m"`` with ``market="india"`` raises ``ValueError``.

        Args:
            timestamp: The ``datetime`` of the most recent candle in the dataset.
                Naive datetimes are treated as UTC.
            market: ``"india"`` or ``"crypto"`` (case-insensitive).
            interval: Candle interval string, e.g. ``"1m"``, ``"5m"``, ``"1h"``.

        Returns:
            A :class:`FreshnessStatus` value (FRESH, STALE, EXPIRED, or UNKNOWN
            when the interval is not recognised).

        Raises:
            ValueError: When ``market="india"`` and ``interval="3m"``.

        Requirements: Task 9.3, Requirement 1.5 (3m ban for India)
        """
        market_normalised = market.lower()
        interval_normalised = interval.lower()

        # Hard-ban 3m for Indian market data
        if market_normalised == "india" and interval_normalised == "3m":
            raise ValueError(
                "interval 3m is permanently unsupported for Indian market data"
            )

        # Select threshold table
        if market_normalised == "india":
            thresholds_map = _INDIA_INTERVAL_THRESHOLDS
        elif market_normalised == "crypto":
            thresholds_map = _CRYPTO_INTERVAL_THRESHOLDS
        else:
            logger.warning(
                "Unknown market for freshness classification; cannot determine thresholds",
                extra={"market": market, "interval": interval},
            )
            return FreshnessStatus.UNKNOWN

        thresholds = thresholds_map.get(interval_normalised)
        if thresholds is None:
            logger.warning(
                "Unrecognised interval for freshness classification",
                extra={"market": market, "interval": interval},
            )
            return FreshnessStatus.UNKNOWN

        age = FreshnessClassifier.age_seconds(timestamp)

        if age <= thresholds.fresh_sec:
            return FreshnessStatus.FRESH
        if age <= thresholds.stale_sec:
            return FreshnessStatus.STALE
        return FreshnessStatus.EXPIRED


# ---------------------------------------------------------------------------
# QualityEngine
# ---------------------------------------------------------------------------


class QualityEngine:
    """Computes DataConfidenceScore and provides grading utilities.

    This class is a pure-logic engine with no external I/O.  Instances are
    stateless; all methods may be called concurrently without locking.

    Tasks 9.2–9.9 will extend this class with DataQualityGate evaluation,
    per-instrument freshness classification, quality classification, option
    chain quality checks, strategy-specific overrides, and alerting.
    """

    # ------------------------------------------------------------------
    # Core score computation (task 9.1)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_confidence_score(
        freshness_score: int,
        completeness_score: int,
        provider_health_score: int,
        timestamp_valid: bool,
        cross_source_agreement: float,
        sequence_integrity: bool,
    ) -> int:
        """Compute a DataConfidenceScore from pre-scaled component values.

        All component scores must already be within their defined ranges when
        passed to this method (see parameter docs below).  The method does NOT
        clamp inputs — callers are responsible for providing valid values.

        Args:
            freshness_score: 0–35.
                FRESH→35, AGING→25, STALE→10, EXPIRED→0, UNKNOWN→5.
            completeness_score: 0–25.
                Typically derived as ``int(completeness_percent * 0.25)`` where
                ``completeness_percent`` is in [0, 100].
            provider_health_score: 0–20.
            timestamp_valid: If True → adds 10 to score; if False → adds 0.
            cross_source_agreement: Float in [0.0, 1.0].
                Agreement score = ``int(cross_source_agreement * 10)``.
            sequence_integrity: If False, a 20% penalty is applied to the
                accumulated score before capping.

        Returns:
            Integer DataConfidenceScore in [0, 95].

        Requirements: 7.1, 9.1
        """
        timestamp_score: int = _TIMESTAMP_SCORE_TRUE if timestamp_valid else _TIMESTAMP_SCORE_FALSE
        agreement_score: int = int(cross_source_agreement * _AGREEMENT_SCORE_MAX)

        raw_score: int = (
            freshness_score
            + completeness_score
            + provider_health_score
            + timestamp_score
            + agreement_score
        )

        # Apply sequence-integrity penalty before capping (Requirement 7.1)
        if not sequence_integrity:
            raw_score = int(raw_score * _SEQUENCE_PENALTY_FACTOR)

        # Clamp: always in [0, 95]
        return max(0, min(raw_score, _SCORE_MAX))

    @staticmethod
    def grade_score(score: int) -> str:
        """Return the grade label for a DataConfidenceScore.

        Grade bands:
            BLOCKED  score < 30
            LOW      30 ≤ score < 50
            MEDIUM   50 ≤ score < 80
            HIGH     80 ≤ score ≤ 95

        Args:
            score: Integer in [0, 95].

        Returns:
            One of ``"BLOCKED"``, ``"LOW"``, ``"MEDIUM"``, ``"HIGH"``.
        """
        if score < _GRADE_BLOCKED_THRESHOLD:
            return "BLOCKED"
        if score < _GRADE_LOW_THRESHOLD:
            return "LOW"
        if score < _GRADE_MEDIUM_THRESHOLD:
            return "MEDIUM"
        return "HIGH"

    @staticmethod
    def compute_confidence_from_inputs(
        freshness_classification: str,
        completeness_percent: float,
        provider_healthy: bool,
        timestamp_valid: bool,
        cross_source_agreement: float,
        sequence_integrity_ok: bool,
    ) -> int:
        """Higher-level convenience method — maps enum-style inputs to a score.

        This method converts human-readable / enum-style inputs to their
        numeric component scores and delegates to ``compute_confidence_score``.

        Args:
            freshness_classification: One of ``"FRESH"``, ``"AGING"``,
                ``"STALE"``, ``"EXPIRED"``, ``"UNKNOWN"``.  Unrecognised values
                are treated as ``"UNKNOWN"`` (score 5) and a warning is logged.
            completeness_percent: Float in [0.0, 100.0].  Scaled to a 0–25
                component via ``int(completeness_percent * 0.25)``.
            provider_healthy: True → provider_score 20; False → 0.
            timestamp_valid: Passed directly to ``compute_confidence_score``.
            cross_source_agreement: Float in [0.0, 1.0], passed directly.
            sequence_integrity_ok: True means no penalty; False applies 20%
                penalty.

        Returns:
            Integer DataConfidenceScore in [0, 95].

        Requirements: 7.1, 9.1
        """
        freshness_score: int = _FRESHNESS_SCORE.get(freshness_classification, _FRESHNESS_SCORE["UNKNOWN"])
        if freshness_classification not in _FRESHNESS_SCORE:
            logger.warning(
                "Unknown freshness_classification received",
                extra={
                    "freshness_classification": freshness_classification,
                    "fallback_score": freshness_score,
                },
            )

        # Scale completeness: 100% → 25, 0% → 0 (integer truncation)
        completeness_score: int = int(max(0.0, min(completeness_percent, 100.0)) * 0.25)

        provider_health_score: int = _PROVIDER_HEALTH_SCORE_MAX if provider_healthy else 0

        return QualityEngine.compute_confidence_score(
            freshness_score=freshness_score,
            completeness_score=completeness_score,
            provider_health_score=provider_health_score,
            timestamp_valid=timestamp_valid,
            cross_source_agreement=cross_source_agreement,
            sequence_integrity=sequence_integrity_ok,
        )

    # ------------------------------------------------------------------
    # signalEngineAllowed gate helper (Requirement 7.11)
    # ------------------------------------------------------------------

    @staticmethod
    def is_signal_engine_allowed(score: int) -> bool:
        """Return whether the Signal Engine may use data with this score.

        A score below 30 (grade BLOCKED) unconditionally blocks the signal
        engine — no exception exists (Requirement 7.11).

        Note: this is a lightweight helper for the score-only check.  The full
        DataQualityGate (tasks 9.2–9.6) additionally enforces the five boolean
        gate conditions (dataFresh, dataComplete, dataTimestampValid,
        dataProviderHealthy, dataSemanticallyValid).

        Args:
            score: Integer DataConfidenceScore in [0, 95].

        Returns:
            ``False`` when ``score < 30``; ``True`` otherwise.
        """
        return score >= _GRADE_BLOCKED_THRESHOLD

    # ------------------------------------------------------------------
    # DataQualityGate evaluation (task 9.2, Requirements 7.2, 7.9)
    # ------------------------------------------------------------------

    def evaluate_gate(
        self,
        data: dict[str, Any],
        *,
        min_confidence_score: float = _DEFAULT_MIN_CONFIDENCE_SCORE,
    ) -> "GateResult":
        """Evaluate the DataQualityGate's five conditions against a data dict.

        This is the primary entry point for quality gate evaluation.  Each
        condition is checked independently; all five must pass for the gate to
        allow downstream signal-engine use.

        The five conditions map to the design specification:
            1. **Freshness**  (dataFresh) — data.quoteAgeMs is within the
               freshness window declared by the caller, OR the ``eventTimeMs``
               timestamp is not older than 30 days. Callers that want strict
               freshness enforcement should pre-classify freshness and pass
               ``freshness_classification`` alongside the data dict.
            2. **Completeness** (dataComplete) — all required OHLCV fields
               (symbol, timestamp, open, high, low, close, volume) are present
               and non-None.
            3. **Consistency / Timestamp validity** (dataTimestampValid) — OHLCV
               invariants hold (high ≥ max(open,close), low ≤ min(open,close),
               volume ≥ 0, prices > 0) AND the timestamp is not negative,
               not in the far future, and not excessively old.
            4. **Source availability** (dataProviderHealthy) — the
               ``providerAvailable`` flag in the data dict is True, or the
               ``source`` field is non-empty. Represents "at least one data
               source is marked available for the symbol".
            5. **Confidence** (dataSemanticallyValid) — the pre-computed
               ``confidenceScore`` in the data dict (or recomputed from helper
               fields if present) is ≥ ``min_confidence_score`` (default 60).

        Args:
            data: Dict representing a market data observation.  Expected keys:
                - ``symbol``              (str, required for completeness)
                - ``timestamp``           (int ms or str, required)
                - ``open``                (float, required for OHLCV)
                - ``high``                (float, required)
                - ``low``                 (float, required)
                - ``close``               (float, required)
                - ``volume``              (int/float ≥ 0, required)
                - ``eventTimeMs``         (int ms, optional — used for freshness)
                - ``quoteAgeMs``          (int ms, optional — explicit age)
                - ``freshnessFreshMs``    (int ms, optional — max acceptable age)
                - ``providerAvailable``   (bool, optional — source availability)
                - ``source``              (str, optional — source identifier)
                - ``confidenceScore``     (float 0–95, optional)
            min_confidence_score: Minimum DataConfidenceScore required to pass
                the confidence condition.  Must be ≥ 0.  Defaults to 60.

        Returns:
            ``GateResult`` with ``passed``, ``failed_conditions``, and
            ``score`` fields, plus the full ``DataQualityGate`` breakdown.

        Requirements: 7.2, 7.9
        """
        gate = DataQualityGate.evaluate(data, min_confidence_score=min_confidence_score)
        return GateResult(
            passed=gate.signalEngineAllowed,
            failed_conditions=gate.blockReasons,
            score=float(gate.confidenceScore),
            gate=gate,
        )

    # ------------------------------------------------------------------
    # Stubs — to be implemented in tasks 9.3–9.9
    # ------------------------------------------------------------------

    def classify_freshness(
        self,
        event_time_ms: int,
        instrument_tier: str,
        is_regular_session: bool,
    ) -> FreshnessStatus:
        """Classify freshness of a live quote by instrument tier and session phase.

        Delegates to :meth:`FreshnessClassifier.classify_live`.

        Args:
            event_time_ms: Exchange event timestamp in UTC epoch milliseconds.
            instrument_tier: One of ``"INDEX"``, ``"FO_LIQUID"``, ``"FO_NORMAL"``,
                ``"EQUITY"``.
            is_regular_session: ``True`` when the NSE session phase is ``REGULAR``.

        Returns:
            A :class:`FreshnessStatus` value.

        Requirements: 7.3, 7.4
        """
        return FreshnessClassifier.classify_live(
            event_time_ms=event_time_ms,
            instrument_tier=instrument_tier,
            is_regular_session=is_regular_session,
        )

    def classify_quality(
        self,
        score: int,
        gate_result: "GateResult",
    ) -> "QualityClassification":
        """Classify overall quality of a market data observation.

        Maps a DataConfidenceScore and a GateResult into a QualityClassification
        that exposes the grade, signal-engine allowance, and a consolidated list
        of reasons for any degraded state.

        Grade mapping (mirrors grade_score — Requirement 7.5 / design spec):
            score ≥ 80                 → HIGH
            50 ≤ score < 80            → MEDIUM
            30 ≤ score < 50            → LOW
            score < 30                 → BLOCKED

        ``signalEngineAllowed`` is ``True`` only when BOTH:
            - score ≥ 30 (not BLOCKED), AND
            - gate_result.passed is True  (all five gate conditions passed)

        ``reasons`` is empty for HIGH (all-good), otherwise contains:
            - the gate's failed_conditions when any gate condition failed, AND/OR
            - a grade-explanation string when the score places the data in
              MEDIUM, LOW, or BLOCKED.

        Args:
            score: Integer DataConfidenceScore in [0, 95].
            gate_result: Result from DataQualityGate.check() or
                QualityEngine.evaluate_gate().

        Returns:
            QualityClassification instance.

        Requirements: 7.1, 7.2, 7.5, 7.11
        """
        grade = self.grade_score(score)
        signal_allowed = (score >= _GRADE_BLOCKED_THRESHOLD) and gate_result.passed

        reasons: list[str] = []

        # Include any gate-level failures (conditions that didn't pass)
        if gate_result.failed_conditions:
            reasons.extend(gate_result.failed_conditions)

        # Add a grade-level reason when quality is not HIGH
        if grade == "BLOCKED":
            if not any("confidenceScore" in r or "score" in r.lower() for r in reasons):
                reasons.append(
                    f"grade=BLOCKED: DataConfidenceScore={score} < 30 "
                    f"(signalEngineAllowed=False with no exceptions)"
                )
        elif grade == "LOW":
            reasons.append(
                f"grade=LOW: DataConfidenceScore={score} is in [30, 50) "
                f"(reduced confidence)"
            )
        elif grade == "MEDIUM":
            reasons.append(
                f"grade=MEDIUM: DataConfidenceScore={score} is in [50, 80) "
                f"(moderate confidence)"
            )

        return QualityClassification(
            grade=grade,
            signalEngineAllowed=signal_allowed,
            score=score,
            reasons=reasons,
        )

    def validate_option_chain_record(
        self,
        record: dict[str, Any],
    ) -> "OptionChainValidationResult":
        """Validate a single option chain row against quality rules.

        Validation rules (all applied independently — multiple issues may be
        reported in a single call):

        1. ``optionType`` must be ``"CE"`` or ``"PE"`` (case-insensitive).
        2. ``strike`` must be > 0.
        3. ``ltp`` must be >= 0 when present (not None).
        4. ``oi`` must be >= 0 when present.
        5. ``volume`` must be >= 0 when present.
        6. ``iv`` must be in (0, 500] when present — 0 is suspicious (placeholder
           zero), >500 is impossible as a percentage.
        7. ``bid`` >= 0 and ``ask`` >= bid when both are present.
        8. ``expiry`` must be a valid YYYY-MM-DD date that is today or in the
           future.
        9. When ``bid`` > 0 and ``ask`` > 0 and ``ltp`` is present and > 0:
           the spread (ask - bid) must be < 50% of ltp (spread quality check).

        All violations are collected before returning — the caller receives a
        complete picture of every problem in a single pass.

        Each violation also generates a ``DataIncident`` log entry with
        ``symbol``, ``violationType``, ``measuredValue``, and
        ``detectionTimestamp`` (Requirement 7.10).

        Args:
            record: Raw dict representing one option chain row.  Expected keys
                match ``OptionChainRecord`` — missing required fields cause a
                validation failure that is reported in ``issues`` rather than
                raising an exception.

        Returns:
            ``OptionChainValidationResult`` with ``valid``, ``issues``, and the
            original ``record`` dict.

        Requirements: 7.10, 3.7, 3.8, 3.9, 3.10
        """
        issues: list[str] = []
        symbol: str = str(record.get("symbol", "UNKNOWN"))
        detection_ts = datetime.now(tz=timezone.utc).isoformat()

        # ---- Parse the record through the Pydantic model ------------------
        # We deliberately do NOT raise on parse failure so that callers always
        # get an OptionChainValidationResult back with the issues listed.
        try:
            parsed = OptionChainRecord.model_validate(record)
        except Exception as exc:  # noqa: BLE001
            # Pydantic validation error — report each field error as an issue
            issues.append(f"schema_validation_failed: {exc}")
            return OptionChainValidationResult(valid=False, issues=issues, record=record)

        # ---- Rule 1: optionType -------------------------------------------
        if parsed.optionType.upper() not in ("CE", "PE"):
            msg = (
                f"invalid_option_type: optionType={parsed.optionType!r} "
                f"must be 'CE' or 'PE'"
            )
            issues.append(msg)
            self._log_incident(symbol, "INVALID_OPTION_TYPE", parsed.optionType, detection_ts)

        # ---- Rule 2: strike > 0 ------------------------------------------
        if parsed.strike <= 0:
            msg = f"invalid_strike: strike={parsed.strike} must be > 0"
            issues.append(msg)
            self._log_incident(symbol, "INVALID_STRIKE", parsed.strike, detection_ts)

        # ---- Rule 3: ltp >= 0 when present --------------------------------
        if parsed.ltp is not None and parsed.ltp < 0:
            msg = f"invalid_ltp: ltp={parsed.ltp} must be >= 0"
            issues.append(msg)
            self._log_incident(symbol, "INVALID_LTP", parsed.ltp, detection_ts)

        # ---- Rule 4: oi >= 0 when present ---------------------------------
        if parsed.oi is not None and parsed.oi < 0:
            msg = f"negative_oi: oi={parsed.oi} must be >= 0"
            issues.append(msg)
            self._log_incident(symbol, "NEGATIVE_OI", parsed.oi, detection_ts)

        # ---- Rule 5: volume >= 0 when present ----------------------------
        if parsed.volume is not None and parsed.volume < 0:
            msg = f"negative_volume: volume={parsed.volume} must be >= 0"
            issues.append(msg)
            self._log_incident(symbol, "NEGATIVE_VOLUME", parsed.volume, detection_ts)

        # ---- Rule 6: iv in (0, 500] when present -------------------------
        # IV of 0 is never a valid substitute for absent IV (Requirement 6.4).
        if parsed.iv is not None:
            if parsed.iv <= 0:
                msg = (
                    f"invalid_iv: iv={parsed.iv} must be > 0 "
                    f"(zero IV is not a valid substitute for absent IV)"
                )
                issues.append(msg)
                self._log_incident(symbol, "NEGATIVE_IV", parsed.iv, detection_ts)
            elif parsed.iv > 500:
                msg = (
                    f"invalid_iv: iv={parsed.iv} exceeds maximum plausible value of 500%"
                )
                issues.append(msg)
                self._log_incident(symbol, "INVALID_IV", parsed.iv, detection_ts)

        # ---- Rule 7: bid/ask relationship when both present --------------
        if parsed.bid is not None and parsed.ask is not None:
            if parsed.bid < 0:
                msg = f"invalid_bid: bid={parsed.bid} must be >= 0"
                issues.append(msg)
                self._log_incident(symbol, "INVALID_BID", parsed.bid, detection_ts)
            if parsed.ask < parsed.bid:
                msg = (
                    f"crossed_market: bid={parsed.bid} > ask={parsed.ask} "
                    f"(crossed markets are invalid)"
                )
                issues.append(msg)
                self._log_incident(symbol, "CROSSED_MARKET", parsed.ask, detection_ts)

        # ---- Rule 8: expiry is today or in the future --------------------
        try:
            expiry_date = date.fromisoformat(parsed.expiry)
            today = datetime.now(tz=timezone.utc).date()
            if expiry_date < today:
                msg = (
                    f"expired_contract: expiry={parsed.expiry} is in the past "
                    f"(today={today.isoformat()})"
                )
                issues.append(msg)
                self._log_incident(symbol, "EXPIRED_CONTRACT", parsed.expiry, detection_ts)
        except ValueError:
            msg = (
                f"invalid_expiry: expiry={parsed.expiry!r} is not a valid "
                f"YYYY-MM-DD date"
            )
            issues.append(msg)
            self._log_incident(symbol, "INVALID_EXPIRY", parsed.expiry, detection_ts)

        # ---- Rule 9: bid-ask spread < 50% of LTP -------------------------
        # Only checked when bid > 0, ask > 0, and ltp > 0.
        if (
            parsed.bid is not None
            and parsed.ask is not None
            and parsed.ltp is not None
            and parsed.bid > 0
            and parsed.ask > 0
            and parsed.ltp > 0
        ):
            spread = parsed.ask - parsed.bid
            spread_pct = (spread / parsed.ltp) * 100.0
            if spread_pct >= 50.0:
                msg = (
                    f"wide_spread: bid-ask spread {spread:.4f} is "
                    f"{spread_pct:.1f}% of ltp={parsed.ltp} "
                    f"(threshold 50%)"
                )
                issues.append(msg)
                self._log_incident(symbol, "WIDE_SPREAD", spread_pct, detection_ts)

        return OptionChainValidationResult(
            valid=len(issues) == 0,
            issues=issues,
            record=record,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _log_incident(
        symbol: str,
        violation_type: str,
        measured_value: Any,
        detection_ts: str,
    ) -> None:
        """Emit a structured DataIncident log entry (Requirement 7.10)."""
        logger.warning(
            "DataIncident detected",
            extra={
                "event": "data_incident",
                "symbol": symbol,
                "violationType": violation_type,
                "measuredValue": measured_value,
                "detectionTimestamp": detection_ts,
            },
        )

    def apply_strategy_overrides(
        self,
        score: int,
        gate_result: "GateResult",
        profile: "StrategyQualityProfile",
    ) -> "QualityClassification":
        """Apply strategy-specific quality overrides on top of the base gate result.

        Strategy overrides are additive — they tighten constraints but can never
        relax any global gate condition (Requirement 7.8).  The hard BLOCKED
        floor (score < 30) is always enforced regardless of the profile
        (Requirement 7.11).

        Override logic applied in order:

        1. **BLOCKED floor** — when score < 30, ``signalEngineAllowed`` is always
           ``False`` with no exceptions.  No profile field can override this.

        2. **minConfidenceScore** — when ``score < profile.minConfidenceScore``
           but ``score >= 30``, a strategy-specific warning reason is appended
           but ``signalEngineAllowed`` is NOT set to ``False`` by this condition
           alone (the base gate may already block it for other reasons).

           Rationale: the spec says strategy overrides apply *in addition to*
           the global gate but cannot relax it.  A score in [30, profile.minConfidenceScore)
           means the global gate might still allow the signal engine (score ≥ 30,
           all five conditions pass).  The strategy has declared it wants a higher
           floor, so we surface a reason but do not double-block — the caller
           should inspect reasons to decide whether to proceed.

           If the base gate was already blocked, nothing changes.

        3. **requireFreshData + freshness gate failure** — when
           ``profile.requireFreshData is True`` and the base gate's
           ``dataFresh`` condition failed, ``signalEngineAllowed`` is forced to
           ``False`` with an explicit strategy-specific reason.  (When
           ``requireFreshData is False`` the strategy accepts stale data and
           freshness failure is not escalated.)

        The returned ``QualityClassification`` preserves the base ``grade``
        (grade is purely score-driven and not overridable) and ``score`` fields,
        but may have a modified ``signalEngineAllowed`` and an augmented
        ``reasons`` list.

        Args:
            score: DataConfidenceScore in [0, 95].
            gate_result: Result from ``DataQualityGate.check()`` or
                ``QualityEngine.evaluate_gate()``.
            profile: ``StrategyQualityProfile`` declaring the strategy's
                data requirements.

        Returns:
            A new ``QualityClassification`` with strategy-aware
            ``signalEngineAllowed`` and ``reasons``.

        Requirements: 7.8, 7.11
        """
        # Start from the base classification
        base = self.classify_quality(score, gate_result)

        # Collect any additional reasons introduced by strategy overrides
        extra_reasons: list[str] = []

        # 1. Hard BLOCKED floor — absolute, no exceptions (Requirement 7.11)
        if score < _GRADE_BLOCKED_THRESHOLD:
            # base.signalEngineAllowed is already False; nothing to do except
            # preserve the state as-is.
            return base

        # Working copy of signalEngineAllowed — start from base decision
        signal_allowed = base.signalEngineAllowed

        # 2. Strategy minConfidenceScore advisory (does NOT block by itself)
        effective_min = max(profile.minConfidenceScore, _GRADE_BLOCKED_THRESHOLD)
        if score < effective_min:
            extra_reasons.append(
                f"strategy '{profile.strategyId}' requires minConfidenceScore="
                f"{effective_min} but got {score} "
                f"(score is in acceptable global range [30, 95] but below "
                f"strategy threshold)"
            )

        # 3. requireFreshData — if strategy requires fresh data and gate failed freshness,
        #    force signalEngineAllowed to False.
        gate_model = gate_result.gate
        freshness_failed = (
            gate_model is not None and not gate_model.dataFresh
        ) or any("dataFresh=False" in r for r in gate_result.failed_conditions)

        if profile.requireFreshData and freshness_failed:
            signal_allowed = False
            extra_reasons.append(
                f"strategy '{profile.strategyId}' requireFreshData=True "
                f"but data freshness gate failed"
            )

        # Build merged reasons list: base reasons first, then strategy extras
        merged_reasons = list(base.reasons) + extra_reasons

        # If nothing changed, return the base result unchanged
        if signal_allowed == base.signalEngineAllowed and not extra_reasons:
            return base

        return QualityClassification(
            grade=base.grade,
            signalEngineAllowed=signal_allowed,
            score=base.score,
            reasons=merged_reasons,
        )


# ---------------------------------------------------------------------------
# OptionChainRecord — canonical single option chain row for validation
# ---------------------------------------------------------------------------


class OptionChainRecord(BaseModel):
    """Canonical representation of a single option chain row.

    All nullable fields (``ltp``, ``oi``, ``volume``, ``iv``, ``bid``,
    ``ask``) may be ``None`` — **never substitute ``0`` for absent data**
    (Requirements 6.2, 6.4, 6.5, 6.6).

    Fields:
        symbol:      NSE symbol / ticker string (e.g. ``"NIFTY"``).
        expiry:      Expiry date as ``YYYY-MM-DD`` ISO-8601 string.
        strike:      Strike price — must be > 0.
        optionType:  ``"CE"`` (call) or ``"PE"`` (put), case-insensitive on
                     input but stored as-is; validation normalises the check
                     case-insensitively.
        ltp:         Last traded price — >= 0 when present; ``None`` when the
                     provider did not supply a value.
        oi:          Open interest in contracts — >= 0 when present; ``None``
                     when absent.  Never populated from ``tradedValue``.
        volume:      Traded volume (number of contracts) — >= 0 when present;
                     ``None`` when absent.
        iv:          Implied volatility as a percentage in (0, 500] when
                     present; ``None`` when the provider did not supply a
                     value.  Zero is **not** a valid substitute.
        bid:         Best bid price — >= 0 when present; ``None`` when absent.
        ask:         Best ask price — >= bid when both present; ``None`` when
                     absent.
        timestamp:   UTC epoch milliseconds of the observation (required).

    Requirements: 3.7, 7.10
    """

    model_config = {"frozen": True}

    symbol: str
    expiry: str                       # YYYY-MM-DD; further validation in QualityEngine
    strike: float
    optionType: str
    ltp: Optional[float] = None
    oi: Optional[float] = None
    volume: Optional[float] = None
    iv: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    timestamp: int                    # UTC epoch ms

    @field_validator("symbol")
    @classmethod
    def symbol_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("symbol must be a non-empty string")
        return v

    @field_validator("timestamp")
    @classmethod
    def timestamp_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"timestamp={v} must be a positive integer (UTC epoch ms)")
        return v


# ---------------------------------------------------------------------------
# OptionChainValidationResult — returned by QualityEngine.validate_option_chain_record
# ---------------------------------------------------------------------------


class OptionChainValidationResult(BaseModel):
    """Result of ``QualityEngine.validate_option_chain_record()``.

    Fields:
        valid:   ``True`` when all quality checks passed for this row;
                 ``False`` when one or more issues were detected.
        issues:  Human-readable list of issue strings — one entry per rule
                 violation.  Empty when ``valid=True``.
        record:  The original input dict passed to the validator (not
                 modified).

    Requirements: 7.10
    """

    model_config = {"frozen": True}

    valid: bool
    issues: list[str] = Field(default_factory=list)
    record: dict[str, Any]


# ---------------------------------------------------------------------------
# QualityClassification — result returned by QualityEngine.classify_quality
# ---------------------------------------------------------------------------


class QualityClassification(BaseModel):
    """Result of ``QualityEngine.classify_quality()``.

    Fields:
        grade:               One of ``"HIGH"``, ``"MEDIUM"``, ``"LOW"``,
                             ``"BLOCKED"``, corresponding directly to the
                             DataConfidenceScore bands from ``grade_score()``.
        signalEngineAllowed: ``True`` iff grade is not BLOCKED **and** the
                             accompanying DataQualityGate passed all five
                             conditions.  ``False`` otherwise — no exceptions.
        score:               The DataConfidenceScore (0–95).
        reasons:             Empty list for ``HIGH`` (all-good case); for all
                             other grades contains at least one reason string
                             explaining what degraded the quality, including
                             any failed gate conditions.

    Grade bands (Requirements 7.1, 7.5):
        HIGH    score ≥ 80
        MEDIUM  50 ≤ score < 80
        LOW     30 ≤ score < 50
        BLOCKED score < 30  → signalEngineAllowed always False (Requirement 7.11)

    Requirements: 7.1, 7.2, 7.5, 7.11
    """

    model_config = {"frozen": True}

    grade: str
    signalEngineAllowed: bool
    score: int = Field(ge=0, le=95)
    reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# StrategyQualityProfile — declares per-strategy data requirements
# ---------------------------------------------------------------------------


class StrategyQualityProfile(BaseModel):
    """Per-strategy data quality requirements passed to
    ``QualityEngine.apply_strategy_overrides()``.

    Strategy overrides are *additive*: they tighten the global gate but can
    never relax any global gate condition.  In particular:
    - ``minConfidenceScore`` below 30 is automatically raised to 30 so that
      the global BLOCKED threshold (Requirement 7.11) can never be bypassed.
    - Setting ``requireFreshData=False`` allows the strategy to tolerate stale
      data, but it cannot override other global gate failures.

    Fields:
        strategyId:               Unique identifier for the strategy.
        minConfidenceScore:       Minimum acceptable ``DataConfidenceScore``
                                  (0–95).  Values below 30 are clamped to 30
                                  at runtime to preserve the BLOCKED floor.
                                  Defaults to 60.
        requireFreshData:         When ``True`` (default), a freshness gate
                                  failure forces ``signalEngineAllowed=False``
                                  even if the base gate would otherwise allow
                                  the signal engine.
        allowedFreshnessStatuses: Optional whitelist of acceptable freshness
                                  status strings (e.g. ``["FRESH", "AGING"]``).
                                  Currently used for documentation / future
                                  freshness-classifier integration; the
                                  ``apply_strategy_overrides`` method enforces
                                  the ``requireFreshData`` flag independently.
        maxOIVariancePercent:     Optional maximum acceptable OI variance
                                  percentage for option strategies.  Reserved
                                  for future option chain quality checks.
        minLiquidityVolume:       Optional minimum acceptable trading volume
                                  threshold for liquidity filter.  Reserved
                                  for future liquidity gate integration.

    Requirements: 7.8
    """

    model_config = {"frozen": True}

    strategyId: str
    minConfidenceScore: int = Field(default=60, ge=0, le=95)
    requireFreshData: bool = True
    allowedFreshnessStatuses: list[str] = Field(
        default_factory=lambda: ["FRESH", "AGING"]
    )
    maxOIVariancePercent: Optional[float] = Field(default=None, ge=0.0)
    minLiquidityVolume: Optional[float] = Field(default=None, ge=0.0)


# ---------------------------------------------------------------------------
# GateResult — lightweight Pydantic v2 result returned by DataQualityGate.check
# ---------------------------------------------------------------------------


class GateResult(BaseModel):
    """Result returned by ``DataQualityGate.check()`` and
    ``QualityEngine.evaluate_gate()``.

    Fields:
        passed:            True iff all five gate conditions passed.
        failed_conditions: List of human-readable reason strings for each
                           condition that failed.  Empty when passed=True.
        score:             The DataConfidenceScore used by the gate (0.0–95.0).
        gate:              Full ``DataQualityGate`` breakdown (optional
                           convenience reference; not included in equality
                           comparisons to keep assertions simple in tests).

    Requirements: 7.2, 7.9
    """

    model_config = {"frozen": True}

    passed: bool
    failed_conditions: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=95.0)
    gate: Optional["DataQualityGate"] = Field(default=None, exclude=True)


# ---------------------------------------------------------------------------
# DataQualityGate — Pydantic v2 model + evaluation logic
# ---------------------------------------------------------------------------


class DataQualityGate(BaseModel):
    """Five-condition signal-safety contract.

    Design specification (Quality Engine section):

        signalEngineAllowed = true iff all five are true:
            dataFresh ∧ dataComplete ∧ dataTimestampValid
                ∧ dataProviderHealthy ∧ dataSemanticallyValid

    When ``signalEngineAllowed`` is False, ``blockReasons`` contains at least
    one string explaining which condition failed and the measured value.

    Requirements: 7.2, 7.9
    """

    model_config = {"frozen": True}

    # Five boolean gate conditions
    dataFresh: bool
    dataComplete: bool
    dataTimestampValid: bool
    dataProviderHealthy: bool
    dataSemanticallyValid: bool

    # Derived
    signalEngineAllowed: bool   # True iff all five are True
    confidenceScore: int        # 0–95
    blockReasons: list[str] = Field(default_factory=list)
    gates: dict[str, bool] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Class-level entry point
    # ------------------------------------------------------------------

    @classmethod
    def evaluate(
        cls,
        data: dict[str, Any],
        *,
        min_confidence_score: float = _DEFAULT_MIN_CONFIDENCE_SCORE,
    ) -> "DataQualityGate":
        """Evaluate all five conditions against ``data`` and return a gate.

        Condition 1 — Freshness (dataFresh):
            True when ``quoteAgeMs <= freshnessFreshMs`` (if both present), OR
            the ``eventTimeMs`` timestamp is within 30 days of now, OR
            no timing information is present (optimistic default: True — callers
            that require strict freshness must supply timing fields).

        Condition 2 — Completeness (dataComplete):
            True when all of: ``symbol``, ``timestamp``, ``open``, ``high``,
            ``low``, ``close``, ``volume`` are present and non-None.

        Condition 3 — Timestamp validity + OHLCV consistency
            (dataTimestampValid):
            True when:
              - ``timestamp`` or ``eventTimeMs`` is a positive integer not older
                than 30 days and not more than 1 minute in the future
              - If OHLCV fields are present and numeric:
                  high >= max(open, close)
                  low  <= min(open, close)
                  volume >= 0
                  open, high, low, close > 0

        Condition 4 — Provider / source availability (dataProviderHealthy):
            True when ``providerAvailable`` is True in data, OR ``source`` is
            a non-empty string, OR ``provider`` is a non-empty string.

        Condition 5 — Confidence threshold (dataSemanticallyValid):
            True when ``confidenceScore`` in data >= ``min_confidence_score``.
            If ``confidenceScore`` is absent, defaults to 0.

        Args:
            data: Market-data observation dict.
            min_confidence_score: Minimum confidence score (default 60.0).

        Returns:
            DataQualityGate instance.
        """
        block_reasons: list[str] = []

        # ---- Condition 1: Freshness ----------------------------------------
        data_fresh = cls._check_freshness(data, block_reasons)

        # ---- Condition 2: Completeness -------------------------------------
        data_complete = cls._check_completeness(data, block_reasons)

        # ---- Condition 3: Timestamp validity + OHLCV consistency -----------
        data_timestamp_valid = cls._check_timestamp_and_ohlcv(data, block_reasons)

        # ---- Condition 4: Provider / source availability -------------------
        data_provider_healthy = cls._check_provider_availability(data, block_reasons)

        # ---- Condition 5: Confidence threshold (semantic validity) ---------
        confidence_score, data_semantically_valid = cls._check_confidence(
            data, min_confidence_score, block_reasons
        )

        # ---- Gate decision -------------------------------------------------
        all_passed = (
            data_fresh
            and data_complete
            and data_timestamp_valid
            and data_provider_healthy
            and data_semantically_valid
        )

        # When all passed, blockReasons must be empty (Requirement 7.9)
        if all_passed:
            block_reasons = []

        return cls(
            dataFresh=data_fresh,
            dataComplete=data_complete,
            dataTimestampValid=data_timestamp_valid,
            dataProviderHealthy=data_provider_healthy,
            dataSemanticallyValid=data_semantically_valid,
            signalEngineAllowed=all_passed,
            confidenceScore=confidence_score,
            blockReasons=block_reasons,
            gates={
                "dataFresh": data_fresh,
                "dataComplete": data_complete,
                "dataTimestampValid": data_timestamp_valid,
                "dataProviderHealthy": data_provider_healthy,
                "dataSemanticallyValid": data_semantically_valid,
            },
        )

    # ------------------------------------------------------------------
    # Public check() interface (task spec)
    # ------------------------------------------------------------------

    @classmethod
    def check(
        cls,
        data: dict[str, Any],
        *,
        min_confidence_score: float = _DEFAULT_MIN_CONFIDENCE_SCORE,
    ) -> "GateResult":
        """Evaluate the gate and return a ``GateResult``.

        This is the primary consumer-facing API specified by the task:
            ``DataQualityGate.check(data: dict) -> GateResult``

        Args:
            data: Market-data observation dict (see ``evaluate`` for keys).
            min_confidence_score: Minimum confidence score threshold.

        Returns:
            GateResult with ``passed``, ``failed_conditions``, and ``score``.
        """
        gate = cls.evaluate(data, min_confidence_score=min_confidence_score)
        return GateResult(
            passed=gate.signalEngineAllowed,
            failed_conditions=gate.blockReasons,
            score=float(gate.confidenceScore),
            gate=gate,
        )

    # ------------------------------------------------------------------
    # Private condition checkers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_freshness(data: dict[str, Any], reasons: list[str]) -> bool:
        """Condition 1: data is fresh.

        Returns True when:
          - ``quoteAgeMs`` <= ``freshnessFreshMs`` (both present), OR
          - ``eventTimeMs`` is within 30 days of now (present), OR
          - No timing information is supplied (optimistic default).
        """
        now_ms = int(time.time() * 1000)

        # Explicit age check
        quote_age_ms = data.get("quoteAgeMs")
        freshness_fresh_ms = data.get("freshnessFreshMs")
        if quote_age_ms is not None and freshness_fresh_ms is not None:
            try:
                age = int(quote_age_ms)
                threshold = int(freshness_fresh_ms)
                if age > threshold:
                    reasons.append(
                        f"dataFresh=False: quoteAgeMs={age}ms exceeds "
                        f"freshnessFreshMs={threshold}ms"
                    )
                    return False
            except (TypeError, ValueError):
                reasons.append(
                    f"dataFresh=False: quoteAgeMs or freshnessFreshMs is "
                    f"not a valid integer (quoteAgeMs={quote_age_ms!r})"
                )
                return False

        # Fallback: check absolute age via eventTimeMs
        event_time_ms = data.get("eventTimeMs") or data.get("timestamp")
        if event_time_ms is not None:
            try:
                ts = int(event_time_ms)
                if ts > 0:
                    age_ms = now_ms - ts
                    if age_ms > _TIMESTAMP_MAX_AGE_MS:
                        reasons.append(
                            f"dataFresh=False: eventTimeMs age {age_ms}ms "
                            f"exceeds max allowed {_TIMESTAMP_MAX_AGE_MS}ms (30 days)"
                        )
                        return False
            except (TypeError, ValueError):
                pass  # Can't parse; don't fail freshness on unparseable ts alone

        return True

    @staticmethod
    def _check_completeness(data: dict[str, Any], reasons: list[str]) -> bool:
        """Condition 2: all required fields are present and non-None."""
        missing: list[str] = [
            field
            for field in _REQUIRED_OHLCV_FIELDS
            if data.get(field) is None
        ]
        if missing:
            reasons.append(
                f"dataComplete=False: required fields missing or null: "
                f"{', '.join(missing)}"
            )
            return False
        return True

    @staticmethod
    def _check_timestamp_and_ohlcv(data: dict[str, Any], reasons: list[str]) -> bool:
        """Condition 3: timestamp in valid range AND OHLCV invariants hold.

        Timestamp checks:
            - Must be a positive integer (epoch ms)
            - Must not be older than 30 days
            - Must not be more than 1 minute in the future

        OHLCV consistency (when all four price fields are present & numeric):
            - high >= max(open, close)
            - low  <= min(open, close)
            - all prices > 0
            - volume >= 0
        """
        now_ms = int(time.time() * 1000)
        failures: list[str] = []

        # --- Timestamp validation ---
        ts_raw = data.get("timestamp") or data.get("eventTimeMs")
        if ts_raw is not None:
            try:
                ts = int(ts_raw)
                if ts <= 0:
                    failures.append(f"timestamp={ts} is not positive")
                else:
                    age_ms = now_ms - ts
                    if age_ms > _TIMESTAMP_MAX_AGE_MS:
                        failures.append(
                            f"timestamp age {age_ms}ms exceeds 30-day limit"
                        )
                    if ts > now_ms + _TIMESTAMP_FUTURE_TOLERANCE_MS:
                        failures.append(
                            f"timestamp={ts} is {ts - now_ms}ms in the future "
                            f"(tolerance {_TIMESTAMP_FUTURE_TOLERANCE_MS}ms)"
                        )
            except (TypeError, ValueError):
                failures.append(f"timestamp={ts_raw!r} is not a valid integer")

        # --- OHLCV consistency ---
        try:
            open_ = data.get("open")
            high = data.get("high")
            low = data.get("low")
            close = data.get("close")
            volume = data.get("volume")

            # Only validate if all four price fields are present
            if all(v is not None for v in (open_, high, low, close)):
                o, h, l, c = float(open_), float(high), float(low), float(close)

                if o <= 0:
                    failures.append(f"open={o} must be > 0")
                if h <= 0:
                    failures.append(f"high={h} must be > 0")
                if l <= 0:
                    failures.append(f"low={l} must be > 0")
                if c <= 0:
                    failures.append(f"close={c} must be > 0")

                if h < max(o, c):
                    failures.append(
                        f"high={h} < max(open={o}, close={c})={max(o, c)}"
                    )
                if l > min(o, c):
                    failures.append(
                        f"low={l} > min(open={o}, close={c})={min(o, c)}"
                    )

            if volume is not None:
                v = float(volume)
                if v < 0:
                    failures.append(f"volume={v} must be >= 0")

        except (TypeError, ValueError) as exc:
            failures.append(f"OHLCV field type error: {exc}")

        if failures:
            reasons.append(
                "dataTimestampValid=False: " + "; ".join(failures)
            )
            return False

        return True

    @staticmethod
    def _check_provider_availability(data: dict[str, Any], reasons: list[str]) -> bool:
        """Condition 4: at least one data source is marked available.

        Returns True when any of the following is satisfied:
          - ``providerAvailable`` is truthy
          - ``source`` is a non-empty string
          - ``provider`` is a non-empty string
        """
        provider_available = data.get("providerAvailable")
        source = data.get("source")
        provider = data.get("provider")

        if provider_available:
            return True
        if source and isinstance(source, str) and source.strip():
            return True
        if provider and isinstance(provider, str) and provider.strip():
            return True

        reasons.append(
            "dataProviderHealthy=False: no available source indicated "
            "(providerAvailable=False/absent, source and provider are empty/absent)"
        )
        return False

    @staticmethod
    def _check_confidence(
        data: dict[str, Any],
        min_confidence_score: float,
        reasons: list[str],
    ) -> tuple[int, bool]:
        """Condition 5: DataConfidenceScore >= min_confidence_score.

        Returns ``(score, passed)`` tuple.
        Score defaults to 0 when ``confidenceScore`` is absent.
        """
        raw = data.get("confidenceScore", 0)
        try:
            score = max(0, min(int(float(raw)), _SCORE_MAX))
        except (TypeError, ValueError):
            score = 0

        # Hard block: score < 30 always fails regardless of min_confidence_score
        # (Requirement 7.11 is enforced here as defence-in-depth)
        effective_threshold = max(min_confidence_score, float(_GRADE_BLOCKED_THRESHOLD))
        passed = score >= effective_threshold

        if not passed:
            reasons.append(
                f"dataSemanticallyValid=False: confidenceScore={score} "
                f"< required {effective_threshold:.0f}"
            )
        return score, passed
