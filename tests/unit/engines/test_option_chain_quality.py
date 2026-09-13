"""
tests/unit/engines/test_option_chain_quality.py

Unit tests for QualityEngine.validate_option_chain_record (task 9.5).

Covers:
  - OptionChainRecord Pydantic v2 model field validation
  - OptionChainValidationResult structure
  - All nine validation rules independently
  - Multiple simultaneous violations accumulate in issues list
  - Null semantics: None is allowed; 0 is NOT a substitute (Requirements 6.2, 6.4)
  - Crossed-market detection (bid > ask)
  - Negative IV and zero IV rejection
  - Wide spread detection (>= 50% of ltp)
  - Expired contract detection
  - Valid record returns valid=True with empty issues

Requirements: 3.7, 3.8, 3.9, 3.10, 7.10
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from src.engines.quality_engine import (
    OptionChainRecord,
    OptionChainValidationResult,
    QualityEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_engine = QualityEngine()

# A future expiry date always ≥ today
_FUTURE_EXPIRY = (date.today() + timedelta(days=30)).isoformat()
_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
_TODAY = date.today().isoformat()
_VALID_TS = 1_700_000_000_000  # a fixed past-but-not-too-old epoch-ms value


def _valid_record(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid option chain row dict that passes all rules."""
    base: dict[str, Any] = {
        "symbol": "NIFTY",
        "expiry": _FUTURE_EXPIRY,
        "strike": 22_000.0,
        "optionType": "CE",
        "ltp": 150.0,
        "oi": 500_000.0,
        "volume": 12_345.0,
        "iv": 15.5,       # 15.5% — valid
        "bid": 148.0,
        "ask": 152.0,
        "timestamp": _VALID_TS,
    }
    base.update(overrides)
    return base


def _validate(record: dict[str, Any]) -> OptionChainValidationResult:
    return _engine.validate_option_chain_record(record)


# ---------------------------------------------------------------------------
# OptionChainRecord model
# ---------------------------------------------------------------------------


class TestOptionChainRecordModel:
    """Pydantic v2 model structure tests for OptionChainRecord."""

    def test_valid_record_parses(self) -> None:
        r = OptionChainRecord.model_validate(_valid_record())
        assert r.symbol == "NIFTY"
        assert r.strike == 22_000.0
        assert r.optionType == "CE"

    def test_frozen_model(self) -> None:
        r = OptionChainRecord.model_validate(_valid_record())
        with pytest.raises(Exception):
            r.symbol = "BANKNIFTY"  # type: ignore[misc]

    def test_nullable_fields_accept_none(self) -> None:
        """ltp, oi, volume, iv, bid, ask are all allowed to be None."""
        r = OptionChainRecord.model_validate(
            _valid_record(ltp=None, oi=None, volume=None, iv=None, bid=None, ask=None)
        )
        assert r.ltp is None
        assert r.oi is None
        assert r.volume is None
        assert r.iv is None
        assert r.bid is None
        assert r.ask is None

    def test_empty_symbol_raises(self) -> None:
        with pytest.raises(Exception):
            OptionChainRecord.model_validate(_valid_record(symbol=""))

    def test_whitespace_symbol_raises(self) -> None:
        with pytest.raises(Exception):
            OptionChainRecord.model_validate(_valid_record(symbol="   "))

    def test_non_positive_timestamp_raises(self) -> None:
        with pytest.raises(Exception):
            OptionChainRecord.model_validate(_valid_record(timestamp=0))
        with pytest.raises(Exception):
            OptionChainRecord.model_validate(_valid_record(timestamp=-1))


# ---------------------------------------------------------------------------
# OptionChainValidationResult model
# ---------------------------------------------------------------------------


class TestOptionChainValidationResultModel:
    """Structure tests for OptionChainValidationResult."""

    def test_valid_true_empty_issues(self) -> None:
        r = OptionChainValidationResult(valid=True, issues=[], record={})
        assert r.valid is True
        assert r.issues == []

    def test_invalid_false_with_issues(self) -> None:
        r = OptionChainValidationResult(
            valid=False, issues=["invalid_strike: strike=0"], record={}
        )
        assert r.valid is False
        assert len(r.issues) == 1

    def test_frozen_model(self) -> None:
        r = OptionChainValidationResult(valid=True, issues=[], record={})
        with pytest.raises(Exception):
            r.valid = False  # type: ignore[misc]

    def test_record_field_preserves_input(self) -> None:
        raw = _valid_record()
        result = _validate(raw)
        assert result.record == raw


# ---------------------------------------------------------------------------
# Valid record passes all checks
# ---------------------------------------------------------------------------


class TestValidRecord:
    def test_fully_valid_record_is_valid(self) -> None:
        result = _validate(_valid_record())
        assert result.valid is True
        assert result.issues == []

    def test_valid_pe_option(self) -> None:
        result = _validate(_valid_record(optionType="PE"))
        assert result.valid is True

    def test_none_optional_fields_still_valid(self) -> None:
        """Absent ltp/oi/volume/iv/bid/ask are allowed — not fabricated as 0."""
        result = _validate(
            _valid_record(ltp=None, oi=None, volume=None, iv=None, bid=None, ask=None)
        )
        assert result.valid is True

    def test_today_expiry_is_valid(self) -> None:
        """An option expiring today is still valid."""
        result = _validate(_valid_record(expiry=_TODAY))
        assert result.valid is True

    def test_ltp_zero_is_valid(self) -> None:
        """ltp=0 is allowed (deep OTM options can trade at 0.05 and round to 0)."""
        result = _validate(_valid_record(ltp=0.0))
        assert result.valid is True


# ---------------------------------------------------------------------------
# Rule 1: optionType must be CE or PE
# ---------------------------------------------------------------------------


class TestRule1OptionType:
    def test_ce_uppercase_passes(self) -> None:
        assert _validate(_valid_record(optionType="CE")).valid is True

    def test_pe_uppercase_passes(self) -> None:
        assert _validate(_valid_record(optionType="PE")).valid is True

    def test_ce_lowercase_passes(self) -> None:
        """Case-insensitive — 'ce' is accepted."""
        assert _validate(_valid_record(optionType="ce")).valid is True

    def test_pe_lowercase_passes(self) -> None:
        assert _validate(_valid_record(optionType="pe")).valid is True

    def test_invalid_option_type_fails(self) -> None:
        result = _validate(_valid_record(optionType="CALL"))
        assert result.valid is False
        assert any("option_type" in i.lower() or "optionType" in i for i in result.issues)

    def test_empty_option_type_fails(self) -> None:
        result = _validate(_valid_record(optionType=""))
        assert result.valid is False

    def test_numeric_option_type_fails(self) -> None:
        result = _validate(_valid_record(optionType="1"))
        assert result.valid is False


# ---------------------------------------------------------------------------
# Rule 2: strike > 0
# ---------------------------------------------------------------------------


class TestRule2Strike:
    def test_positive_strike_passes(self) -> None:
        assert _validate(_valid_record(strike=100.0)).valid is True

    def test_zero_strike_fails(self) -> None:
        result = _validate(_valid_record(strike=0.0))
        assert result.valid is False
        assert any("strike" in i for i in result.issues)

    def test_negative_strike_fails(self) -> None:
        result = _validate(_valid_record(strike=-500.0))
        assert result.valid is False
        assert any("strike" in i for i in result.issues)

    def test_very_small_positive_strike_passes(self) -> None:
        assert _validate(_valid_record(strike=0.01)).valid is True


# ---------------------------------------------------------------------------
# Rule 3: ltp >= 0 when present
# ---------------------------------------------------------------------------


class TestRule3Ltp:
    def test_positive_ltp_passes(self) -> None:
        assert _validate(_valid_record(ltp=250.0)).valid is True

    def test_zero_ltp_passes(self) -> None:
        """ltp=0 is allowed."""
        assert _validate(_valid_record(ltp=0.0)).valid is True

    def test_none_ltp_passes(self) -> None:
        """None ltp is allowed — not fabricated."""
        assert _validate(_valid_record(ltp=None, bid=None, ask=None)).valid is True

    def test_negative_ltp_fails(self) -> None:
        result = _validate(_valid_record(ltp=-1.0))
        assert result.valid is False
        assert any("ltp" in i for i in result.issues)


# ---------------------------------------------------------------------------
# Rule 4: oi >= 0 when present
# ---------------------------------------------------------------------------


class TestRule4Oi:
    def test_positive_oi_passes(self) -> None:
        assert _validate(_valid_record(oi=1_000.0)).valid is True

    def test_zero_oi_passes(self) -> None:
        """oi=0 is valid (no open interest)."""
        assert _validate(_valid_record(oi=0.0)).valid is True

    def test_none_oi_passes(self) -> None:
        """Absent oi is allowed — must not be fabricated from tradedValue."""
        assert _validate(_valid_record(oi=None)).valid is True

    def test_negative_oi_fails(self) -> None:
        result = _validate(_valid_record(oi=-100.0))
        assert result.valid is False
        assert any("oi" in i for i in result.issues)

    def test_issue_message_mentions_oi(self) -> None:
        result = _validate(_valid_record(oi=-50.0))
        assert any("oi" in i.lower() for i in result.issues)


# ---------------------------------------------------------------------------
# Rule 5: volume >= 0 when present
# ---------------------------------------------------------------------------


class TestRule5Volume:
    def test_positive_volume_passes(self) -> None:
        assert _validate(_valid_record(volume=5_000.0)).valid is True

    def test_zero_volume_passes(self) -> None:
        assert _validate(_valid_record(volume=0.0)).valid is True

    def test_none_volume_passes(self) -> None:
        assert _validate(_valid_record(volume=None)).valid is True

    def test_negative_volume_fails(self) -> None:
        result = _validate(_valid_record(volume=-1.0))
        assert result.valid is False
        assert any("volume" in i for i in result.issues)


# ---------------------------------------------------------------------------
# Rule 6: iv in (0, 500] when present — 0 is NOT a substitute for absent IV
# ---------------------------------------------------------------------------


class TestRule6Iv:
    def test_valid_iv_passes(self) -> None:
        assert _validate(_valid_record(iv=25.0)).valid is True

    def test_high_but_valid_iv_passes(self) -> None:
        """IV of 500% is the upper limit and still valid."""
        assert _validate(_valid_record(iv=500.0)).valid is True

    def test_none_iv_passes(self) -> None:
        """Absent IV is allowed — it is null, not zero."""
        assert _validate(_valid_record(iv=None)).valid is True

    def test_zero_iv_fails(self) -> None:
        """iv=0 is the classic placeholder zero — must be rejected (Requirement 6.4)."""
        result = _validate(_valid_record(iv=0.0))
        assert result.valid is False
        assert any("iv" in i.lower() for i in result.issues)
        # The issue should mention that zero is not a valid substitute
        assert any("zero" in i.lower() or "substitute" in i.lower() for i in result.issues)

    def test_negative_iv_fails(self) -> None:
        result = _validate(_valid_record(iv=-5.0))
        assert result.valid is False
        assert any("iv" in i.lower() for i in result.issues)

    def test_iv_above_500_fails(self) -> None:
        result = _validate(_valid_record(iv=501.0))
        assert result.valid is False
        assert any("iv" in i.lower() for i in result.issues)

    def test_iv_just_below_500_passes(self) -> None:
        assert _validate(_valid_record(iv=499.9)).valid is True

    def test_iv_exactly_500_passes(self) -> None:
        assert _validate(_valid_record(iv=500.0)).valid is True

    def test_very_small_positive_iv_passes(self) -> None:
        """A tiny IV (e.g. 0.01%) is technically valid — it is > 0."""
        assert _validate(_valid_record(iv=0.01)).valid is True


# ---------------------------------------------------------------------------
# Rule 7: bid >= 0 and ask >= bid when both present
# ---------------------------------------------------------------------------


class TestRule7BidAsk:
    def test_valid_bid_ask_passes(self) -> None:
        assert _validate(_valid_record(bid=148.0, ask=152.0)).valid is True

    def test_bid_equals_ask_passes(self) -> None:
        """Zero spread is unusual but not invalid by rule 7."""
        assert _validate(_valid_record(bid=150.0, ask=150.0, ltp=150.0)).valid is True

    def test_none_bid_and_ask_passes(self) -> None:
        """Both absent is allowed."""
        assert _validate(_valid_record(bid=None, ask=None)).valid is True

    def test_none_bid_only_passes(self) -> None:
        """bid absent, ask present — rule 7 only applies when BOTH present."""
        assert _validate(_valid_record(bid=None, ask=152.0)).valid is True

    def test_none_ask_only_passes(self) -> None:
        """ask absent, bid present — rule 7 only applies when BOTH present."""
        assert _validate(_valid_record(bid=148.0, ask=None)).valid is True

    def test_crossed_market_ask_less_than_bid_fails(self) -> None:
        """bid > ask — crossed market (Requirement 7.10)."""
        result = _validate(_valid_record(bid=155.0, ask=150.0))
        assert result.valid is False
        assert any("crossed" in i.lower() or "bid" in i for i in result.issues)

    def test_negative_bid_fails(self) -> None:
        result = _validate(_valid_record(bid=-1.0, ask=150.0))
        assert result.valid is False
        assert any("bid" in i for i in result.issues)

    def test_zero_bid_zero_ask_passes_rule7(self) -> None:
        """bid=0, ask=0: ask >= bid → rule 7 passes (spread check is separate)."""
        # Spread check rule 9 requires ltp > 0 to trigger, so set ltp=None to isolate
        assert _validate(_valid_record(bid=0.0, ask=0.0, ltp=None)).valid is True


# ---------------------------------------------------------------------------
# Rule 8: expiry is a valid YYYY-MM-DD date in the future or today
# ---------------------------------------------------------------------------


class TestRule8Expiry:
    def test_future_expiry_passes(self) -> None:
        assert _validate(_valid_record(expiry=_FUTURE_EXPIRY)).valid is True

    def test_today_expiry_passes(self) -> None:
        assert _validate(_valid_record(expiry=_TODAY)).valid is True

    def test_yesterday_expiry_fails(self) -> None:
        result = _validate(_valid_record(expiry=_YESTERDAY))
        assert result.valid is False
        assert any("expir" in i.lower() for i in result.issues)

    def test_invalid_date_format_fails(self) -> None:
        result = _validate(_valid_record(expiry="31-12-2030"))
        assert result.valid is False
        assert any("expiry" in i.lower() or "expir" in i.lower() for i in result.issues)

    def test_nonsense_expiry_fails(self) -> None:
        result = _validate(_valid_record(expiry="not-a-date"))
        assert result.valid is False

    def test_expiry_well_in_the_future_passes(self) -> None:
        far_future = (date.today() + timedelta(days=365)).isoformat()
        assert _validate(_valid_record(expiry=far_future)).valid is True


# ---------------------------------------------------------------------------
# Rule 9: bid-ask spread < 50% of ltp
# ---------------------------------------------------------------------------


class TestRule9SpreadQuality:
    def test_narrow_spread_passes(self) -> None:
        """Spread = 4 / ltp=150 ≈ 2.7% — well below 50%."""
        assert _validate(_valid_record(ltp=150.0, bid=148.0, ask=152.0)).valid is True

    def test_spread_exactly_50pct_fails(self) -> None:
        """Spread = 50 / ltp=100 = 50% — at the boundary (>= fails)."""
        result = _validate(_valid_record(ltp=100.0, bid=75.0, ask=125.0))
        assert result.valid is False
        assert any("spread" in i.lower() for i in result.issues)

    def test_spread_above_50pct_fails(self) -> None:
        """Spread = 80 / ltp=100 = 80% — clearly above threshold."""
        result = _validate(_valid_record(ltp=100.0, bid=60.0, ask=140.0))
        assert result.valid is False
        assert any("spread" in i.lower() for i in result.issues)

    def test_spread_just_below_50pct_passes(self) -> None:
        """Spread = 49 / ltp=100 = 49% — just under threshold."""
        assert _validate(_valid_record(ltp=100.0, bid=75.5, ask=124.5)).valid is True

    def test_rule9_not_triggered_when_ltp_zero(self) -> None:
        """Rule 9 requires ltp > 0; ltp=0 skips the spread check."""
        assert _validate(_valid_record(ltp=0.0, bid=0.0, ask=0.0)).valid is True

    def test_rule9_not_triggered_when_ltp_none(self) -> None:
        """Rule 9 requires ltp present; ltp=None skips the spread check."""
        assert _validate(_valid_record(ltp=None, bid=148.0, ask=152.0)).valid is True

    def test_rule9_not_triggered_when_bid_none(self) -> None:
        """Rule 9 requires bid > 0; bid=None skips the check."""
        assert _validate(_valid_record(ltp=100.0, bid=None, ask=152.0)).valid is True

    def test_rule9_not_triggered_when_ask_none(self) -> None:
        assert _validate(_valid_record(ltp=100.0, bid=148.0, ask=None)).valid is True

    def test_rule9_not_triggered_when_bid_zero(self) -> None:
        """bid=0 means no valid bid; skip rule 9."""
        assert _validate(_valid_record(ltp=100.0, bid=0.0, ask=50.0)).valid is True

    def test_rule9_not_triggered_when_ask_zero(self) -> None:
        """ask=0 with bid=0 — neither bid nor ask > 0, so rule 9 is skipped.
        Rule 7 is also satisfied because ask(0) >= bid(0)."""
        assert _validate(_valid_record(ltp=100.0, bid=0.0, ask=0.0)).valid is True


# ---------------------------------------------------------------------------
# Multiple simultaneous violations
# ---------------------------------------------------------------------------


class TestMultipleViolations:
    def test_two_violations_reported(self) -> None:
        """Negative strike and negative IV both reported in one call."""
        result = _validate(_valid_record(strike=-1.0, iv=-5.0))
        assert result.valid is False
        assert len(result.issues) >= 2

    def test_three_violations_reported(self) -> None:
        """Zero IV, negative OI, and crossed market all reported."""
        result = _validate(_valid_record(iv=0.0, oi=-10.0, bid=200.0, ask=100.0))
        assert result.valid is False
        assert len(result.issues) >= 3

    def test_all_optional_fields_invalid_accumulate(self) -> None:
        """All four optional numeric field violations at once."""
        result = _validate(_valid_record(ltp=-1.0, oi=-1.0, volume=-1.0, iv=0.0))
        assert result.valid is False
        issue_text = " ".join(result.issues)
        assert "ltp" in issue_text
        assert "oi" in issue_text.lower()
        assert "volume" in issue_text
        assert "iv" in issue_text.lower()

    def test_schema_failure_returns_result_not_exception(self) -> None:
        """A completely broken record must return a result, not raise."""
        result = _validate({"symbol": "X", "expiry": "bad", "strike": "NaN",
                             "optionType": "CE", "timestamp": 1_000_000_000_000})
        assert isinstance(result, OptionChainValidationResult)
        assert result.valid is False

    def test_issues_list_empty_only_when_valid(self) -> None:
        """issues must be empty when valid=True and non-empty when valid=False."""
        ok = _validate(_valid_record())
        assert ok.valid is True
        assert ok.issues == []

        bad = _validate(_valid_record(strike=-1.0))
        assert bad.valid is False
        assert len(bad.issues) >= 1


# ---------------------------------------------------------------------------
# Null semantics — zero is NEVER a substitute for absent data
# ---------------------------------------------------------------------------


class TestNullSemantics:
    """Critical: None is allowed; 0 as a placeholder for absent IV is rejected."""

    def test_none_iv_is_valid(self) -> None:
        """Absent IV → None; must not be rejected."""
        result = _validate(_valid_record(iv=None))
        assert result.valid is True

    def test_zero_iv_is_invalid(self) -> None:
        """iv=0 is the classic placeholder zero — Requirement 6.4 forbids it."""
        result = _validate(_valid_record(iv=0.0))
        assert result.valid is False

    def test_none_oi_is_valid(self) -> None:
        """Absent OI → None; never populated from tradedValue (Requirement 6.2)."""
        result = _validate(_valid_record(oi=None))
        assert result.valid is True

    def test_zero_oi_is_valid(self) -> None:
        """oi=0 means no open positions — this is semantically valid (not a placeholder)."""
        result = _validate(_valid_record(oi=0.0))
        assert result.valid is True

    def test_none_bid_ask_is_valid(self) -> None:
        """Absent bid/ask → None; zeros are not substituted (Requirement 6.6)."""
        result = _validate(_valid_record(bid=None, ask=None))
        assert result.valid is True
