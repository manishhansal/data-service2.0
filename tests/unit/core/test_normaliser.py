"""
Unit tests for src/core/normaliser.py

Covers:
- OI null semantics: absent → (None, True); supplied normally → value
- OI never populated from tradedValue (semantic integrity)
- IV null semantics: absent → None+missing; zero → None+missing; real → value
- Greeks null semantics: absent/zero → None+missing; real → value
- Bid/ask null semantics: absent or zero → None+missing; real → value
- Volume: absent → 0 + volumeUnavailable=True; valid → value; negative → flag
- tradedValue is kept distinct from oi at all times
- normalisationVersion semver is attached
- MetricTag annotations are present on output
- OHLCV normalisation: happy path, missing required fields → ok=False + incident
- Quote normalisation: happy path, missing ltp → ok=False + incident
- Option chain row: happy path, missing required fields → ok=False + incident
- Partial response: some invalid fields → process valid, set invalid to None
- Change/changePct derivation when not supplied

Requirements: 6.1–6.12, 3.2, 3.3
"""

from __future__ import annotations

import pytest

from src.core.normaliser import (
    NORMALISATION_VERSION,
    MetricTag,
    Normaliser,
    _resolve_bid_ask,
    _resolve_greeks,
    _resolve_iv,
    _resolve_oi,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def normaliser() -> Normaliser:
    return Normaliser()


def _valid_ohlcv() -> dict:
    return {
        "instrumentId": "NSE:NIFTY:IDX",
        "open": 22100.0,
        "high": 22300.0,
        "low": 22050.0,
        "close": 22250.0,
        "volume": 12345,
        "time": 1705300000000,
        "exchange": "NSE",
        "intervalStr": "1m",
    }


def _valid_quote() -> dict:
    return {
        "instrumentId": "NSE:RELIANCE:EQ",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "ltp": 2950.75,
        "open": 2940.0,
        "high": 2960.0,
        "low": 2930.0,
        "prevClose": 2935.0,
        "volume": 50000,
        "tradedValue": 147537500.0,
    }


def _valid_option_row() -> dict:
    return {
        "instrumentId": "NFO:NIFTY:OPTIDX",
        "strike": 22000.0,
        "optionType": "CE",
        "ltp": 200.0,
        "bid": 199.5,
        "ask": 200.5,
        "oi": 1500000,
        "oiChange": 50000,
        "volume": 25000,
        "tradedValue": 5000000.0,
        "iv": 15.5,
        "delta": 0.45,
        "gamma": 0.002,
        "theta": -8.5,
        "vega": 12.3,
        "rho": 0.3,
    }


# ===========================================================================
# OI null semantics (_resolve_oi)
# ===========================================================================


class TestResolveOi:
    def test_oi_absent_returns_none_and_missing_true(self) -> None:
        oi, missing = _resolve_oi({"tradedValue": 1000.0})
        assert oi is None
        assert missing is True

    def test_oi_none_returns_none_and_missing_true(self) -> None:
        oi, missing = _resolve_oi({"oi": None, "tradedValue": 1000.0})
        assert oi is None
        assert missing is True

    def test_oi_valid_returns_value_and_missing_false(self) -> None:
        oi, missing = _resolve_oi({"oi": 500000, "tradedValue": 1000.0})
        assert oi == 500000
        assert missing is False

    def test_oi_equals_traded_value_rejected(self) -> None:
        """OI must never be populated from tradedValue. Requirement 6.2, 3.3."""
        oi, missing = _resolve_oi({"oi": 9999.0, "tradedValue": 9999.0})
        assert oi is None
        assert missing is True

    def test_oi_differs_from_traded_value_accepted(self) -> None:
        oi, missing = _resolve_oi({"oi": 100, "tradedValue": 9999.0})
        assert oi == 100
        assert missing is False

    def test_oi_zero_is_valid_when_provider_supplies_it(self) -> None:
        """Zero OI is a legitimate value (e.g. newly listed contract)."""
        oi, missing = _resolve_oi({"oi": 0, "tradedValue": 9999.0})
        assert oi == 0
        assert missing is False

    def test_oi_conversion_to_int(self) -> None:
        oi, missing = _resolve_oi({"oi": "123456", "tradedValue": 0.0})
        assert oi == 123456
        assert missing is False

    def test_oi_invalid_string_returns_missing(self) -> None:
        oi, missing = _resolve_oi({"oi": "not_a_number", "tradedValue": 0.0})
        assert oi is None
        assert missing is True


# ===========================================================================
# IV null semantics (_resolve_iv)
# ===========================================================================


class TestResolveIv:
    def test_iv_absent_returns_none_missing(self) -> None:
        iv, missing = _resolve_iv({})
        assert iv is None
        assert missing is True

    def test_iv_none_returns_none_missing(self) -> None:
        iv, missing = _resolve_iv({"iv": None})
        assert iv is None
        assert missing is True

    def test_iv_zero_is_not_a_substitute(self) -> None:
        """Zero IV is not a valid substitute for missing IV. Requirement 6.4."""
        iv, missing = _resolve_iv({"iv": 0.0})
        assert iv is None
        assert missing is True

    def test_iv_zero_int_is_not_a_substitute(self) -> None:
        iv, missing = _resolve_iv({"iv": 0})
        assert iv is None
        assert missing is True

    def test_iv_valid_value_accepted(self) -> None:
        iv, missing = _resolve_iv({"iv": 15.5})
        assert iv == pytest.approx(15.5)
        assert missing is False

    def test_iv_small_positive_accepted(self) -> None:
        iv, missing = _resolve_iv({"iv": 0.001})
        assert iv == pytest.approx(0.001)
        assert missing is False


# ===========================================================================
# Greeks null semantics (_resolve_greeks)
# ===========================================================================


class TestResolveGreeks:
    def test_all_absent_returns_all_none_and_missing_true(self) -> None:
        greeks, missing = _resolve_greeks({})
        assert greeks["delta"] is None
        assert greeks["gamma"] is None
        assert greeks["theta"] is None
        assert greeks["vega"] is None
        assert greeks["rho"] is None
        assert missing is True

    def test_partial_present_rest_none_missing_false(self) -> None:
        """greeks_missing is False when at least one Greek is supplied.

        greeksMissing = True only when ALL five are absent.  When some are
        supplied, the consumer can use the available ones; the missing ones
        are individually None.
        """
        greeks, missing = _resolve_greeks({"delta": 0.45, "gamma": 0.003})
        assert greeks["delta"] == pytest.approx(0.45)
        assert greeks["gamma"] == pytest.approx(0.003)
        assert greeks["theta"] is None
        assert greeks["vega"] is None
        assert greeks["rho"] is None
        assert missing is False  # at least one is present → not all missing

    def test_all_present_returns_values_missing_false(self) -> None:
        raw = {"delta": 0.5, "gamma": 0.01, "theta": -5.0, "vega": 10.0, "rho": 0.2}
        greeks, missing = _resolve_greeks(raw)
        assert greeks["delta"] == pytest.approx(0.5)
        assert greeks["rho"] == pytest.approx(0.2)
        assert missing is False

    def test_zero_greeks_are_accepted_when_supplied(self) -> None:
        """Zero is a legitimate Greek value (e.g. deep OTM). Requirement 6.5."""
        raw = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
        greeks, missing = _resolve_greeks(raw)
        assert greeks["delta"] == 0.0
        assert missing is False


# ===========================================================================
# Bid/ask null semantics (_resolve_bid_ask)
# ===========================================================================


class TestResolveBidAsk:
    def test_both_absent_returns_none_missing(self) -> None:
        bid, ask, missing = _resolve_bid_ask({})
        assert bid is None
        assert ask is None
        assert missing is True

    def test_zero_bid_treated_as_missing(self) -> None:
        """Zero bid is not a valid substitute. Requirement 6.6."""
        bid, ask, missing = _resolve_bid_ask({"bid": 0.0, "ask": 200.5})
        assert bid is None
        assert ask == pytest.approx(200.5)
        assert missing is False  # ask is present; only both-missing flags True

    def test_zero_ask_treated_as_missing(self) -> None:
        bid, ask, missing = _resolve_bid_ask({"bid": 199.5, "ask": 0.0})
        assert bid == pytest.approx(199.5)
        assert ask is None
        assert missing is False

    def test_both_zero_both_missing(self) -> None:
        bid, ask, missing = _resolve_bid_ask({"bid": 0.0, "ask": 0.0})
        assert bid is None
        assert ask is None
        assert missing is True

    def test_valid_bid_ask(self) -> None:
        bid, ask, missing = _resolve_bid_ask({"bid": 199.5, "ask": 200.5})
        assert bid == pytest.approx(199.5)
        assert ask == pytest.approx(200.5)
        assert missing is False


# ===========================================================================
# normalise_ohlcv
# ===========================================================================


class TestNormaliseOhlcv:
    def test_happy_path(self, normaliser: Normaliser) -> None:
        raw = _valid_ohlcv()
        out, ok, incident = normaliser.normalise_ohlcv(raw, "angel_one")
        assert ok is True
        assert incident is None
        assert out["open"] == 22100.0
        assert out["high"] == 22300.0
        assert out["low"] == 22050.0
        assert out["close"] == 22250.0
        assert out["volume"] == 12345
        assert out["volumeUnavailable"] is False
        assert out["oi"] is None
        assert out["oiMissing"] is True
        assert out["normalisationVersion"] == NORMALISATION_VERSION
        assert out["provider"] == "angel_one"

    def test_with_oi_supplied(self, normaliser: Normaliser) -> None:
        raw = {**_valid_ohlcv(), "oi": 500000}
        out, ok, _ = normaliser.normalise_ohlcv(raw, "jugaad_data")
        assert ok is True
        assert out["oi"] == 500000
        assert out["oiMissing"] is False

    def test_volume_missing_sets_unavailable(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_ohlcv().items() if k != "volume"}
        out, ok, _ = normaliser.normalise_ohlcv(raw, "openchart")
        assert ok is True
        assert out["volume"] == 0
        assert out["volumeUnavailable"] is True

    def test_missing_required_price_returns_false(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_ohlcv().items() if k != "close"}
        out, ok, incident = normaliser.normalise_ohlcv(raw, "angel_one")
        assert ok is False
        assert incident is not None
        assert incident["incidentType"] == "SCHEMA_VALIDATION"
        assert "close" in str(incident["details"]["validationErrors"])

    def test_all_required_prices_missing_returns_false(self, normaliser: Normaliser) -> None:
        out, ok, incident = normaliser.normalise_ohlcv({"time": 123}, "angel_one")
        assert ok is False
        assert incident is not None

    def test_metric_tags_present(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise_ohlcv(_valid_ohlcv(), "angel_one")
        assert ok is True
        assert "_metricTags" in out
        assert out["_metricTags"]["open"] == MetricTag.OBSERVED.value
        assert out["_metricTags"]["close"] == MetricTag.OBSERVED.value

    def test_timestamp_variants_resolved(self, normaliser: Normaliser) -> None:
        raw = {**_valid_ohlcv()}
        del raw["time"]
        raw["openTime"] = 1705300000000
        out, ok, _ = normaliser.normalise_ohlcv(raw, "binance")
        assert ok is True
        assert out["time"] == 1705300000000

    def test_oi_never_from_traded_value(self, normaliser: Normaliser) -> None:
        raw = {**_valid_ohlcv(), "tradedValue": 9999, "oi": 9999}
        out, ok, _ = normaliser.normalise_ohlcv(raw, "angel_one")
        assert ok is True
        assert out["oi"] is None
        assert out["oiMissing"] is True

    def test_negative_volume_treated_as_unavailable(self, normaliser: Normaliser) -> None:
        raw = {**_valid_ohlcv(), "volume": -1}
        out, ok, _ = normaliser.normalise_ohlcv(raw, "angel_one")
        assert ok is True
        assert out["volume"] == 0
        assert out["volumeUnavailable"] is True

    def test_returns_raw_on_failure(self, normaliser: Normaliser) -> None:
        raw = {"time": 123, "instrumentId": "X"}
        returned, ok, _ = normaliser.normalise_ohlcv(raw, "angel_one")
        assert ok is False
        assert returned is raw


# ===========================================================================
# normalise_quote
# ===========================================================================


class TestNormaliseQuote:
    def test_happy_path(self, normaliser: Normaliser) -> None:
        raw = _valid_quote()
        out, ok, incident = normaliser.normalise_quote(raw, "scrapling_nse")
        assert ok is True
        assert incident is None
        assert out["ltp"] == 2950.75
        assert out["normalisationVersion"] == NORMALISATION_VERSION
        assert out["provider"] == "scrapling_nse"

    def test_missing_ltp_returns_false(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_quote().items() if k != "ltp"}
        out, ok, incident = normaliser.normalise_quote(raw, "angel_one")
        assert ok is False
        assert incident is not None
        assert incident["incidentType"] == "SCHEMA_VALIDATION"

    def test_oi_absent_null_and_missing(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise_quote(_valid_quote(), "angel_one")
        assert ok is True
        assert out["oi"] is None
        assert out["oiMissing"] is True

    def test_oi_supplied_for_fo(self, normaliser: Normaliser) -> None:
        raw = {**_valid_quote(), "oi": 750000}
        out, ok, _ = normaliser.normalise_quote(raw, "angel_one")
        assert ok is True
        assert out["oi"] == 750000
        assert out["oiMissing"] is False

    def test_oi_not_from_traded_value(self, normaliser: Normaliser) -> None:
        tv = 147537500.0
        raw = {**_valid_quote(), "oi": tv, "tradedValue": tv}
        out, ok, _ = normaliser.normalise_quote(raw, "angel_one")
        assert ok is True
        assert out["oi"] is None
        assert out["oiMissing"] is True

    def test_bid_ask_absent_null_and_missing(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise_quote(_valid_quote(), "scrapling_nse")
        assert ok is True
        assert out["bid"] is None
        assert out["ask"] is None
        assert out["bidAskMissing"] is True

    def test_bid_ask_zero_treated_as_missing(self, normaliser: Normaliser) -> None:
        raw = {**_valid_quote(), "bid": 0.0, "ask": 0.0}
        out, ok, _ = normaliser.normalise_quote(raw, "angel_one")
        assert ok is True
        assert out["bid"] is None
        assert out["ask"] is None
        assert out["bidAskMissing"] is True

    def test_valid_bid_ask_accepted(self, normaliser: Normaliser) -> None:
        raw = {**_valid_quote(), "bid": 2949.0, "ask": 2951.0}
        out, ok, _ = normaliser.normalise_quote(raw, "upstox")
        assert ok is True
        assert out["bid"] == pytest.approx(2949.0)
        assert out["ask"] == pytest.approx(2951.0)
        assert out["bidAskMissing"] is False

    def test_volume_absent_zero_flag(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_quote().items() if k != "volume"}
        out, ok, _ = normaliser.normalise_quote(raw, "scrapling_nse")
        assert ok is True
        assert out["volume"] == 0
        assert out["volumeUnavailable"] is True

    def test_change_pct_derived_when_absent(self, normaliser: Normaliser) -> None:
        raw = {
            "instrumentId": "NSE:X",
            "symbol": "X",
            "ltp": 110.0,
            "prevClose": 100.0,
        }
        out, ok, _ = normaliser.normalise_quote(raw, "scrapling_nse")
        assert ok is True
        assert out["change"] == pytest.approx(10.0)
        assert out["changePct"] == pytest.approx(10.0)

    def test_metric_tags_present(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise_quote(_valid_quote(), "angel_one")
        assert ok is True
        assert "_metricTags" in out
        assert out["_metricTags"]["ltp"] == MetricTag.OBSERVED.value
        assert out["_metricTags"]["change"] == MetricTag.DERIVED.value
        assert out["_metricTags"]["changePct"] == MetricTag.DERIVED.value

    def test_traded_value_preserved_as_distinct_field(self, normaliser: Normaliser) -> None:
        raw = {**_valid_quote(), "tradedValue": 147537500.0}
        out, ok, _ = normaliser.normalise_quote(raw, "angel_one")
        assert ok is True
        assert out["tradedValue"] == pytest.approx(147537500.0)
        # tradedValue and oi must be independent
        assert out["tradedValue"] != out["oi"]

    def test_normalise_dispatcher_quote(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise(_valid_quote(), "angel_one", data_type="quote")
        assert ok is True
        assert out["ltp"] == 2950.75


# ===========================================================================
# normalise_option_chain_row
# ===========================================================================


class TestNormaliseOptionChainRow:
    def test_happy_path(self, normaliser: Normaliser) -> None:
        raw = _valid_option_row()
        out, ok, incident = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is True
        assert incident is None
        assert out["strike"] == 22000.0
        assert out["optionType"] == "CE"
        assert out["iv"] == pytest.approx(15.5)
        assert out["ivMissing"] is False
        assert out["delta"] == pytest.approx(0.45)
        assert out["greeksMissing"] is False
        assert out["oiMissing"] is False
        assert out["normalisationVersion"] == NORMALISATION_VERSION

    def test_missing_strike_returns_false(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_option_row().items() if k != "strike"}
        out, ok, incident = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is False
        assert incident is not None
        assert "strike" in str(incident["details"]["validationErrors"])

    def test_missing_option_type_returns_false(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_option_row().items() if k != "optionType"}
        out, ok, incident = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is False
        assert incident is not None

    def test_iv_absent_null_and_missing(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_option_row().items() if k != "iv"}
        out, ok, _ = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is True
        assert out["iv"] is None
        assert out["ivMissing"] is True

    def test_iv_zero_is_null(self, normaliser: Normaliser) -> None:
        """Zero IV is not valid. Requirement 6.4."""
        raw = {**_valid_option_row(), "iv": 0.0}
        out, ok, _ = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is True
        assert out["iv"] is None
        assert out["ivMissing"] is True

    def test_greeks_absent_all_none_and_missing(self, normaliser: Normaliser) -> None:
        raw = {
            k: v for k, v in _valid_option_row().items()
            if k not in ("delta", "gamma", "theta", "vega", "rho")
        }
        out, ok, _ = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is True
        for greek in ("delta", "gamma", "theta", "vega", "rho"):
            assert out[greek] is None, f"{greek} should be None"
        assert out["greeksMissing"] is True

    def test_oi_absent_null_and_missing(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_option_row().items() if k != "oi"}
        out, ok, _ = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is True
        assert out["oi"] is None
        assert out["oiMissing"] is True

    def test_oi_not_from_traded_value(self, normaliser: Normaliser) -> None:
        tv = 5000000.0
        raw = {**_valid_option_row(), "oi": tv, "tradedValue": tv}
        out, ok, _ = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is True
        assert out["oi"] is None
        assert out["oiMissing"] is True

    def test_bid_ask_zero_missing(self, normaliser: Normaliser) -> None:
        raw = {**_valid_option_row(), "bid": 0.0, "ask": 0.0}
        out, ok, _ = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is True
        assert out["bid"] is None
        assert out["ask"] is None
        assert out["bidAskMissing"] is True

    def test_volume_absent_missing_flag(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_option_row().items() if k != "volume"}
        out, ok, _ = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is True
        assert out["volume"] is None
        assert out["volumeMissing"] is True

    def test_oi_change_absent_missing_flag(self, normaliser: Normaliser) -> None:
        raw = {k: v for k, v in _valid_option_row().items() if k != "oiChange"}
        out, ok, _ = normaliser.normalise_option_chain_row(raw, "scrapling_nse")
        assert ok is True
        assert out["oiChange"] is None
        assert out["oiChangeMissing"] is True

    def test_metric_tags_present(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise_option_chain_row(_valid_option_row(), "scrapling_nse")
        assert ok is True
        assert "_metricTags" in out
        assert out["_metricTags"]["iv"] == MetricTag.MODELLED.value
        assert out["_metricTags"]["delta"] == MetricTag.MODELLED.value
        assert out["_metricTags"]["strike"] == MetricTag.OBSERVED.value

    def test_normalise_dispatcher_option_chain(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise(
            _valid_option_row(), "scrapling_nse", data_type="option_chain_row"
        )
        assert ok is True
        assert out["strike"] == 22000.0


# ===========================================================================
# normalisationVersion
# ===========================================================================


class TestNormalisationVersion:
    def test_default_version_attached_to_ohlcv(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise_ohlcv(_valid_ohlcv(), "angel_one")
        assert ok is True
        assert out["normalisationVersion"] == "2.0.0"

    def test_custom_version_propagates(self) -> None:
        n = Normaliser(normalisation_version="3.1.0")
        out, ok, _ = n.normalise_ohlcv(_valid_ohlcv(), "angel_one")
        assert ok is True
        assert out["normalisationVersion"] == "3.1.0"

    def test_version_attached_to_quote(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise_quote(_valid_quote(), "angel_one")
        assert ok is True
        assert out["normalisationVersion"] == "2.0.0"

    def test_version_attached_to_option_chain_row(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise_option_chain_row(_valid_option_row(), "scrapling_nse")
        assert ok is True
        assert out["normalisationVersion"] == "2.0.0"


# ===========================================================================
# Normalise dispatcher default behaviour
# ===========================================================================


class TestNormaliseDispatcher:
    def test_unknown_data_type_falls_back_to_quote(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise(_valid_quote(), "angel_one", data_type="unknown_type")
        assert ok is True
        assert out["ltp"] == 2950.75

    def test_ohlcv_dispatch(self, normaliser: Normaliser) -> None:
        out, ok, _ = normaliser.normalise(_valid_ohlcv(), "angel_one", data_type="ohlcv")
        assert ok is True
        assert out["open"] == 22100.0
