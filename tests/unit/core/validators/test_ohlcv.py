"""
Unit tests for src/core/validators/ohlcv.py

Covers:
- Happy path: all invariants satisfied → (True, None)
- Each invariant failure separately: high < max(open, close), etc.
- Multiple simultaneous invariant failures
- Strict > 0 price checks for all four price fields
- volume == 0 is valid (genuine zero-volume bar, requirement 4.4)
- volume < 0 is invalid
- Missing required price fields → False + incident
- Incident fields: incidentType, instrumentId, failedInvariant, rejectedValues, detectedAt
- 3m interval hard block for Indian market → raises ValueError
- 3m NOT blocked for Binance crypto (is_indian_market=False)
- CANONICAL_INDIAN_TIMEFRAMES: all valid; 3m absent
- All canonical timeframes pass the validator (happy path)
- Numeric type coercion: string prices are accepted

Requirements: 4.3, 1.5, 13.8, 17.1
"""

from __future__ import annotations

import pytest

from src.core.validators.ohlcv import (
    CANONICAL_INDIAN_TIMEFRAMES,
    validate_ohlcv_invariants,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_candle(**overrides: object) -> dict:
    """Return a minimally valid candle dict."""
    base: dict = {
        "open":   100.0,
        "high":   105.0,
        "low":     95.0,
        "close":  102.0,
        "volume": 1000,
        "time":   1705300000000,
    }
    base.update(overrides)
    return base


# ===========================================================================
# 3m interval hard block
# ===========================================================================


class TestThreeMintervalBlock:
    def test_3m_raises_value_error_for_indian_market(self) -> None:
        """Requirement 1.5, 4.2, 10.11 — 3m is permanently unsupported."""
        with pytest.raises(ValueError, match="3m is permanently unsupported"):
            validate_ohlcv_invariants(
                _valid_candle(),
                provider="angel_one",
                instrument_id="NSE:NIFTY:IDX",
                interval="3m",
                is_indian_market=True,
            )

    def test_3m_default_is_indian_market(self) -> None:
        """is_indian_market=True by default."""
        with pytest.raises(ValueError, match="3m is permanently unsupported"):
            validate_ohlcv_invariants(
                _valid_candle(),
                provider="angel_one",
                instrument_id="NSE:NIFTY:IDX",
                interval="3m",
            )

    def test_3m_allowed_for_binance_crypto(self) -> None:
        """Requirement 13.1 — 3m is valid for Binance crypto only."""
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(),
            provider="binance",
            instrument_id="CRYPTO:BTCUSDT",
            interval="3m",
            is_indian_market=False,
        )
        assert ok is True
        assert incident is None

    def test_3m_raises_before_any_validation_logic(self) -> None:
        """Even with an otherwise invalid candle, 3m raises first."""
        bad_candle = _valid_candle(high=50.0)  # high < open → invariant fail
        with pytest.raises(ValueError):
            validate_ohlcv_invariants(
                bad_candle,
                provider="angel_one",
                instrument_id="NSE:X",
                interval="3m",
            )


# ===========================================================================
# CANONICAL_INDIAN_TIMEFRAMES
# ===========================================================================


class TestCanonicalTimeframes:
    def test_3m_absent_from_canonical_timeframes(self) -> None:
        assert "3m" not in CANONICAL_INDIAN_TIMEFRAMES

    def test_expected_intervals_present(self) -> None:
        for interval in ("1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"):
            assert interval in CANONICAL_INDIAN_TIMEFRAMES

    def test_all_canonical_timeframes_pass_validator(self) -> None:
        for interval in CANONICAL_INDIAN_TIMEFRAMES:
            ok, incident = validate_ohlcv_invariants(
                _valid_candle(),
                provider="angel_one",
                instrument_id="NSE:NIFTY:IDX",
                interval=interval,
            )
            assert ok is True, f"Expected pass for interval={interval}, got failure"
            assert incident is None


# ===========================================================================
# Happy path
# ===========================================================================


class TestHappyPath:
    def test_valid_candle_passes(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(),
            provider="angel_one",
            instrument_id="NSE:NIFTY:IDX",
            interval="1m",
        )
        assert ok is True
        assert incident is None

    def test_doji_candle_open_equals_close(self) -> None:
        """Doji: open == close; high must be >= both (which equals both)."""
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open=100.0, high=105.0, low=95.0, close=100.0),
            provider="angel_one",
            instrument_id="NSE:X",
            interval="5m",
        )
        assert ok is True
        assert incident is None

    def test_high_equals_close(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open=100.0, high=102.0, low=95.0, close=102.0),
            provider="angel_one",
            instrument_id="NSE:X",
            interval="1d",
        )
        assert ok is True

    def test_low_equals_open(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open=95.0, high=105.0, low=95.0, close=102.0),
            provider="angel_one",
            instrument_id="NSE:X",
            interval="15m",
        )
        assert ok is True

    def test_zero_volume_valid(self) -> None:
        """Zero volume is a valid genuine zero-volume bar. Requirement 4.4."""
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(volume=0),
            provider="openchart",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is True
        assert incident is None

    def test_none_volume_skips_volume_check(self) -> None:
        """volume=None means volumeUnavailable; skip volume invariant."""
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(volume=None),
            provider="openchart",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is True
        assert incident is None

    def test_string_prices_accepted(self) -> None:
        """Provider may supply prices as strings; coercion to float is applied."""
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open="100.0", high="105.0", low="95.0", close="102.0"),
            provider="openchart",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is True
        assert incident is None


# ===========================================================================
# Invariant: high >= max(open, close)
# ===========================================================================


class TestHighInvariant:
    def test_high_less_than_close_fails(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open=100.0, high=101.0, low=95.0, close=103.0),
            provider="angel_one",
            instrument_id="NSE:NIFTY:IDX",
            interval="1m",
        )
        assert ok is False
        assert incident is not None
        assert "high_ge_max_open_close" in str(incident["details"]["failedInvariants"])

    def test_high_less_than_open_fails(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open=104.0, high=101.0, low=95.0, close=102.0),
            provider="angel_one",
            instrument_id="NSE:NIFTY:IDX",
            interval="5m",
        )
        assert ok is False
        assert incident is not None
        assert "high_ge_max_open_close" in str(incident["details"]["failedInvariants"])

    def test_high_exactly_equals_max_passes(self) -> None:
        ok, _ = validate_ohlcv_invariants(
            _valid_candle(open=100.0, high=103.0, low=95.0, close=103.0),
            provider="angel_one",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is True


# ===========================================================================
# Invariant: low <= min(open, close)
# ===========================================================================


class TestLowInvariant:
    def test_low_greater_than_open_fails(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open=100.0, high=105.0, low=101.0, close=103.0),
            provider="angel_one",
            instrument_id="NSE:NIFTY:IDX",
            interval="1m",
        )
        assert ok is False
        assert incident is not None
        assert "low_le_min_open_close" in str(incident["details"]["failedInvariants"])

    def test_low_greater_than_close_fails(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open=105.0, high=110.0, low=103.0, close=100.0),
            provider="angel_one",
            instrument_id="NSE:X",
            interval="15m",
        )
        assert ok is False
        assert "low_le_min_open_close" in str(incident["details"]["failedInvariants"])

    def test_low_exactly_equals_min_passes(self) -> None:
        ok, _ = validate_ohlcv_invariants(
            _valid_candle(open=100.0, high=105.0, low=100.0, close=103.0),
            provider="angel_one",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is True


# ===========================================================================
# Invariant: volume >= 0
# ===========================================================================


class TestVolumeInvariant:
    def test_negative_volume_fails(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(volume=-1),
            provider="openchart",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is False
        assert incident is not None
        assert "volume_non_negative" in str(incident["details"]["failedInvariants"])

    def test_large_negative_volume_fails(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(volume=-999999),
            provider="openchart",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is False
        assert "volume_non_negative" in str(incident["details"]["failedInvariants"])


# ===========================================================================
# Invariant: all price values > 0
# ===========================================================================


class TestPricePositiveInvariant:
    @pytest.mark.parametrize("field", ["open", "high", "low", "close"])
    def test_zero_price_fails(self, field: str) -> None:
        candle = _valid_candle(**{field: 0.0})
        ok, incident = validate_ohlcv_invariants(
            candle,
            provider="angel_one",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is False
        assert incident is not None

    @pytest.mark.parametrize("field", ["open", "high", "low", "close"])
    def test_negative_price_fails(self, field: str) -> None:
        candle = _valid_candle(**{field: -1.0})
        ok, incident = validate_ohlcv_invariants(
            candle,
            provider="angel_one",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is False
        assert incident is not None


# ===========================================================================
# Multiple simultaneous invariant failures
# ===========================================================================


class TestMultipleFailures:
    def test_both_high_and_low_violated(self) -> None:
        """high too low AND low too high — both should be reported."""
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open=100.0, high=99.0, low=101.0, close=100.0),
            provider="angel_one",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is False
        assert incident is not None
        violations = incident["details"]["failedInvariants"]
        assert "high_ge_max_open_close" in violations
        assert "low_le_min_open_close" in violations

    def test_price_negative_and_volume_negative(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(open=-5.0, high=105.0, low=95.0, close=102.0, volume=-10),
            provider="angel_one",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is False
        assert "open_gt_zero" in str(incident["details"]["failedInvariants"])


# ===========================================================================
# Missing required fields
# ===========================================================================


class TestMissingFields:
    def test_missing_open_fails(self) -> None:
        candle = {k: v for k, v in _valid_candle().items() if k != "open"}
        ok, incident = validate_ohlcv_invariants(
            candle, provider="angel_one", instrument_id="NSE:X", interval="1m"
        )
        assert ok is False
        assert incident is not None
        assert "missing_field:open" in str(incident["details"]["failedInvariants"])

    def test_missing_all_prices_fails(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            {"volume": 100, "time": 123},
            provider="angel_one",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is False
        assert incident is not None


# ===========================================================================
# DataIncident structure
# ===========================================================================


class TestIncidentStructure:
    def test_incident_fields_on_invariant_failure(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(high=50.0),  # high < open=100
            provider="angel_one",
            instrument_id="NSE:NIFTY:IDX",
            interval="5m",
        )
        assert ok is False
        assert incident is not None
        assert incident["incidentType"] == "OHLC_INVARIANT"
        assert incident["instrumentId"] == "NSE:NIFTY:IDX"
        assert incident["provider"] == "angel_one"
        assert "incidentId" in incident
        assert "timestamp" in incident
        details = incident["details"]
        assert "intervalStr" in details
        assert details["intervalStr"] == "5m"
        assert "rejectedValues" in details
        assert "detectedAt" in details
        assert "failedInvariants" in details

    def test_incident_contains_rejected_values(self) -> None:
        ok, incident = validate_ohlcv_invariants(
            _valid_candle(volume=-5),
            provider="openchart",
            instrument_id="NSE:X",
            interval="1m",
        )
        assert ok is False
        assert incident["details"]["rejectedValues"]["volume"] == -5.0

    def test_incident_has_unique_id(self) -> None:
        ok1, inc1 = validate_ohlcv_invariants(
            _valid_candle(high=50.0), "angel_one", "NSE:X", "1m"
        )
        ok2, inc2 = validate_ohlcv_invariants(
            _valid_candle(high=50.0), "angel_one", "NSE:X", "1m"
        )
        assert inc1 is not None and inc2 is not None
        assert inc1["incidentId"] != inc2["incidentId"]
