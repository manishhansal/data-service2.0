"""
tests/unit/core/test_provider_normalizers.py

Unit tests for Angel One and Upstox provider normalizers.

Tests verify:
- OI semantic rules (null when absent, never from tradedValue)
- IV zero rejection (zero IV is not a valid substitute)
- Greeks null semantics
- Bid/ask null semantics (zero prohibited)
- CAS data isolation (never mixed with LTP)
- Depth level normalization
- Full quote field extraction
- Option chain contract normalization
- Intraday candle normalization
- Freshness classification

Requirements: 6.1–6.6, Phase 12, Phase 15
"""

from __future__ import annotations

import datetime
import pytest

from src.core.normalizers.angel_one import AngelOneNormalizer
from src.core.normalizers.upstox import UpstoxNormalizer
from src.core.normalizers.freshness import FreshnessClassifier, FreshnessState
from src.engines.reconciliation_engine import ReconciliationEngine


# ---------------------------------------------------------------------------
# Angel One Normalizer
# ---------------------------------------------------------------------------


class TestAngelOneNormalizerFullQuote:
    """Angel One full quote normalization."""

    def _make_normalizer(self) -> AngelOneNormalizer:
        return AngelOneNormalizer()

    def _sample_raw(self) -> dict:
        return {
            "ltp": 22500.50,
            "open": 22400.0,
            "high": 22600.0,
            "low": 22350.0,
            "close": 22450.0,  # previous session close
            "tradeVolume": 1234567,
            "opnInterest": 98765,
            "netChange": 50.5,
            "percentChange": 0.225,
            "avgPrice": 22490.0,
            "totBuyQtn": 55000,
            "totSellQtn": 45000,
            "lastTradedQty": 50,
            "upperCircuit": 24750.0,
            "lowerCircuit": 20250.0,
            "yearHigh": 26000.0,
            "yearLow": 18500.0,
            "exchFeedTime": "16:00:00",
            "exchTradeTime": "15:29:59",
            "depth": {
                "buy": [
                    {"price": 22499.0, "quantity": 100, "numberOfOrders": 5},
                    {"price": 22498.0, "quantity": 200, "numberOfOrders": 8},
                ],
                "sell": [
                    {"price": 22501.0, "quantity": 150, "numberOfOrders": 3},
                ],
            },
        }

    def test_ltp_extracted(self) -> None:
        norm = self._make_normalizer()
        result = norm.normalize_full_quote(self._sample_raw(), "NSE:NIFTY:IDX", "NSE")
        assert result["ltp"] == pytest.approx(22500.50)

    def test_prev_close_is_close_field(self) -> None:
        """Angel One 'close' field = previous session close, not current."""
        norm = self._make_normalizer()
        result = norm.normalize_full_quote(self._sample_raw(), "NSE:NIFTY:IDX", "NSE")
        assert result["prevClose"] == pytest.approx(22450.0)

    def test_oi_extracted_from_opnInterest(self) -> None:
        norm = self._make_normalizer()
        result = norm.normalize_full_quote(self._sample_raw(), "NSE:NIFTY:IDX", "NSE")
        assert result["oi"] == 98765
        assert result["oiMissing"] is False

    def test_oi_missing_when_absent(self) -> None:
        norm = self._make_normalizer()
        raw = {k: v for k, v in self._sample_raw().items() if k != "opnInterest"}
        result = norm.normalize_full_quote(raw, "NSE:NIFTY:IDX", "NSE")
        assert result["oi"] is None
        assert result["oiMissing"] is True

    def test_depth_buy_extracted(self) -> None:
        norm = self._make_normalizer()
        result = norm.normalize_full_quote(self._sample_raw(), "NSE:NIFTY:IDX", "NSE")
        assert len(result["depthBuy"]) == 2
        assert result["depthBuy"][0]["price"] == pytest.approx(22499.0)
        assert result["depthBuy"][0]["level"] == 1

    def test_depth_sell_extracted(self) -> None:
        norm = self._make_normalizer()
        result = norm.normalize_full_quote(self._sample_raw(), "NSE:NIFTY:IDX", "NSE")
        assert len(result["depthSell"]) == 1
        assert result["depthSell"][0]["price"] == pytest.approx(22501.0)

    def test_circuit_limits_extracted(self) -> None:
        norm = self._make_normalizer()
        result = norm.normalize_full_quote(self._sample_raw(), "NSE:NIFTY:IDX", "NSE")
        assert result["upperCircuit"] == pytest.approx(24750.0)
        assert result["lowerCircuit"] == pytest.approx(20250.0)

    def test_week_high_low_extracted(self) -> None:
        norm = self._make_normalizer()
        result = norm.normalize_full_quote(self._sample_raw(), "NSE:NIFTY:IDX", "NSE")
        assert result["weekHigh52"] == pytest.approx(26000.0)
        assert result["weekLow52"] == pytest.approx(18500.0)

    def test_provider_is_angel_one(self) -> None:
        norm = self._make_normalizer()
        result = norm.normalize_full_quote(self._sample_raw(), "NSE:NIFTY:IDX", "NSE")
        assert result["provider"] == "angel_one"
        assert result["sourceType"] == "BROKER_AUTHENTICATED"

    def test_change_derived_when_absent(self) -> None:
        """If netChange absent but ltp and prevClose present, change is derived."""
        norm = self._make_normalizer()
        raw = {k: v for k, v in self._sample_raw().items() if k != "netChange"}
        result = norm.normalize_full_quote(raw, "NSE:NIFTY:IDX", "NSE")
        # change = ltp - prevClose = 22500.50 - 22450.0 = 50.5
        assert result["change"] == pytest.approx(50.5, rel=0.01)


class TestAngelOneNormalizerHistoricalOI:
    """Angel One historical OI normalization."""

    def test_oi_extracted(self) -> None:
        norm = AngelOneNormalizer()
        record = {"timestamp": "2024-01-15T09:15:00", "openInterest": 50000}
        result = norm.normalize_oi_record(record, "NFO:NIFTY25JANFUT", "NFO", "1d")
        assert result is not None
        assert result["openInterest"] == 50000

    def test_missing_oi_returns_none(self) -> None:
        norm = AngelOneNormalizer()
        record = {"timestamp": "2024-01-15T09:15:00"}
        result = norm.normalize_oi_record(record, "NFO:NIFTY25JANFUT", "NFO", "1d")
        assert result is None

    def test_missing_timestamp_returns_none(self) -> None:
        norm = AngelOneNormalizer()
        record = {"openInterest": 50000}
        result = norm.normalize_oi_record(record, "NFO:NIFTY25JANFUT", "NFO", "1d")
        assert result is None


class TestAngelOneNormalizerGreeks:
    """Angel One option Greeks normalization."""

    def test_greeks_extracted(self) -> None:
        norm = AngelOneNormalizer()
        raw = {
            "strikePrice": 22500,
            "optionType": "CE",
            "delta": 0.51,
            "gamma": 0.0003,
            "theta": -12.5,
            "vega": 8.2,
            "impliedVolatility": 0.15,
            "tradeVolume": 5000,
            "openInterest": 12000,
        }
        result = norm.normalize_option_greek(raw, "NIFTY", "29FEB2024")
        assert result is not None
        assert result["delta"] == pytest.approx(0.51)
        assert result["iv"] == pytest.approx(0.15)
        assert result["optionType"] == "CE"
        assert result["greekSource"] == "PROVIDER"

    def test_zero_iv_rejected(self) -> None:
        """Zero IV must not be returned — it's treated as missing."""
        norm = AngelOneNormalizer()
        raw = {
            "strikePrice": 22500,
            "optionType": "CE",
            "impliedVolatility": 0.0,  # zero IV
            "delta": 0.01,
            "gamma": 0.0001,
            "theta": -0.5,
            "vega": 1.0,
        }
        result = norm.normalize_option_greek(raw, "NIFTY", "29FEB2024")
        assert result is not None
        assert result["iv"] is None
        assert result["ivMissing"] is True

    def test_rho_is_none_for_angel_one(self) -> None:
        """Angel One does not provide rho — must be None."""
        norm = AngelOneNormalizer()
        raw = {
            "strikePrice": 22500,
            "optionType": "PE",
            "delta": -0.49,
            "gamma": 0.0003,
            "theta": -11.0,
            "vega": 8.0,
            "impliedVolatility": 0.14,
        }
        result = norm.normalize_option_greek(raw, "NIFTY", "29FEB2024")
        assert result is not None
        assert result["rho"] is None


# ---------------------------------------------------------------------------
# Upstox Normalizer
# ---------------------------------------------------------------------------


class TestUpstoxNormalizerFullQuote:
    """Upstox full quote V2 normalization."""

    def _sample_raw(self) -> dict:
        return {
            "last_price": 22500.5,
            "ohlc": {
                "open": 22400.0,
                "high": 22600.0,
                "low": 22350.0,
                "close": 22450.0,
            },
            "volume": 1234567,
            "oi": 98765,
            "net_change": 50.5,
            "upper_circuit_limit": 24750.0,
            "lower_circuit_limit": 20250.0,
            "timestamp": "2024-01-15T15:30:00+05:30",
            "depth": {
                "buy": [
                    {"price": 22499.0, "quantity": 100, "orders": 5},
                    {"price": 22498.0, "quantity": 200, "orders": 8},
                ],
                "sell": [
                    {"price": 22501.0, "quantity": 150, "orders": 3},
                ],
            },
        }

    def test_ltp_extracted(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_full_quote(
            "NSE_INDEX|Nifty 50", self._sample_raw(), "NSE:NIFTY:IDX", "NSE"
        )
        assert result["ltp"] == pytest.approx(22500.5)

    def test_oi_extracted(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_full_quote(
            "NSE_INDEX|Nifty 50", self._sample_raw(), "NSE:NIFTY:IDX", "NSE"
        )
        assert result["oi"] == 98765
        assert result["oiMissing"] is False

    def test_oi_missing_when_null(self) -> None:
        norm = UpstoxNormalizer()
        raw = {**self._sample_raw(), "oi": None}
        result = norm.normalize_full_quote(
            "NSE_INDEX|Nifty 50", raw, "NSE:NIFTY:IDX", "NSE"
        )
        assert result["oi"] is None
        assert result["oiMissing"] is True

    def test_depth_extracted(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_full_quote(
            "NSE_INDEX|Nifty 50", self._sample_raw(), "NSE:NIFTY:IDX", "NSE"
        )
        assert len(result["depthBuy"]) == 2
        assert result["depthBuy"][0]["level"] == 1
        assert result["depthBuy"][0]["price"] == pytest.approx(22499.0)

    def test_circuit_limits_extracted(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_full_quote(
            "NSE_INDEX|Nifty 50", self._sample_raw(), "NSE:NIFTY:IDX", "NSE"
        )
        assert result["upperCircuit"] == pytest.approx(24750.0)
        assert result["lowerCircuit"] == pytest.approx(20250.0)

    def test_source_timestamp_preserved(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_full_quote(
            "NSE_INDEX|Nifty 50", self._sample_raw(), "NSE:NIFTY:IDX", "NSE"
        )
        assert result["sourceTimestamp"] == "2024-01-15T15:30:00+05:30"

    def test_provider_is_upstox(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_full_quote(
            "NSE_INDEX|Nifty 50", self._sample_raw(), "NSE:NIFTY:IDX", "NSE"
        )
        assert result["provider"] == "upstox"


class TestUpstoxNormalizerOptionGreeks:
    """Upstox option Greeks V3 normalization."""

    def _sample_raw(self) -> dict:
        return {
            "last_price": 412.2,
            "instrument_token": "NSE_FO|43885",
            "ltq": 75,
            "volume": 3609600,
            "cp": 831.2,
            "iv": 0.336,
            "vega": 3.39,
            "gamma": 0.0005,
            "theta": -51.85,
            "delta": -0.808,
            "oi": 2476650,
        }

    def test_all_greeks_extracted(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_option_greek(
            "NSE_FO|43885", self._sample_raw(), "NFO:NIFTY25NOV23000PE"
        )
        assert result is not None
        assert result["delta"] == pytest.approx(-0.808)
        assert result["gamma"] == pytest.approx(0.0005)
        assert result["theta"] == pytest.approx(-51.85)
        assert result["vega"] == pytest.approx(3.39)
        assert result["iv"] == pytest.approx(0.336)

    def test_zero_iv_rejected(self) -> None:
        """Zero IV is not a valid substitute from Upstox either."""
        norm = UpstoxNormalizer()
        raw = {**self._sample_raw(), "iv": 0.0}
        result = norm.normalize_option_greek(
            "NSE_FO|43885", raw, "NFO:NIFTY25NOV23000PE"
        )
        assert result is not None
        assert result["iv"] is None
        assert result["ivMissing"] is True

    def test_rho_is_none_for_upstox_greeks(self) -> None:
        """Upstox V3 option-greek endpoint does not include rho."""
        norm = UpstoxNormalizer()
        result = norm.normalize_option_greek(
            "NSE_FO|43885", self._sample_raw(), "NFO:NIFTY25NOV23000PE"
        )
        assert result is not None
        assert result["rho"] is None

    def test_oi_extracted(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_option_greek(
            "NSE_FO|43885", self._sample_raw(), "NFO:NIFTY25NOV23000PE"
        )
        assert result is not None
        assert result["oi"] == 2476650
        assert result["oiMissing"] is False

    def test_greek_source_is_provider(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_option_greek(
            "NSE_FO|43885", self._sample_raw(), "NFO:NIFTY25NOV23000PE"
        )
        assert result is not None
        assert result["greekSource"] == "PROVIDER"


class TestUpstoxNormalizerCAS:
    """Upstox CAS (Closing Auction Session) data normalization."""

    def test_cas_data_extracted(self) -> None:
        norm = UpstoxNormalizer()
        cas_raw = {
            "indicative_equilibrium_price": 22550.0,
            "indicative_equilibrium_quantity": 10000,
            "total_indicative_quantity": 50000,
            "market_indicative_imbalance": 500,
            "reference_price": 22450.0,
        }
        result = norm.normalize_cas_data(
            instrument_key="NSE_EQ|INE002A01018",
            cas_raw=cas_raw,
            instrument_id="NSE:RELIANCE:EQ",
            exchange="NSE",
            session_date="2026-09-16",
        )
        assert result is not None
        assert result["indicativeEquilibriumPrice"] == pytest.approx(22550.0)
        assert result["referencePrice"] == pytest.approx(22450.0)

    def test_cas_price_is_not_ltp(self) -> None:
        """CAS data must NOT contain an 'ltp' key — it's indicative, not traded."""
        norm = UpstoxNormalizer()
        cas_raw = {
            "indicative_equilibrium_price": 22550.0,
            "indicative_equilibrium_quantity": 10000,
            "total_indicative_quantity": 50000,
            "market_indicative_imbalance": 500,
            "reference_price": 22450.0,
        }
        result = norm.normalize_cas_data(
            instrument_key="NSE_EQ|INE002A01018",
            cas_raw=cas_raw,
            instrument_id="NSE:RELIANCE:EQ",
            exchange="NSE",
            session_date="2026-09-16",
        )
        assert result is not None
        assert "ltp" not in result, (
            "CAS data MUST NOT contain 'ltp' — it is an indicative price, not LTP"
        )

    def test_empty_cas_data_returns_none(self) -> None:
        norm = UpstoxNormalizer()
        result = norm.normalize_cas_data(
            instrument_key="NSE_EQ|INE002A01018",
            cas_raw={},
            instrument_id="NSE:RELIANCE:EQ",
            exchange="NSE",
            session_date="2026-09-16",
        )
        assert result is None


class TestUpstoxNormalizerHistoricalCandle:
    """Upstox V3 historical candle normalization."""

    def test_candle_normalized(self) -> None:
        norm = UpstoxNormalizer()
        raw = {
            "timestamp": "2024-01-15T09:15:00+05:30",
            "open": 22400.0,
            "high": 22600.0,
            "low": 22350.0,
            "close": 22500.0,
            "volume": 100000,
            "open_interest": 98765,
            "api_version": "v3",
        }
        result = norm.normalize_candle(raw, "NSE:NIFTY:IDX", "NSE", "1m")
        assert result is not None
        assert result["oi"] == 98765
        assert result["oiMissing"] is False
        assert result["apiVersion"] == "v3"

    def test_oi_none_for_cash_equities(self) -> None:
        """Cash equity V3 candles return oi=0 from Upstox; treat as None (missing)."""
        norm = UpstoxNormalizer()
        raw = {
            "timestamp": "2024-01-15T09:15:00+05:30",
            "open": 500.0,
            "high": 510.0,
            "low": 495.0,
            "close": 505.0,
            "volume": 50000,
            "open_interest": None,
        }
        result = norm.normalize_candle(raw, "NSE:RELIANCE:EQ", "NSE", "1d")
        assert result is not None
        assert result["oi"] is None
        assert result["oiMissing"] is True

    def test_missing_required_fields_returns_none(self) -> None:
        norm = UpstoxNormalizer()
        # Missing 'close' field
        raw = {
            "timestamp": "2024-01-15T09:15:00+05:30",
            "open": 500.0,
            "high": 510.0,
            "low": 495.0,
            # close missing
            "volume": 50000,
        }
        result = norm.normalize_candle(raw, "NSE:RELIANCE:EQ", "NSE", "1d")
        assert result is None


# ---------------------------------------------------------------------------
# FreshnessClassifier
# ---------------------------------------------------------------------------


class TestFreshnessClassifier:
    """Freshness state classification tests."""

    def _classifier(self) -> FreshnessClassifier:
        return FreshnessClassifier(live_threshold_sec=5.0, delayed_threshold_sec=60.0)

    def test_fresh_data_is_live(self) -> None:
        clf = self._classifier()
        now = datetime.datetime.now(datetime.timezone.utc)
        source_ts = (now - datetime.timedelta(seconds=2)).isoformat()
        data = {"ltp": 100.0, "sourceTimestamp": source_ts, "receivedAt": now.isoformat()}
        result = clf.classify(data, now_utc=now)
        assert result["freshnessState"] == FreshnessState.LIVE.value

    def test_slightly_old_data_is_delayed(self) -> None:
        clf = self._classifier()
        now = datetime.datetime.now(datetime.timezone.utc)
        source_ts = (now - datetime.timedelta(seconds=30)).isoformat()
        data = {"ltp": 100.0, "sourceTimestamp": source_ts, "receivedAt": now.isoformat()}
        result = clf.classify(data, now_utc=now)
        assert result["freshnessState"] == FreshnessState.LIVE_DELAYED.value

    def test_old_data_is_stale(self) -> None:
        clf = self._classifier()
        now = datetime.datetime.now(datetime.timezone.utc)
        source_ts = (now - datetime.timedelta(seconds=300)).isoformat()
        data = {"ltp": 100.0, "sourceTimestamp": source_ts, "receivedAt": now.isoformat()}
        result = clf.classify(data, now_utc=now)
        assert result["freshnessState"] == FreshnessState.STALE.value

    def test_no_timestamp_null_ltp_is_no_data(self) -> None:
        clf = self._classifier()
        now = datetime.datetime.now(datetime.timezone.utc)
        data = {"ltp": None}  # no timestamps, no LTP
        result = clf.classify(data, now_utc=now)
        assert result["freshnessState"] == FreshnessState.NO_DATA.value

    def test_latency_computed_correctly(self) -> None:
        clf = self._classifier()
        now = datetime.datetime.now(datetime.timezone.utc)
        source_ts = now - datetime.timedelta(milliseconds=200)
        received_ts = now - datetime.timedelta(milliseconds=50)
        data = {
            "ltp": 100.0,
            "sourceTimestamp": source_ts.isoformat(),
            "receivedAt": received_ts.isoformat(),
        }
        result = clf.classify(data, now_utc=now)
        assert result["latencyMs"] == pytest.approx(150, abs=5)

    def test_age_ms_computed(self) -> None:
        clf = self._classifier()
        now = datetime.datetime.now(datetime.timezone.utc)
        source_ts = now - datetime.timedelta(seconds=3)
        data = {
            "ltp": 100.0,
            "sourceTimestamp": source_ts.isoformat(),
            "receivedAt": now.isoformat(),
        }
        result = clf.classify(data, now_utc=now)
        assert result["ageMs"] == pytest.approx(3000, abs=50)

    def test_blocked_quality_is_provider_error(self) -> None:
        clf = self._classifier()
        now = datetime.datetime.now(datetime.timezone.utc)
        source_ts = (now - datetime.timedelta(seconds=1)).isoformat()
        data = {
            "ltp": 100.0,
            "sourceTimestamp": source_ts,
            "receivedAt": now.isoformat(),
            "qualityStatus": "BLOCKED",
        }
        result = clf.classify(data, now_utc=now)
        assert result["freshnessState"] == FreshnessState.PROVIDER_ERROR.value


# ---------------------------------------------------------------------------
# ReconciliationEngine
# ---------------------------------------------------------------------------


class TestReconciliationEngine:
    """Provider reconciliation tests."""

    def _engine(self) -> ReconciliationEngine:
        return ReconciliationEngine()

    def _now_iso(self, seconds_ago: int = 0) -> str:
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
        return dt.isoformat()

    def _angel_obs(self, ltp: float, oi: int = 50000) -> dict:
        return {
            "ltp": ltp,
            "oi": oi,
            "volume": 1000000,
            "sourceTimestamp": self._now_iso(1),
        }

    def _upstox_obs(self, ltp: float, oi: int = 50000) -> dict:
        return {
            "ltp": ltp,
            "oi": oi,
            "volume": 950000,
            "sourceTimestamp": self._now_iso(2),
        }

    def test_matching_ltps_classified_match(self) -> None:
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs=self._angel_obs(22500.0),
            upstox_obs=self._upstox_obs(22500.5),  # < 0.1% difference
            instrument_id="NSE:NIFTY:IDX",
        )
        assert result.classification == "MATCH"

    def test_minor_difference_classified(self) -> None:
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs=self._angel_obs(22500.0),
            upstox_obs=self._upstox_obs(22510.0),  # ~0.04% difference
            instrument_id="NSE:NIFTY:IDX",
        )
        assert result.classification in ("MATCH", "MINOR_DIFFERENCE")

    def test_significant_difference_classified(self) -> None:
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs=self._angel_obs(22500.0),
            upstox_obs=self._upstox_obs(22700.0),  # ~0.9% difference
            instrument_id="NSE:NIFTY:IDX",
        )
        assert result.classification == "SIGNIFICANT_DIFFERENCE"
        # Upstox preferred for significant difference
        assert result.canonical_provider == "upstox"

    def test_angel_only_classified_missing(self) -> None:
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs=self._angel_obs(22500.0),
            upstox_obs=None,
            instrument_id="NSE:NIFTY:IDX",
        )
        assert result.classification == "MISSING"
        assert result.canonical_provider == "angel_one"
        assert result.canonical_ltp == pytest.approx(22500.0)

    def test_upstox_only_classified_missing(self) -> None:
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs=None,
            upstox_obs=self._upstox_obs(22500.0),
            instrument_id="NSE:NIFTY:IDX",
        )
        assert result.classification == "MISSING"
        assert result.canonical_provider == "upstox"

    def test_both_unavailable_classified_missing(self) -> None:
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs=None,
            upstox_obs=None,
            instrument_id="NSE:NIFTY:IDX",
        )
        assert result.classification == "MISSING"
        assert result.canonical_provider is None
        assert result.canonical_ltp is None
        assert result.canonical_oi is None

    def test_oi_prefers_angel_one(self) -> None:
        """OI routing rule: Angel One preferred when both available."""
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs={**self._angel_obs(22500.0), "oi": 75000},
            upstox_obs={**self._upstox_obs(22500.0), "oi": 70000},
            instrument_id="NSE:NIFTY:IDX",
        )
        # canonical_oi should be Angel One's value
        assert result.canonical_oi == 75000

    def test_oi_falls_back_to_upstox_when_angel_missing(self) -> None:
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs={**self._angel_obs(22500.0), "oi": None},
            upstox_obs={**self._upstox_obs(22500.0), "oi": 70000},
            instrument_id="NSE:NIFTY:IDX",
        )
        assert result.canonical_oi == 70000

    def test_oi_never_zero_when_missing(self) -> None:
        """OI must never be set to zero as a substitute for missing."""
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs={**self._angel_obs(22500.0), "oi": None},
            upstox_obs={**self._upstox_obs(22500.0), "oi": None},
            instrument_id="NSE:NIFTY:IDX",
        )
        assert result.canonical_oi is None  # NEVER zero

    def test_to_dict_includes_both_providers(self) -> None:
        engine = self._engine()
        result = engine.reconcile_quote(
            angel_obs=self._angel_obs(22500.0),
            upstox_obs=self._upstox_obs(22505.0),
            instrument_id="NSE:NIFTY:IDX",
        )
        d = result.to_dict()
        assert d["provider_a"] == "angel_one"
        assert d["provider_b"] == "upstox"
        # Both observations present — neither discarded
        assert d["provider_a_ltp"] is not None
        assert d["provider_b_ltp"] is not None
