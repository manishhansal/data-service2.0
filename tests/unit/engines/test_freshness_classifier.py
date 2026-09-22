"""
tests/unit/engines/test_freshness_classifier.py

Unit tests for FreshnessClassifier and FreshnessStatus.

Covers:
  FreshnessClassifier.age_seconds / age_seconds_from_ms:
    - recent timestamp → age near 0
    - past timestamp → positive age
    - future timestamp → 0 (clamped)

  FreshnessClassifier.classify_live (Requirements 7.3, 7.4):
    - REGULAR session, all four tiers: at-threshold → FRESH
    - REGULAR session, all four tiers: just-over-threshold → STALE
    - REGULAR session: beyond stale window → EXPIRED
    - Non-REGULAR session: extended windows applied
    - Unknown instrument_tier falls back to EQUITY thresholds
    - case-insensitive instrument_tier input

  FreshnessClassifier.classify (Task 9.3, interval-based):
    - India 1m / 5m / 10m / 15m / 30m / 1h / 1d: FRESH at-threshold
    - India: just-over FRESH → STALE
    - India: past STALE → EXPIRED
    - Crypto 1m / 5m / 1h: FRESH threshold is 1.5× the India threshold
    - Crypto 3m: allowed (not banned)
    - India 3m: raises ValueError (permanently banned)
    - Unknown market → UNKNOWN
    - Unknown interval → UNKNOWN
    - case-insensitive market and interval inputs

  QualityEngine.classify_freshness:
    - delegates correctly to FreshnessClassifier.classify_live
    - returns FreshnessStatus, not a plain string

Requirements: 7.3, 7.4, 1.5
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from src.engines.quality_engine import FreshnessClassifier, FreshnessStatus, QualityEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _past(seconds: float) -> datetime:
    """Return a UTC datetime that is *seconds* in the past."""
    return _now_utc() - timedelta(seconds=seconds)


def _future(seconds: float) -> datetime:
    """Return a UTC datetime that is *seconds* in the future."""
    return _now_utc() + timedelta(seconds=seconds)


def _ms(seconds: float) -> int:
    """Convert seconds-ago to a UTC epoch-millisecond timestamp."""
    return int((time.time() - seconds) * 1000)


# ---------------------------------------------------------------------------
# age_seconds
# ---------------------------------------------------------------------------


class TestAgeSeconds:
    def test_recent_timestamp_is_near_zero(self) -> None:
        ts = _past(0.001)
        age = FreshnessClassifier.age_seconds(ts)
        assert 0.0 <= age <= 1.0, f"Expected age near 0, got {age}"

    def test_past_timestamp_returns_positive_age(self) -> None:
        ts = _past(30.0)
        age = FreshnessClassifier.age_seconds(ts)
        # Allow ±2s tolerance for slow test runners
        assert 28.0 <= age <= 32.0, f"Expected ~30s, got {age}"

    def test_future_timestamp_clamped_to_zero(self) -> None:
        ts = _future(10.0)
        age = FreshnessClassifier.age_seconds(ts)
        assert age == 0.0, f"Future timestamp should return 0.0, got {age}"

    def test_naive_datetime_treated_as_utc(self) -> None:
        # Naive datetime 60s in the past (interpreted as UTC)
        import datetime as _dt
        ts = _dt.datetime.now(_dt.UTC).replace(tzinfo=None) - timedelta(seconds=60)
        age = FreshnessClassifier.age_seconds(ts)
        assert 58.0 <= age <= 62.0, f"Expected ~60s, got {age}"

    def test_timezone_aware_datetime_converted(self) -> None:
        ts = _past(120.0)
        age = FreshnessClassifier.age_seconds(ts)
        assert 118.0 <= age <= 122.0


# ---------------------------------------------------------------------------
# age_seconds_from_ms
# ---------------------------------------------------------------------------


class TestAgeSecondsFromMs:
    def test_current_ms_gives_near_zero(self) -> None:
        now_ms = int(time.time() * 1000)
        age = FreshnessClassifier.age_seconds_from_ms(now_ms)
        assert 0.0 <= age <= 1.0

    def test_past_ms_gives_correct_age(self) -> None:
        ms = _ms(45.0)
        age = FreshnessClassifier.age_seconds_from_ms(ms)
        assert 43.0 <= age <= 47.0

    def test_future_ms_clamped_to_zero(self) -> None:
        future_ms = int((time.time() + 100) * 1000)
        age = FreshnessClassifier.age_seconds_from_ms(future_ms)
        assert age == 0.0


# ---------------------------------------------------------------------------
# classify_live — REGULAR session (Requirement 7.3)
# ---------------------------------------------------------------------------


class TestClassifyLiveRegularSession:
    """Live-quote freshness during the NSE REGULAR session."""

    # --- INDEX ---

    def test_index_at_fresh_threshold_is_fresh(self) -> None:
        # age ≈ 9s, well within the 10s fresh window
        ms = _ms(9.0)
        status = FreshnessClassifier.classify_live(ms, "INDEX", is_regular_session=True)
        assert status == FreshnessStatus.FRESH

    def test_index_just_over_fresh_threshold_is_stale(self) -> None:
        # age ≈ 11s, clearly beyond the 10s fresh window
        ms = _ms(11.0)
        status = FreshnessClassifier.classify_live(ms, "INDEX", is_regular_session=True)
        assert status == FreshnessStatus.STALE

    def test_index_expired(self) -> None:
        # age ≈ 65s, beyond stale window (60s) → EXPIRED
        ms = _ms(65.0)
        status = FreshnessClassifier.classify_live(ms, "INDEX", is_regular_session=True)
        assert status == FreshnessStatus.EXPIRED

    # --- FO_LIQUID ---

    def test_fo_liquid_at_fresh_threshold_is_fresh(self) -> None:
        ms = _ms(9.0)
        status = FreshnessClassifier.classify_live(ms, "FO_LIQUID", is_regular_session=True)
        assert status == FreshnessStatus.FRESH

    def test_fo_liquid_just_over_fresh_is_stale(self) -> None:
        ms = _ms(11.0)
        status = FreshnessClassifier.classify_live(ms, "FO_LIQUID", is_regular_session=True)
        assert status == FreshnessStatus.STALE

    def test_fo_liquid_expired(self) -> None:
        ms = _ms(65.0)
        status = FreshnessClassifier.classify_live(ms, "FO_LIQUID", is_regular_session=True)
        assert status == FreshnessStatus.EXPIRED

    # --- FO_NORMAL ---

    def test_fo_normal_at_fresh_threshold_is_fresh(self) -> None:
        # age ≈ 14s, within the 15s fresh window
        ms = _ms(14.0)
        status = FreshnessClassifier.classify_live(ms, "FO_NORMAL", is_regular_session=True)
        assert status == FreshnessStatus.FRESH

    def test_fo_normal_just_over_fresh_is_stale(self) -> None:
        ms = _ms(16.0)
        status = FreshnessClassifier.classify_live(ms, "FO_NORMAL", is_regular_session=True)
        assert status == FreshnessStatus.STALE

    def test_fo_normal_expired(self) -> None:
        # stale window = 90s → 95s is EXPIRED
        ms = _ms(95.0)
        status = FreshnessClassifier.classify_live(ms, "FO_NORMAL", is_regular_session=True)
        assert status == FreshnessStatus.EXPIRED

    # --- EQUITY ---

    def test_equity_at_fresh_threshold_is_fresh(self) -> None:
        # age ≈ 29s, within the 30s fresh window
        ms = _ms(29.0)
        status = FreshnessClassifier.classify_live(ms, "EQUITY", is_regular_session=True)
        assert status == FreshnessStatus.FRESH

    def test_equity_just_over_fresh_is_stale(self) -> None:
        ms = _ms(31.0)
        status = FreshnessClassifier.classify_live(ms, "EQUITY", is_regular_session=True)
        assert status == FreshnessStatus.STALE

    def test_equity_expired(self) -> None:
        # stale window = 120s → 125s is EXPIRED
        ms = _ms(125.0)
        status = FreshnessClassifier.classify_live(ms, "EQUITY", is_regular_session=True)
        assert status == FreshnessStatus.EXPIRED

    # --- Very fresh ---

    def test_very_fresh_data_is_always_fresh(self) -> None:
        ms = _ms(1.0)
        for tier in ("INDEX", "FO_LIQUID", "FO_NORMAL", "EQUITY"):
            status = FreshnessClassifier.classify_live(ms, tier, is_regular_session=True)
            assert status == FreshnessStatus.FRESH, f"Tier {tier}: expected FRESH"


# ---------------------------------------------------------------------------
# classify_live — non-REGULAR session (Requirement 7.4)
# ---------------------------------------------------------------------------


class TestClassifyLiveNonRegularSession:
    """Live-quote freshness during non-REGULAR NSE sessions (extended windows)."""

    def test_index_at_extended_fresh_threshold(self) -> None:
        # age ≈ 59s, within the 60s extended fresh window
        ms = _ms(59.0)
        status = FreshnessClassifier.classify_live(ms, "INDEX", is_regular_session=False)
        assert status == FreshnessStatus.FRESH

    def test_index_just_over_extended_fresh_is_stale(self) -> None:
        ms = _ms(61.0)
        status = FreshnessClassifier.classify_live(ms, "INDEX", is_regular_session=False)
        assert status == FreshnessStatus.STALE

    def test_fo_liquid_extended_fresh(self) -> None:
        ms = _ms(59.0)
        status = FreshnessClassifier.classify_live(ms, "FO_LIQUID", is_regular_session=False)
        assert status == FreshnessStatus.FRESH

    def test_fo_normal_extended_fresh_threshold(self) -> None:
        # Extended FRESH for FO_NORMAL = 90s; use 89s to stay safely inside
        ms = _ms(89.0)
        status = FreshnessClassifier.classify_live(ms, "FO_NORMAL", is_regular_session=False)
        assert status == FreshnessStatus.FRESH

    def test_fo_normal_extended_just_over_fresh(self) -> None:
        ms = _ms(91.0)
        status = FreshnessClassifier.classify_live(ms, "FO_NORMAL", is_regular_session=False)
        assert status == FreshnessStatus.STALE

    def test_equity_extended_fresh_threshold(self) -> None:
        # Extended FRESH for EQUITY = 120s; use 119s to stay safely inside
        ms = _ms(119.0)
        status = FreshnessClassifier.classify_live(ms, "EQUITY", is_regular_session=False)
        assert status == FreshnessStatus.FRESH

    def test_equity_extended_just_over_fresh(self) -> None:
        ms = _ms(121.0)
        status = FreshnessClassifier.classify_live(ms, "EQUITY", is_regular_session=False)
        assert status == FreshnessStatus.STALE

    def test_extended_windows_wider_than_regular(self) -> None:
        """At age=50s, non-REGULAR session gives FRESH for INDEX; REGULAR gives STALE."""
        ms = _ms(50.0)
        regular_status = FreshnessClassifier.classify_live(ms, "INDEX", is_regular_session=True)
        non_regular_status = FreshnessClassifier.classify_live(ms, "INDEX", is_regular_session=False)
        # REGULAR: age=50s is beyond the 10s fresh window → STALE
        # Non-REGULAR: age=50s is within the 60s fresh window → FRESH
        assert regular_status == FreshnessStatus.STALE
        assert non_regular_status == FreshnessStatus.FRESH


# ---------------------------------------------------------------------------
# classify_live — edge cases
# ---------------------------------------------------------------------------


class TestClassifyLiveEdgeCases:
    def test_unknown_tier_falls_back_to_equity(self) -> None:
        # Unknown tier uses EQUITY thresholds (30s FRESH in REGULAR)
        ms = _ms(20.0)
        status = FreshnessClassifier.classify_live(ms, "UNKNOWN_TIER", is_regular_session=True)
        assert status == FreshnessStatus.FRESH  # within EQUITY 30s window

    def test_unknown_tier_beyond_equity_fresh(self) -> None:
        ms = _ms(31.0)
        status = FreshnessClassifier.classify_live(ms, "UNKNOWN_TIER", is_regular_session=True)
        assert status == FreshnessStatus.STALE

    def test_instrument_tier_case_insensitive(self) -> None:
        ms = _ms(5.0)
        for variant in ("index", "INDEX", "Index", "InDeX"):
            status = FreshnessClassifier.classify_live(ms, variant, is_regular_session=True)
            assert status == FreshnessStatus.FRESH, f"Variant '{variant}' failed"


# ---------------------------------------------------------------------------
# classify — India interval-based thresholds (Task 9.3)
# ---------------------------------------------------------------------------


class TestClassifyIndiaIntervals:
    """India interval-based candle freshness."""

    # --- 1m ---

    def test_india_1m_fresh_at_threshold(self) -> None:
        ts = _past(119.0)  # 1s inside the 120s (2 min) fresh threshold
        assert FreshnessClassifier.classify(ts, "india", "1m") == FreshnessStatus.FRESH

    def test_india_1m_stale_just_over_fresh(self) -> None:
        ts = _past(121.0)
        assert FreshnessClassifier.classify(ts, "india", "1m") == FreshnessStatus.STALE

    def test_india_1m_expired(self) -> None:
        ts = _past(605.0)  # > 10 min
        assert FreshnessClassifier.classify(ts, "india", "1m") == FreshnessStatus.EXPIRED

    # --- 5m ---

    def test_india_5m_fresh_at_threshold(self) -> None:
        ts = _past(419.0)  # 1s inside the 420s (7 min) fresh threshold
        assert FreshnessClassifier.classify(ts, "india", "5m") == FreshnessStatus.FRESH

    def test_india_5m_stale_just_over_fresh(self) -> None:
        ts = _past(421.0)
        assert FreshnessClassifier.classify(ts, "india", "5m") == FreshnessStatus.STALE

    def test_india_5m_expired(self) -> None:
        ts = _past(1805.0)  # > 30 min
        assert FreshnessClassifier.classify(ts, "india", "5m") == FreshnessStatus.EXPIRED

    # --- 15m ---

    def test_india_15m_fresh_at_threshold(self) -> None:
        ts = _past(1199.0)  # 1s inside the 1200s (20 min) fresh threshold
        assert FreshnessClassifier.classify(ts, "india", "15m") == FreshnessStatus.FRESH

    def test_india_15m_stale_just_over_fresh(self) -> None:
        ts = _past(1201.0)
        assert FreshnessClassifier.classify(ts, "india", "15m") == FreshnessStatus.STALE

    def test_india_15m_expired(self) -> None:
        ts = _past(3605.0)  # > 60 min
        assert FreshnessClassifier.classify(ts, "india", "15m") == FreshnessStatus.EXPIRED

    # --- 30m ---

    def test_india_30m_fresh_at_threshold(self) -> None:
        ts = _past(2399.0)  # 1s inside the 2400s (40 min) fresh threshold
        assert FreshnessClassifier.classify(ts, "india", "30m") == FreshnessStatus.FRESH

    def test_india_30m_stale_just_over_fresh(self) -> None:
        ts = _past(2401.0)
        assert FreshnessClassifier.classify(ts, "india", "30m") == FreshnessStatus.STALE

    def test_india_30m_expired(self) -> None:
        ts = _past(5405.0)  # > 90 min
        assert FreshnessClassifier.classify(ts, "india", "30m") == FreshnessStatus.EXPIRED

    # --- 1h ---

    def test_india_1h_fresh_at_threshold(self) -> None:
        ts = _past(4199.0)  # 1s inside the 4200s (70 min) fresh threshold
        assert FreshnessClassifier.classify(ts, "india", "1h") == FreshnessStatus.FRESH

    def test_india_1h_stale_just_over_fresh(self) -> None:
        ts = _past(4201.0)
        assert FreshnessClassifier.classify(ts, "india", "1h") == FreshnessStatus.STALE

    def test_india_1h_expired(self) -> None:
        ts = _past(10805.0)  # > 3h
        assert FreshnessClassifier.classify(ts, "india", "1h") == FreshnessStatus.EXPIRED

    # --- 1d ---

    def test_india_1d_fresh_at_threshold(self) -> None:
        ts = _past(93599.0)  # 1s inside the 93600s (26h) fresh threshold
        assert FreshnessClassifier.classify(ts, "india", "1d") == FreshnessStatus.FRESH

    def test_india_1d_stale_just_over_fresh(self) -> None:
        ts = _past(93601.0)
        assert FreshnessClassifier.classify(ts, "india", "1d") == FreshnessStatus.STALE

    def test_india_1d_expired(self) -> None:
        ts = _past(172805.0)  # > 48h
        assert FreshnessClassifier.classify(ts, "india", "1d") == FreshnessStatus.EXPIRED

    # --- very fresh data ---

    def test_very_recent_data_is_always_fresh(self) -> None:
        ts = _past(1.0)  # 1 second old
        for interval in ("1m", "5m", "10m", "15m", "30m", "1h", "1d"):
            result = FreshnessClassifier.classify(ts, "india", interval)
            assert result == FreshnessStatus.FRESH, f"interval={interval}"


# ---------------------------------------------------------------------------
# classify — 3m interval ban for India
# ---------------------------------------------------------------------------


class TestClassify3mBan:
    def test_india_3m_raises_value_error(self) -> None:
        ts = _past(1.0)
        with pytest.raises(ValueError, match="3m"):
            FreshnessClassifier.classify(ts, "india", "3m")

    def test_india_3m_raises_regardless_of_case(self) -> None:
        ts = _past(1.0)
        with pytest.raises(ValueError):
            FreshnessClassifier.classify(ts, "India", "3M")

    def test_crypto_3m_does_not_raise(self) -> None:
        ts = _past(1.0)
        # Should not raise — crypto supports 3m
        result = FreshnessClassifier.classify(ts, "crypto", "3m")
        assert result == FreshnessStatus.FRESH

    def test_crypto_3m_stale(self) -> None:
        ts = _past(182.0)  # > 3 min (crypto 3m fresh threshold = 180s)
        result = FreshnessClassifier.classify(ts, "crypto", "3m")
        assert result == FreshnessStatus.STALE


# ---------------------------------------------------------------------------
# classify — Crypto: thresholds are 1.5× India baseline
# ---------------------------------------------------------------------------


class TestClassifyCryptoMultiplier:
    def test_crypto_1m_fresh_threshold_is_1_5x_india(self) -> None:
        # India 1m fresh = 120s → Crypto 1m fresh = 180s
        ts_india_inside = _past(119.0)   # safely inside India threshold
        ts_crypto_inside = _past(179.0)  # safely inside crypto threshold

        india_status = FreshnessClassifier.classify(ts_india_inside, "india", "1m")
        crypto_status = FreshnessClassifier.classify(ts_crypto_inside, "crypto", "1m")

        assert india_status == FreshnessStatus.FRESH
        assert crypto_status == FreshnessStatus.FRESH

    def test_crypto_1m_just_over_india_fresh_still_fresh(self) -> None:
        # 121s: stale in India (>120s), but fresh in crypto (threshold is 180s)
        ts = _past(121.0)
        india_status = FreshnessClassifier.classify(ts, "india", "1m")
        crypto_status = FreshnessClassifier.classify(ts, "crypto", "1m")
        assert india_status == FreshnessStatus.STALE
        assert crypto_status == FreshnessStatus.FRESH

    def test_crypto_5m_fresh_threshold_is_1_5x_india(self) -> None:
        # India 5m fresh = 420s → Crypto 5m fresh = 630s; use 629s to stay inside
        ts = _past(629.0)
        result = FreshnessClassifier.classify(ts, "crypto", "5m")
        assert result == FreshnessStatus.FRESH

    def test_crypto_1h_fresh_threshold_is_1_5x_india(self) -> None:
        # India 1h fresh = 4200s → Crypto 1h fresh = 6300s; use 6299s to stay inside
        ts = _past(6299.0)
        result = FreshnessClassifier.classify(ts, "crypto", "1h")
        assert result == FreshnessStatus.FRESH

    def test_crypto_1h_just_over_threshold_is_stale(self) -> None:
        ts = _past(6301.0)
        result = FreshnessClassifier.classify(ts, "crypto", "1h")
        assert result == FreshnessStatus.STALE


# ---------------------------------------------------------------------------
# classify — Crypto-only extra intervals
# ---------------------------------------------------------------------------


class TestClassifyCryptoExtraIntervals:
    def test_crypto_2h_is_supported(self) -> None:
        ts = _past(1.0)
        result = FreshnessClassifier.classify(ts, "crypto", "2h")
        assert result == FreshnessStatus.FRESH

    def test_crypto_4h_is_supported(self) -> None:
        ts = _past(1.0)
        result = FreshnessClassifier.classify(ts, "crypto", "4h")
        assert result == FreshnessStatus.FRESH

    def test_crypto_12h_is_supported(self) -> None:
        ts = _past(1.0)
        result = FreshnessClassifier.classify(ts, "crypto", "12h")
        assert result == FreshnessStatus.FRESH


# ---------------------------------------------------------------------------
# classify — unknown market / interval → UNKNOWN
# ---------------------------------------------------------------------------


class TestClassifyUnknown:
    def test_unknown_market_returns_unknown(self) -> None:
        ts = _past(10.0)
        result = FreshnessClassifier.classify(ts, "forex", "1h")
        assert result == FreshnessStatus.UNKNOWN

    def test_unknown_interval_returns_unknown(self) -> None:
        ts = _past(10.0)
        result = FreshnessClassifier.classify(ts, "india", "3h")
        assert result == FreshnessStatus.UNKNOWN

    def test_unknown_crypto_interval_returns_unknown(self) -> None:
        ts = _past(10.0)
        result = FreshnessClassifier.classify(ts, "crypto", "45m")
        assert result == FreshnessStatus.UNKNOWN


# ---------------------------------------------------------------------------
# classify — case-insensitive inputs
# ---------------------------------------------------------------------------


class TestClassifyCaseInsensitive:
    def test_market_case_insensitive(self) -> None:
        ts = _past(1.0)
        for market_variant in ("india", "INDIA", "India"):
            result = FreshnessClassifier.classify(ts, market_variant, "1m")
            assert result == FreshnessStatus.FRESH, f"market='{market_variant}'"

    def test_interval_case_insensitive(self) -> None:
        ts = _past(1.0)
        for interval_variant in ("1m", "1M"):
            result = FreshnessClassifier.classify(ts, "india", interval_variant)
            assert result == FreshnessStatus.FRESH, f"interval='{interval_variant}'"

    def test_crypto_case_insensitive(self) -> None:
        ts = _past(1.0)
        for market_variant in ("crypto", "CRYPTO", "Crypto"):
            result = FreshnessClassifier.classify(ts, market_variant, "1h")
            assert result == FreshnessStatus.FRESH, f"market='{market_variant}'"


# ---------------------------------------------------------------------------
# FreshnessStatus enum
# ---------------------------------------------------------------------------


class TestFreshnessStatusEnum:
    def test_all_statuses_exist(self) -> None:
        assert FreshnessStatus.FRESH == "FRESH"
        assert FreshnessStatus.AGING == "AGING"
        assert FreshnessStatus.STALE == "STALE"
        assert FreshnessStatus.EXPIRED == "EXPIRED"
        assert FreshnessStatus.UNKNOWN == "UNKNOWN"

    def test_is_string_enum(self) -> None:
        assert isinstance(FreshnessStatus.FRESH, str)


# ---------------------------------------------------------------------------
# QualityEngine.classify_freshness — integration with FreshnessClassifier
# ---------------------------------------------------------------------------


class TestQualityEngineClassifyFreshness:
    """classify_freshness on QualityEngine delegates to FreshnessClassifier.classify_live."""

    _engine = QualityEngine()

    def test_returns_freshness_status_instance(self) -> None:
        ms = _ms(1.0)
        result = self._engine.classify_freshness(ms, "INDEX", is_regular_session=True)
        assert isinstance(result, FreshnessStatus)

    def test_fresh_index_during_regular(self) -> None:
        ms = _ms(5.0)
        result = self._engine.classify_freshness(ms, "INDEX", is_regular_session=True)
        assert result == FreshnessStatus.FRESH

    def test_stale_equity_during_regular(self) -> None:
        ms = _ms(35.0)
        result = self._engine.classify_freshness(ms, "EQUITY", is_regular_session=True)
        assert result == FreshnessStatus.STALE

    def test_expired_data_during_regular(self) -> None:
        ms = _ms(200.0)
        result = self._engine.classify_freshness(ms, "EQUITY", is_regular_session=True)
        assert result == FreshnessStatus.EXPIRED

    def test_fresh_index_during_non_regular(self) -> None:
        ms = _ms(55.0)
        result = self._engine.classify_freshness(ms, "INDEX", is_regular_session=False)
        assert result == FreshnessStatus.FRESH
