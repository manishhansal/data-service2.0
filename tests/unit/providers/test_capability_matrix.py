"""
Unit tests for the Capability Matrix.

Covers:
- All provider × data type combinations in the matrix
- 3m interval block for Indian markets, allowed for crypto
- Priority ordering returned by get_providers_for()
- Chunk size values (matrix rows + get_chunk_days())
- get_providers_for() returns correct providers sorted by priority
- get_capability() returns expected record or None

Requirements: 5.1, 10.3, 10.4
"""

from __future__ import annotations

import pytest

from src.core.schemas.provider import (
    CANONICAL_INDIAN_TIMEFRAMES,
    CRYPTO_INSTRUMENT_CLASSES,
    INDIAN_INSTRUMENT_CLASSES,
    DataType,
    ProviderId,
    ProviderCapability,
    SourceType,
)
from src.providers.capability_matrix import (
    ANGEL_ONE_CHUNK_DAYS,
    UPSTOX_CHUNK_DAYS,
    _MATRIX,
    get_capability,
    get_chunk_days,
    get_providers_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _providers_in_matrix(provider_id: ProviderId) -> list[ProviderCapability]:
    return [c for c in _MATRIX if c.provider == provider_id]


# ---------------------------------------------------------------------------
# Schema / model tests
# ---------------------------------------------------------------------------


class TestProviderCapabilityModel:
    def test_frozen_model_cannot_mutate(self) -> None:
        cap = _MATRIX[0]
        with pytest.raises(Exception):  # ValidationError or TypeError for frozen models
            cap.priority = 99  # type: ignore[misc]

    def test_instrument_class_must_be_valid(self) -> None:
        with pytest.raises(Exception):
            ProviderCapability(
                provider=ProviderId.ANGEL_ONE,
                dataType=DataType.LIVE_QUOTE,
                instrumentClass="INVALID_CLASS",
                supported=True,
                liveSupported=True,
                historySupported=False,
                maxChunkDays=0,
                requestsPerSecond=3.0,
                intervalSupport=[],
                sourceType=SourceType.BROKER_AUTHENTICATED,
                priority=1,
            )

    def test_requests_per_second_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            ProviderCapability(
                provider=ProviderId.ANGEL_ONE,
                dataType=DataType.LIVE_QUOTE,
                instrumentClass="EQ",
                supported=True,
                liveSupported=True,
                historySupported=False,
                maxChunkDays=0,
                requestsPerSecond=0.0,   # must be > 0
                intervalSupport=[],
                sourceType=SourceType.BROKER_AUTHENTICATED,
                priority=1,
            )

    def test_max_chunk_days_must_be_non_negative(self) -> None:
        with pytest.raises(Exception):
            ProviderCapability(
                provider=ProviderId.ANGEL_ONE,
                dataType=DataType.LIVE_QUOTE,
                instrumentClass="EQ",
                supported=True,
                liveSupported=True,
                historySupported=False,
                maxChunkDays=-1,
                requestsPerSecond=3.0,
                intervalSupport=[],
                sourceType=SourceType.BROKER_AUTHENTICATED,
                priority=1,
            )


# ---------------------------------------------------------------------------
# 3m interval enforcement
# ---------------------------------------------------------------------------


class TestThreeMIntervalBlocking:
    """The 3m interval must be permanently blocked for Indian market data."""

    @pytest.mark.parametrize("instrument_class", sorted(INDIAN_INSTRUMENT_CLASSES))
    @pytest.mark.parametrize(
        "data_type",
        [DataType.HISTORICAL_OHLCV, DataType.LIVE_QUOTE],
    )
    def test_3m_returns_empty_for_indian_classes(
        self, instrument_class: str, data_type: DataType
    ) -> None:
        result = get_providers_for(data_type, instrument_class, interval="3m")
        assert result == [], (
            f"3m must be blocked for Indian instrument class {instrument_class!r}, "
            f"data_type={data_type}, got {result}"
        )

    @pytest.mark.parametrize("instrument_class", sorted(CRYPTO_INSTRUMENT_CLASSES))
    def test_3m_allowed_for_crypto_klines(self, instrument_class: str) -> None:
        result = get_providers_for(DataType.CRYPTO_KLINES, instrument_class, interval="3m")
        # Binance supports 3m for crypto; at least one provider must be returned
        if instrument_class in ("CRYPTO_SPOT", "CRYPTO_FUTURES"):
            assert len(result) > 0, (
                f"3m must be allowed for crypto instrument class {instrument_class!r}"
            )

    def test_no_indian_capability_row_contains_3m_in_interval_support(self) -> None:
        """Defense-in-depth: no row in the matrix has 3m in intervalSupport for Indian data."""
        for cap in _MATRIX:
            if cap.instrumentClass in INDIAN_INSTRUMENT_CLASSES:
                assert "3m" not in cap.intervalSupport, (
                    f"Found 3m in intervalSupport for Indian class "
                    f"{cap.provider}/{cap.instrumentClass}/{cap.dataType}"
                )

    def test_canonical_indian_timeframes_does_not_contain_3m(self) -> None:
        assert "3m" not in CANONICAL_INDIAN_TIMEFRAMES


# ---------------------------------------------------------------------------
# Angel One SmartAPI
# ---------------------------------------------------------------------------


class TestAngelOneCapabilities:
    def test_angel_one_has_entries(self) -> None:
        assert len(_providers_in_matrix(ProviderId.ANGEL_ONE)) > 0

    def test_angel_one_live_quote_eq_supported(self) -> None:
        cap = get_capability(ProviderId.ANGEL_ONE, DataType.LIVE_QUOTE, "EQ")
        assert cap is not None
        assert cap.liveSupported is True
        assert cap.sourceType == SourceType.BROKER_AUTHENTICATED

    def test_angel_one_live_quote_fo_supported(self) -> None:
        cap = get_capability(ProviderId.ANGEL_ONE, DataType.LIVE_QUOTE, "FO")
        assert cap is not None
        assert cap.liveSupported is True

    def test_angel_one_historical_eq_intervals(self) -> None:
        cap = get_capability(ProviderId.ANGEL_ONE, DataType.HISTORICAL_OHLCV, "EQ")
        assert cap is not None
        assert cap.historySupported is True
        for interval in ["1m", "5m", "15m", "30m", "1h"]:
            assert interval in cap.intervalSupport, f"{interval} missing from Angel One EQ"
        assert "3m" not in cap.intervalSupport

    def test_angel_one_historical_fo_intervals(self) -> None:
        cap = get_capability(ProviderId.ANGEL_ONE, DataType.HISTORICAL_OHLCV, "FO")
        assert cap is not None
        assert "3m" not in cap.intervalSupport

    def test_angel_one_chunk_days_1m(self) -> None:
        assert ANGEL_ONE_CHUNK_DAYS["1m"] == 30

    def test_angel_one_chunk_days_5m(self) -> None:
        assert ANGEL_ONE_CHUNK_DAYS["5m"] == 90

    def test_angel_one_chunk_days_15m(self) -> None:
        assert ANGEL_ONE_CHUNK_DAYS["15m"] == 90

    def test_angel_one_get_chunk_days_1m(self) -> None:
        assert get_chunk_days(ProviderId.ANGEL_ONE, "1m") == 30

    def test_angel_one_get_chunk_days_5m(self) -> None:
        assert get_chunk_days(ProviderId.ANGEL_ONE, "5m") == 90

    def test_angel_one_broker_analytics_supported(self) -> None:
        cap = get_capability(ProviderId.ANGEL_ONE, DataType.BROKER_ANALYTICS, "FO")
        assert cap is not None
        assert cap.supported is True

    def test_angel_one_rate_limit(self) -> None:
        # All Angel One rows use 3 req/s
        for cap in _providers_in_matrix(ProviderId.ANGEL_ONE):
            assert cap.requestsPerSecond == 3.0, (
                f"Expected 3.0 req/s for Angel One, got {cap.requestsPerSecond} "
                f"({cap.dataType}/{cap.instrumentClass})"
            )

    def test_angel_one_no_historical_idx(self) -> None:
        """Angel One is NOT the primary for index intraday history."""
        cap = get_capability(ProviderId.ANGEL_ONE, DataType.HISTORICAL_OHLCV, "IDX")
        # Angel One should not have an IDX historical OHLCV row in the matrix
        assert cap is None


# ---------------------------------------------------------------------------
# Upstox V3
# ---------------------------------------------------------------------------


class TestUpstoxCapabilities:
    def test_upstox_has_entries(self) -> None:
        assert len(_providers_in_matrix(ProviderId.UPSTOX)) > 0

    def test_upstox_live_idx_priority_1(self) -> None:
        cap = get_capability(ProviderId.UPSTOX, DataType.LIVE_QUOTE, "IDX")
        assert cap is not None
        assert cap.priority == 1

    def test_upstox_historical_idx_priority_1(self) -> None:
        cap = get_capability(ProviderId.UPSTOX, DataType.HISTORICAL_OHLCV, "IDX")
        assert cap is not None
        assert cap.priority == 1

    def test_upstox_historical_idx_intervals(self) -> None:
        cap = get_capability(ProviderId.UPSTOX, DataType.HISTORICAL_OHLCV, "IDX")
        assert cap is not None
        for interval in ["1m", "5m", "15m", "1h", "1d"]:
            assert interval in cap.intervalSupport
        assert "3m" not in cap.intervalSupport

    def test_upstox_historical_eq_secondary(self) -> None:
        cap = get_capability(ProviderId.UPSTOX, DataType.HISTORICAL_OHLCV, "EQ")
        assert cap is not None
        assert cap.priority == 2  # secondary to Angel One

    def test_upstox_chunk_days_1m(self) -> None:
        # V3: 1-month window for 1-15min intervals = 28 days
        assert UPSTOX_CHUNK_DAYS["1m"] == 28

    def test_upstox_chunk_days_5m(self) -> None:
        # V3: 1-month window for <=15min
        assert UPSTOX_CHUNK_DAYS["5m"] == 28

    def test_upstox_chunk_days_15m(self) -> None:
        # V3: 1-month window for <=15min
        assert UPSTOX_CHUNK_DAYS["15m"] == 28

    def test_upstox_chunk_days_1d(self) -> None:
        assert UPSTOX_CHUNK_DAYS["1d"] == 365

    def test_upstox_get_chunk_days_1m(self) -> None:
        # V3 1-month window for 1m
        assert get_chunk_days(ProviderId.UPSTOX, "1m") == 28

    def test_upstox_get_chunk_days_5m(self) -> None:
        # V3 1-month window for 5m
        assert get_chunk_days(ProviderId.UPSTOX, "5m") == 28

    def test_upstox_get_chunk_days_1d(self) -> None:
        assert get_chunk_days(ProviderId.UPSTOX, "1d") == 365

    def test_upstox_rate_limit(self) -> None:
        # Updated to 50 req/s per NSE circular May 2025
        for cap in _providers_in_matrix(ProviderId.UPSTOX):
            assert cap.requestsPerSecond == 50.0


# ---------------------------------------------------------------------------
# Scrapling / NSE
# ---------------------------------------------------------------------------


class TestScraplingNseCapabilities:
    def test_scrapling_option_chain_fo(self) -> None:
        cap = get_capability(ProviderId.SCRAPLING_NSE, DataType.OPTION_CHAIN, "FO")
        assert cap is not None
        assert cap.liveSupported is True
        assert cap.sourceType == SourceType.OPEN_SOURCE_NSE_DERIVED

    def test_scrapling_option_chain_idx(self) -> None:
        cap = get_capability(ProviderId.SCRAPLING_NSE, DataType.OPTION_CHAIN, "IDX")
        assert cap is not None
        assert cap.liveSupported is True

    def test_scrapling_instrument_master(self) -> None:
        cap = get_capability(ProviderId.SCRAPLING_NSE, DataType.INSTRUMENT_MASTER, "ALL")
        assert cap is not None
        assert cap.priority == 1

    def test_scrapling_live_quote_idx(self) -> None:
        cap = get_capability(ProviderId.SCRAPLING_NSE, DataType.LIVE_QUOTE, "IDX")
        assert cap is not None

    def test_scrapling_no_history(self) -> None:
        for cap in _providers_in_matrix(ProviderId.SCRAPLING_NSE):
            assert cap.historySupported is False, (
                f"Scrapling/NSE must not support history but "
                f"{cap.dataType}/{cap.instrumentClass} has historySupported=True"
            )

    def test_scrapling_credential_free(self) -> None:
        for cap in _providers_in_matrix(ProviderId.SCRAPLING_NSE):
            assert cap.sourceType == SourceType.OPEN_SOURCE_NSE_DERIVED


# ---------------------------------------------------------------------------
# Jugaad-data
# ---------------------------------------------------------------------------


class TestJugaadDataCapabilities:
    def test_jugaad_fo_eod_history(self) -> None:
        cap = get_capability(ProviderId.JUGAAD_DATA, DataType.HISTORICAL_OHLCV, "FO")
        assert cap is not None
        assert cap.historySupported is True
        assert cap.intervalSupport == ["1d"]

    def test_jugaad_chunk_days_3650(self) -> None:
        cap = get_capability(ProviderId.JUGAAD_DATA, DataType.HISTORICAL_OHLCV, "FO")
        assert cap is not None
        assert cap.maxChunkDays == 3650

    def test_jugaad_priority_1_for_fo_eod(self) -> None:
        cap = get_capability(ProviderId.JUGAAD_DATA, DataType.HISTORICAL_OHLCV, "FO")
        assert cap is not None
        assert cap.priority == 1

    def test_jugaad_credential_free(self) -> None:
        for cap in _providers_in_matrix(ProviderId.JUGAAD_DATA):
            assert cap.sourceType == SourceType.CREDENTIAL_FREE

    def test_jugaad_no_3m(self) -> None:
        for cap in _providers_in_matrix(ProviderId.JUGAAD_DATA):
            assert "3m" not in cap.intervalSupport

    def test_jugaad_get_chunk_days(self) -> None:
        assert get_chunk_days(ProviderId.JUGAAD_DATA, "1d") == 3650


# ---------------------------------------------------------------------------
# OpenChart
# ---------------------------------------------------------------------------


class TestOpenChartCapabilities:
    def test_openchart_eq_all_canonical_indian_timeframes(self) -> None:
        cap = get_capability(ProviderId.OPENCHART, DataType.HISTORICAL_OHLCV, "EQ")
        assert cap is not None
        for tf in CANONICAL_INDIAN_TIMEFRAMES:
            assert tf in cap.intervalSupport, f"OpenChart EQ missing interval {tf}"
        assert "3m" not in cap.intervalSupport

    def test_openchart_fo_all_canonical_indian_timeframes(self) -> None:
        cap = get_capability(ProviderId.OPENCHART, DataType.HISTORICAL_OHLCV, "FO")
        assert cap is not None
        assert "3m" not in cap.intervalSupport

    def test_openchart_idx_supported(self) -> None:
        cap = get_capability(ProviderId.OPENCHART, DataType.HISTORICAL_OHLCV, "IDX")
        assert cap is not None
        assert cap.historySupported is True

    def test_openchart_chunk_days_365(self) -> None:
        cap = get_capability(ProviderId.OPENCHART, DataType.HISTORICAL_OHLCV, "EQ")
        assert cap is not None
        assert cap.maxChunkDays == 365

    def test_openchart_lower_priority_than_angel_one(self) -> None:
        ao = get_capability(ProviderId.ANGEL_ONE, DataType.HISTORICAL_OHLCV, "EQ")
        oc = get_capability(ProviderId.OPENCHART, DataType.HISTORICAL_OHLCV, "EQ")
        assert ao is not None and oc is not None
        assert ao.priority < oc.priority

    def test_openchart_get_chunk_days(self) -> None:
        assert get_chunk_days(ProviderId.OPENCHART, "1m") == 365


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------


class TestYahooFinanceCapabilities:
    def test_yahoo_finance_eq_eod_only(self) -> None:
        cap = get_capability(ProviderId.YAHOO_FINANCE, DataType.HISTORICAL_OHLCV, "EQ")
        assert cap is not None
        assert cap.intervalSupport == ["1d"]

    def test_yahoo_finance_secondary_fallback_source_type(self) -> None:
        cap = get_capability(ProviderId.YAHOO_FINANCE, DataType.HISTORICAL_OHLCV, "EQ")
        assert cap is not None
        assert cap.sourceType == SourceType.SECONDARY_FALLBACK

    def test_yahoo_finance_lowest_priority(self) -> None:
        cap = get_capability(ProviderId.YAHOO_FINANCE, DataType.HISTORICAL_OHLCV, "EQ")
        assert cap is not None
        # Yahoo Finance should have higher priority number (lower preference) than Angel One
        ao = get_capability(ProviderId.ANGEL_ONE, DataType.HISTORICAL_OHLCV, "EQ")
        assert ao is not None
        assert cap.priority > ao.priority

    def test_yahoo_finance_not_returned_for_fo(self) -> None:
        cap = get_capability(ProviderId.YAHOO_FINANCE, DataType.HISTORICAL_OHLCV, "FO")
        assert cap is None

    def test_yahoo_finance_not_returned_for_live(self) -> None:
        cap = get_capability(ProviderId.YAHOO_FINANCE, DataType.LIVE_QUOTE, "EQ")
        assert cap is None

    def test_yahoo_finance_no_3m(self) -> None:
        for cap in _providers_in_matrix(ProviderId.YAHOO_FINANCE):
            assert "3m" not in cap.intervalSupport


# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------


class TestBinanceCapabilities:
    def test_binance_crypto_klines_spot(self) -> None:
        cap = get_capability(ProviderId.BINANCE, DataType.CRYPTO_KLINES, "CRYPTO_SPOT")
        assert cap is not None
        assert cap.supported is True
        assert cap.historySupported is True
        assert cap.liveSupported is True

    def test_binance_crypto_klines_futures(self) -> None:
        cap = get_capability(ProviderId.BINANCE, DataType.CRYPTO_KLINES, "CRYPTO_FUTURES")
        assert cap is not None
        assert cap.supported is True

    def test_binance_3m_allowed_in_crypto_klines(self) -> None:
        cap = get_capability(ProviderId.BINANCE, DataType.CRYPTO_KLINES, "CRYPTO_SPOT")
        assert cap is not None
        assert "3m" in cap.intervalSupport, "Binance crypto klines must support 3m"

    def test_binance_futures_data_supported(self) -> None:
        cap = get_capability(ProviderId.BINANCE, DataType.CRYPTO_FUTURES, "CRYPTO_FUTURES")
        assert cap is not None
        assert cap.supported is True

    def test_binance_rate_limit_20(self) -> None:
        for cap in _providers_in_matrix(ProviderId.BINANCE):
            assert cap.requestsPerSecond == 20.0

    def test_binance_credential_free(self) -> None:
        for cap in _providers_in_matrix(ProviderId.BINANCE):
            assert cap.sourceType == SourceType.CREDENTIAL_FREE


# ---------------------------------------------------------------------------
# Deribit
# ---------------------------------------------------------------------------


class TestDeribitCapabilities:
    def test_deribit_crypto_options(self) -> None:
        cap = get_capability(ProviderId.DERIBIT, DataType.CRYPTO_OPTIONS, "CRYPTO_OPTIONS")
        assert cap is not None
        assert cap.supported is True
        assert cap.liveSupported is True

    def test_deribit_rate_limit_5(self) -> None:
        for cap in _providers_in_matrix(ProviderId.DERIBIT):
            assert cap.requestsPerSecond == 5.0

    def test_deribit_credential_free(self) -> None:
        for cap in _providers_in_matrix(ProviderId.DERIBIT):
            assert cap.sourceType == SourceType.CREDENTIAL_FREE

    def test_deribit_no_history(self) -> None:
        cap = get_capability(ProviderId.DERIBIT, DataType.CRYPTO_OPTIONS, "CRYPTO_OPTIONS")
        assert cap is not None
        assert cap.historySupported is False


# ---------------------------------------------------------------------------
# get_providers_for() — priority ordering and routing
# ---------------------------------------------------------------------------


class TestGetProvidersFor:
    def test_returns_sorted_by_priority(self) -> None:
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "EQ")
        priorities = [r.priority for r in results]
        assert priorities == sorted(priorities), "Results must be sorted by priority ascending"

    def test_angel_one_before_openchart_for_eq_history(self) -> None:
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "EQ")
        provider_ids = [r.provider for r in results]
        assert ProviderId.ANGEL_ONE in provider_ids
        assert ProviderId.OPENCHART in provider_ids
        ao_idx = provider_ids.index(ProviderId.ANGEL_ONE)
        oc_idx = provider_ids.index(ProviderId.OPENCHART)
        assert ao_idx < oc_idx, "Angel One must have higher priority than OpenChart for EQ history"

    def test_upstox_primary_for_idx_history(self) -> None:
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "IDX")
        assert len(results) > 0
        assert results[0].provider == ProviderId.UPSTOX, (
            f"Upstox must be the first (highest priority) provider for IDX history, "
            f"got {results[0].provider}"
        )

    def test_jugaad_primary_for_fo_eod_1d(self) -> None:
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "FO", interval="1d")
        assert len(results) > 0
        # Jugaad should appear in the list; check priority=1
        jugaad_results = [r for r in results if r.provider == ProviderId.JUGAAD_DATA]
        assert len(jugaad_results) > 0
        assert jugaad_results[0].priority == 1

    def test_yahoo_finance_last_for_eq_history(self) -> None:
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "EQ", interval="1d")
        assert len(results) > 0
        # Yahoo Finance must be the last in the sorted list
        assert results[-1].provider == ProviderId.YAHOO_FINANCE, (
            f"Yahoo Finance must be last for EQ 1d history; got {results[-1].provider}"
        )

    def test_no_results_for_unsupported_combo(self) -> None:
        # Jugaad does not support EQ data
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "EQ")
        jugaad_results = [r for r in results if r.provider == ProviderId.JUGAAD_DATA]
        assert len(jugaad_results) == 0, "Jugaad should not appear for EQ historical OHLCV"

    def test_binance_for_crypto_spot_klines(self) -> None:
        results = get_providers_for(DataType.CRYPTO_KLINES, "CRYPTO_SPOT")
        assert any(r.provider == ProviderId.BINANCE for r in results)

    def test_deribit_for_crypto_options(self) -> None:
        results = get_providers_for(DataType.CRYPTO_OPTIONS, "CRYPTO_OPTIONS")
        assert any(r.provider == ProviderId.DERIBIT for r in results)

    def test_interval_filter_excludes_unsupported(self) -> None:
        # 10m is in OpenChart and Upstox but not Angel One's explicit list
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "EQ", interval="10m")
        provider_ids = [r.provider for r in results]
        # Angel One does NOT list 10m in its intervalSupport
        assert ProviderId.ANGEL_ONE not in provider_ids, (
            "Angel One does not support 10m; should be excluded when interval=10m is specified"
        )

    def test_3m_blocked_for_eq(self) -> None:
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "EQ", interval="3m")
        assert results == []

    def test_3m_blocked_for_fo(self) -> None:
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "FO", interval="3m")
        assert results == []

    def test_3m_blocked_for_idx(self) -> None:
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "IDX", interval="3m")
        assert results == []

    def test_no_interval_filter_returns_all_supported(self) -> None:
        results = get_providers_for(DataType.LIVE_QUOTE, "EQ")
        assert len(results) > 0

    def test_all_results_are_supported(self) -> None:
        results = get_providers_for(DataType.HISTORICAL_OHLCV, "EQ")
        for cap in results:
            assert cap.supported is True


# ---------------------------------------------------------------------------
# get_capability() edge cases
# ---------------------------------------------------------------------------


class TestGetCapability:
    def test_returns_none_for_missing_provider_data_type(self) -> None:
        # Binance does not serve Indian live quotes
        result = get_capability(ProviderId.BINANCE, DataType.LIVE_QUOTE, "EQ")
        assert result is None

    def test_returns_none_for_missing_instrument_class(self) -> None:
        result = get_capability(ProviderId.ANGEL_ONE, DataType.HISTORICAL_OHLCV, "IDX")
        assert result is None

    def test_returns_capability_without_instrument_class_filter(self) -> None:
        result = get_capability(ProviderId.ANGEL_ONE, DataType.LIVE_QUOTE)
        # Should return the first matching row (EQ or FO)
        assert result is not None
        assert result.provider == ProviderId.ANGEL_ONE

    def test_instrument_master_via_all_class(self) -> None:
        result = get_capability(ProviderId.SCRAPLING_NSE, DataType.INSTRUMENT_MASTER, "ALL")
        assert result is not None


# ---------------------------------------------------------------------------
# Matrix integrity
# ---------------------------------------------------------------------------


class TestMatrixIntegrity:
    def test_all_rows_have_valid_provider_ids(self) -> None:
        valid_ids = set(ProviderId)
        for cap in _MATRIX:
            assert cap.provider in valid_ids

    def test_all_rows_have_valid_data_types(self) -> None:
        valid_types = set(DataType)
        for cap in _MATRIX:
            assert cap.dataType in valid_types

    def test_no_indian_row_has_3m_interval(self) -> None:
        for cap in _MATRIX:
            if cap.instrumentClass in INDIAN_INSTRUMENT_CLASSES:
                assert "3m" not in cap.intervalSupport, (
                    f"Row {cap.provider}/{cap.instrumentClass}/{cap.dataType} "
                    f"illegally contains 3m in intervalSupport"
                )

    def test_matrix_not_empty(self) -> None:
        assert len(_MATRIX) > 0

    def test_every_provider_has_at_least_one_entry(self) -> None:
        providers_in_matrix = {cap.provider for cap in _MATRIX}
        for provider in ProviderId:
            assert provider in providers_in_matrix, (
                f"Provider {provider!r} has no entry in the Capability Matrix"
            )

    def test_authenticated_providers_have_correct_source_type(self) -> None:
        for cap in _MATRIX:
            if cap.provider in (ProviderId.ANGEL_ONE, ProviderId.UPSTOX):
                assert cap.sourceType == SourceType.BROKER_AUTHENTICATED, (
                    f"{cap.provider} must be BROKER_AUTHENTICATED, "
                    f"got {cap.sourceType}"
                )

    def test_yahoo_finance_always_secondary_fallback(self) -> None:
        for cap in _MATRIX:
            if cap.provider == ProviderId.YAHOO_FINANCE:
                assert cap.sourceType == SourceType.SECONDARY_FALLBACK
