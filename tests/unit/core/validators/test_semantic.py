"""
Unit tests for src/core/validators/semantic.py — pipeline step 5.

Covers:
- OI not populated from tradedValue (explicit substitution detection)
- OI absent → oi=None, oiMissing=True
- OI explicitly provided → not nulled even if equals tradedValue
- IV zero-substitution detection and correction
- IV explicitly provided as 0.0 → preserved
- IV absent → ivMissing=True
- Greeks zero-substitution: each of delta, gamma, theta, vega, rho
- Greeks explicitly provided → preserved
- bid/ask zero-substitution detection and correction
- bid/ask explicitly provided → preserved
- run_semantic_validation composite runner
- Record mutation is in-place
- No fields modified when all values are legitimate

Requirements: 6.2, 6.3, 6.4, 6.5, 6.6, 3.3
"""

from __future__ import annotations

import pytest

from src.core.validators.semantic import (
    run_semantic_validation,
    validate_bid_ask_not_zero_substituted,
    validate_greeks_not_zero_substituted,
    validate_iv_not_zero_substituted,
    validate_oi_not_from_traded_value,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_clean_record(**overrides) -> dict:
    """Return a minimal 'clean' record with no suspicious zero/substitution."""
    rec = {
        "instrumentId": "NSE:NIFTY:IDX",
        "ltp": 22150.50,
        "oi": 1500000,
        "tradedValue": 9_876_543_210.0,
        "oi_explicitly_provided": True,
        "iv": 12.5,
        "iv_explicitly_provided": True,
        "delta": 0.5,
        "delta_explicitly_provided": True,
        "gamma": 0.01,
        "gamma_explicitly_provided": True,
        "theta": -5.0,
        "theta_explicitly_provided": True,
        "vega": 8.0,
        "vega_explicitly_provided": True,
        "rho": 0.3,
        "rho_explicitly_provided": True,
        "bid": 22149.0,
        "bid_explicitly_provided": True,
        "ask": 22151.0,
        "ask_explicitly_provided": True,
    }
    rec.update(overrides)
    return rec


# ---------------------------------------------------------------------------
# validate_oi_not_from_traded_value
# ---------------------------------------------------------------------------


class TestValidateOiNotFromTradedValue:

    def test_oi_equals_traded_value_not_explicit_is_corrected(self):
        """OI matching tradedValue with no explicit flag → nulled."""
        rec = {
            "oi": 9_876_543_210,
            "tradedValue": 9_876_543_210,
            "oi_explicitly_provided": False,
        }
        ok, reason = validate_oi_not_from_traded_value(rec)
        assert ok is False
        assert rec["oi"] is None
        assert rec["oiMissing"] is True
        assert "tradedValue" in reason

    def test_oi_equals_traded_value_explicit_is_preserved(self):
        """If oi_explicitly_provided=True, keep OI even if it equals tradedValue."""
        rec = {
            "oi": 100,
            "tradedValue": 100,
            "oi_explicitly_provided": True,
        }
        ok, reason = validate_oi_not_from_traded_value(rec)
        assert ok is True
        assert rec["oi"] == 100  # unchanged

    def test_oi_absent_sets_missing_flag(self):
        """None OI gets oiMissing=True."""
        rec = {"oi": None, "tradedValue": 500.0}
        ok, _ = validate_oi_not_from_traded_value(rec)
        assert ok is True
        assert rec["oiMissing"] is True

    def test_oi_different_from_traded_value_not_corrected(self):
        """OI that is genuinely different from tradedValue is kept."""
        rec = {
            "oi": 1_500_000,
            "tradedValue": 9_876_543_210.0,
            "oi_explicitly_provided": False,
        }
        ok, _ = validate_oi_not_from_traded_value(rec)
        assert ok is True
        assert rec["oi"] == 1_500_000

    def test_oi_equals_traded_value_no_flag_field(self):
        """Missing oi_explicitly_provided key is treated as False."""
        rec = {"oi": 42, "tradedValue": 42}
        ok, _ = validate_oi_not_from_traded_value(rec)
        assert ok is False
        assert rec["oi"] is None
        assert rec["oiMissing"] is True

    def test_oi_none_no_traded_value(self):
        rec = {"oi": None}
        ok, _ = validate_oi_not_from_traded_value(rec)
        assert ok is True
        assert rec["oiMissing"] is True

    def test_float_oi_equals_traded_value_corrected(self):
        """Float equality: 100.0 == 100 → corrected."""
        rec = {"oi": 100.0, "tradedValue": 100.0, "oi_explicitly_provided": False}
        ok, _ = validate_oi_not_from_traded_value(rec)
        assert ok is False
        assert rec["oi"] is None


# ---------------------------------------------------------------------------
# validate_iv_not_zero_substituted
# ---------------------------------------------------------------------------


class TestValidateIvNotZeroSubstituted:

    def test_zero_iv_not_explicit_is_nulled(self):
        rec = {"iv": 0.0, "iv_explicitly_provided": False}
        ok, reason = validate_iv_not_zero_substituted(rec)
        assert ok is False
        assert rec["iv"] is None
        assert rec["ivMissing"] is True
        assert "zero" in reason.lower() or "0.0" in reason

    def test_zero_iv_explicit_is_preserved(self):
        """If provider explicitly sends 0.0 IV (unusual but valid), keep it."""
        rec = {"iv": 0.0, "iv_explicitly_provided": True}
        ok, _ = validate_iv_not_zero_substituted(rec)
        assert ok is True
        assert rec["iv"] == 0.0

    def test_positive_iv_not_affected(self):
        rec = {"iv": 12.5, "iv_explicitly_provided": False}
        ok, _ = validate_iv_not_zero_substituted(rec)
        assert ok is True
        assert rec["iv"] == 12.5

    def test_none_iv_sets_missing_flag(self):
        rec = {"iv": None}
        ok, _ = validate_iv_not_zero_substituted(rec)
        assert ok is True
        assert rec["ivMissing"] is True

    def test_zero_iv_no_explicit_flag_field(self):
        """Missing iv_explicitly_provided treated as False."""
        rec = {"iv": 0.0}
        ok, _ = validate_iv_not_zero_substituted(rec)
        assert ok is False
        assert rec["iv"] is None

    def test_negative_iv_not_affected_by_zero_check(self):
        """Negative IV is wrong for other reasons — this check only catches zero."""
        rec = {"iv": -5.0, "iv_explicitly_provided": False}
        ok, _ = validate_iv_not_zero_substituted(rec)
        # Zero-substitution check doesn't fire; -5.0 != 0.0
        assert ok is True
        assert rec["iv"] == -5.0


# ---------------------------------------------------------------------------
# validate_greeks_not_zero_substituted
# ---------------------------------------------------------------------------


class TestValidateGreeksNotZeroSubstituted:

    def test_all_greeks_zero_not_explicit_nulled(self):
        rec = {
            "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0,
        }
        ok, reason = validate_greeks_not_zero_substituted(rec)
        assert ok is False
        for g in ("delta", "gamma", "theta", "vega", "rho"):
            assert rec[g] is None, f"{g} should be None"

    def test_single_greek_zero_not_explicit_nulled(self):
        rec = {"delta": 0.0, "gamma": 0.5}
        ok, _ = validate_greeks_not_zero_substituted(rec)
        assert ok is False
        assert rec["delta"] is None
        assert rec["gamma"] == 0.5

    def test_greek_zero_explicit_preserved(self):
        """delta=0.0 with explicit flag kept (e.g. deep-OTM option)."""
        rec = {"delta": 0.0, "delta_explicitly_provided": True}
        ok, _ = validate_greeks_not_zero_substituted(rec)
        assert ok is True
        assert rec["delta"] == 0.0

    def test_none_greeks_unaffected(self):
        rec = {"delta": None, "gamma": None}
        ok, _ = validate_greeks_not_zero_substituted(rec)
        assert ok is True

    def test_non_zero_greeks_unaffected(self):
        rec = {
            "delta": 0.45, "gamma": 0.01, "theta": -4.5,
            "vega": 7.3, "rho": 0.2,
        }
        ok, _ = validate_greeks_not_zero_substituted(rec)
        assert ok is True

    def test_theta_zero_not_explicit_nulled(self):
        rec = {"theta": 0.0}
        ok, _ = validate_greeks_not_zero_substituted(rec)
        assert ok is False
        assert rec["theta"] is None


# ---------------------------------------------------------------------------
# validate_bid_ask_not_zero_substituted
# ---------------------------------------------------------------------------


class TestValidateBidAskNotZeroSubstituted:

    def test_bid_zero_not_explicit_nulled(self):
        rec = {"bid": 0.0, "bid_explicitly_provided": False}
        ok, reason = validate_bid_ask_not_zero_substituted(rec)
        assert ok is False
        assert rec["bid"] is None
        assert "bid" in reason

    def test_ask_zero_not_explicit_nulled(self):
        rec = {"ask": 0.0, "ask_explicitly_provided": False}
        ok, reason = validate_bid_ask_not_zero_substituted(rec)
        assert ok is False
        assert rec["ask"] is None

    def test_both_zero_not_explicit_both_nulled(self):
        rec = {"bid": 0.0, "ask": 0.0}
        ok, _ = validate_bid_ask_not_zero_substituted(rec)
        assert ok is False
        assert rec["bid"] is None
        assert rec["ask"] is None

    def test_bid_zero_explicit_preserved(self):
        """If provider explicitly sends bid=0 (theoretically possible), keep it."""
        rec = {"bid": 0.0, "bid_explicitly_provided": True}
        ok, _ = validate_bid_ask_not_zero_substituted(rec)
        assert ok is True
        assert rec["bid"] == 0.0

    def test_none_bid_ask_unaffected(self):
        rec = {"bid": None, "ask": None}
        ok, _ = validate_bid_ask_not_zero_substituted(rec)
        assert ok is True

    def test_positive_bid_ask_unaffected(self):
        rec = {"bid": 22149.0, "ask": 22151.0}
        ok, _ = validate_bid_ask_not_zero_substituted(rec)
        assert ok is True


# ---------------------------------------------------------------------------
# run_semantic_validation — composite runner
# ---------------------------------------------------------------------------


class TestRunSemanticValidation:

    def test_clean_record_passes(self):
        rec = make_clean_record()
        rec_out, ok, incident = run_semantic_validation(rec, provider="angel_one")
        assert ok is True
        assert incident is None
        assert "_semanticIncident" not in rec_out

    def test_corrections_produce_incident(self):
        """Record with oi=tradedValue triggers a correction and incident."""
        rec = {
            "instrumentId": "NSE:BANKNIFTY:IDX",
            "oi": 500,
            "tradedValue": 500,
            "oi_explicitly_provided": False,
        }
        rec_out, ok, incident = run_semantic_validation(rec, provider="scrapling")
        # ok is still True (non-fatal) but incident is attached to the record
        assert ok is True
        assert incident is None  # returned as None (attached to record)
        assert "_semanticIncident" in rec_out
        assert rec_out["oi"] is None

    def test_multiple_corrections_all_applied(self):
        rec = {
            "instrumentId": "NSE:NIFTY:IDX",
            "oi": 999,
            "tradedValue": 999,
            "oi_explicitly_provided": False,
            "iv": 0.0,
            "iv_explicitly_provided": False,
            "delta": 0.0,
        }
        rec_out, ok, incident = run_semantic_validation(rec, provider="test")
        assert ok is True
        assert rec_out["oi"] is None
        assert rec_out["iv"] is None
        assert rec_out["delta"] is None
        si = rec_out.get("_semanticIncident")
        assert si is not None
        assert si["details"]["correctionCount"] >= 2

    def test_return_type_always_tuple_of_three(self):
        rec = {"instrumentId": "X"}
        result = run_semantic_validation(rec, provider="test")
        assert len(result) == 3
        modified, ok, inc = result
        assert isinstance(modified, dict)
        assert isinstance(ok, bool)

    def test_record_mutated_in_place(self):
        rec = {
            "instrumentId": "NSE:NIFTY:IDX",
            "iv": 0.0,
            "iv_explicitly_provided": False,
        }
        rec_before_id = id(rec)
        run_semantic_validation(rec, provider="test")
        # Same dict object is mutated
        assert id(rec) == rec_before_id
        assert rec["iv"] is None
