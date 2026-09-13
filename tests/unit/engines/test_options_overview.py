"""
tests/unit/engines/test_options_overview.py

Unit tests for DeribitOptionsOverview, OptionContractSummary, and
OptionsOverviewResult.

Test coverage:
  - OptionContractSummary field validation and bid_ask_spread auto-computation
  - OptionsOverviewResult field validation and put_call_oi_ratio
  - DeribitOptionsOverview.compute() — full happy-path with mocked DeribitClient
  - Null-preservation: mark_iv, open_interest, best_bid, best_ask are never
    zero-substituted (Requirements 14.5, 6.4, 6.6)
  - put_call_oi_ratio is None when total_call_oi == 0 (Requirement 14.4)
  - Instruments with unrecognised option_type are silently skipped
  - Ticker fetch failures are silently skipped (contract count decreases)
  - bid_ask_spread is None when either bid or ask is None / <= 0
  - computed_at is a non-empty UTC ISO-8601 string with Z suffix
  - Currency is uppercased; only BTC/ETH/SOL accepted by OptionsOverviewResult
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.engines.options_overview import (
    DeribitOptionsOverview,
    OptionContractSummary,
    OptionsOverviewResult,
    _aggregate_oi,
    _compute_pcr,
    _expiry_to_date_str,
    _extract_float,
    _resolve_option_type,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_ISO8601_Z_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$"
)


def _make_instrument(
    name: str = "BTC-27DEC24-100000-C",
    option_type: str = "call",
    strike: float = 100_000.0,
    expiration_timestamp: int = 1_735_286_400_000,  # 2024-12-27 08:00 UTC
) -> dict[str, Any]:
    return {
        "instrument_name": name,
        "option_type": option_type,
        "strike": strike,
        "expiration_timestamp": expiration_timestamp,
    }


def _make_ticker(
    mark_price: float | None = 0.05,
    mark_iv: float | None = 80.0,
    open_interest: float | None = 1_500.0,
    best_bid_price: float | None = 0.049,
    best_ask_price: float | None = 0.051,
) -> dict[str, Any]:
    """Build a minimal Deribit-style ticker dict."""
    return {
        "mark_price": mark_price,
        "mark_iv": mark_iv,
        "open_interest": open_interest,
        "best_bid_price": best_bid_price,
        "best_ask_price": best_ask_price,
    }


def _make_mock_client(
    instruments: list[dict[str, Any]] | None = None,
    index_price_value: float = 95_000.0,
    tickers: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Return a MagicMock that mimics DeribitClient with async methods."""
    client = MagicMock()

    if instruments is None:
        instruments = [_make_instrument()]

    if tickers is None:
        tickers = [_make_ticker() for _ in instruments]

    client.get_instruments = AsyncMock(return_value=instruments)
    client.get_index_price = AsyncMock(
        return_value={"index_name": "btc_usd", "index_price": index_price_value}
    )
    # Return tickers in order; if there are more instruments than tickers supplied,
    # cycle back using side_effect with a list.
    client.get_ticker = AsyncMock(side_effect=tickers)

    return client


# ---------------------------------------------------------------------------
# OptionContractSummary — model tests
# ---------------------------------------------------------------------------


class TestOptionContractSummary:
    """Tests for OptionContractSummary Pydantic model."""

    def test_happy_path_call_with_all_fields(self) -> None:
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
            mark_price=0.05,
            mark_iv=80.0,
            open_interest=1_500.0,
            best_bid=0.049,
            best_ask=0.051,
        )
        assert contract.instrument_name == "BTC-27DEC24-100000-C"
        assert contract.expiry == "2024-12-27"
        assert contract.strike == 100_000.0
        assert contract.option_type == "call"
        assert contract.mark_price == 0.05
        assert contract.mark_iv == 80.0
        assert contract.open_interest == 1_500.0
        assert contract.best_bid == 0.049
        assert contract.best_ask == 0.051
        # bid_ask_spread auto-computed: 0.051 - 0.049 = 0.002
        assert contract.bid_ask_spread == pytest.approx(0.002, abs=1e-9)

    def test_happy_path_put_contract(self) -> None:
        contract = OptionContractSummary(
            instrument_name="ETH-27DEC24-3000-P",
            expiry="2024-12-27",
            strike=3_000.0,
            option_type="put",
            mark_price=0.01,
            mark_iv=90.5,
            open_interest=500.0,
            best_bid=0.009,
            best_ask=0.011,
        )
        assert contract.option_type == "put"
        assert contract.bid_ask_spread == pytest.approx(0.002, abs=1e-9)

    def test_mark_iv_none_preserved(self) -> None:
        """mark_iv=None must be preserved; zero must not be substituted."""
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
            mark_iv=None,
        )
        assert contract.mark_iv is None

    def test_open_interest_none_preserved(self) -> None:
        """open_interest=None must be preserved; zero must not be substituted."""
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
            open_interest=None,
        )
        assert contract.open_interest is None

    def test_best_bid_none_preserved(self) -> None:
        """best_bid=None must be preserved; zero must not be substituted."""
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
            best_bid=None,
            best_ask=0.05,
        )
        assert contract.best_bid is None
        assert contract.bid_ask_spread is None

    def test_best_ask_none_preserved(self) -> None:
        """best_ask=None must be preserved; zero must not be substituted."""
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
            best_bid=0.04,
            best_ask=None,
        )
        assert contract.best_ask is None
        assert contract.bid_ask_spread is None

    def test_bid_ask_spread_none_when_both_none(self) -> None:
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
            best_bid=None,
            best_ask=None,
        )
        assert contract.bid_ask_spread is None

    def test_bid_ask_spread_none_when_bid_zero(self) -> None:
        """bid_ask_spread must be None when bid is 0 (not a valid quote)."""
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
            best_bid=0.0,
            best_ask=0.051,
        )
        assert contract.bid_ask_spread is None

    def test_bid_ask_spread_none_when_ask_zero(self) -> None:
        """bid_ask_spread must be None when ask is 0."""
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
            best_bid=0.049,
            best_ask=0.0,
        )
        assert contract.bid_ask_spread is None

    def test_invalid_option_type_raises(self) -> None:
        with pytest.raises(Exception):
            OptionContractSummary(
                instrument_name="BTC-27DEC24-100000-X",
                expiry="2024-12-27",
                strike=100_000.0,
                option_type="unknown",
            )

    def test_model_is_frozen(self) -> None:
        """The model must be immutable (frozen=True)."""
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
        )
        with pytest.raises(Exception):
            contract.strike = 200_000.0  # type: ignore[misc]

    def test_mark_price_none(self) -> None:
        """mark_price defaults to None and is preserved."""
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
        )
        assert contract.mark_price is None


# ---------------------------------------------------------------------------
# OptionsOverviewResult — model tests
# ---------------------------------------------------------------------------


class TestOptionsOverviewResult:
    """Tests for OptionsOverviewResult Pydantic model."""

    def _make_result(self, **overrides: Any) -> OptionsOverviewResult:
        defaults: dict[str, Any] = {
            "currency": "BTC",
            "index_price": 95_000.0,
            "contracts": [],
            "total_call_oi": 10_000.0,
            "total_put_oi": 8_000.0,
            "put_call_oi_ratio": 0.8,
            "computed_at": "2024-12-27T10:00:00.000000Z",
        }
        defaults.update(overrides)
        return OptionsOverviewResult(**defaults)

    def test_happy_path(self) -> None:
        result = self._make_result()
        assert result.currency == "BTC"
        assert result.index_price == 95_000.0
        assert result.total_call_oi == 10_000.0
        assert result.total_put_oi == 8_000.0
        assert result.put_call_oi_ratio == pytest.approx(0.8)

    def test_put_call_ratio_none_when_no_calls(self) -> None:
        """put_call_oi_ratio must accept None (not raise a validation error)."""
        result = self._make_result(total_call_oi=0.0, put_call_oi_ratio=None)
        assert result.put_call_oi_ratio is None

    def test_currency_uppercased(self) -> None:
        result = self._make_result(currency="eth")
        assert result.currency == "ETH"

    def test_invalid_currency_raises(self) -> None:
        with pytest.raises(Exception):
            self._make_result(currency="DOGE")

    def test_contracts_list_preserved(self) -> None:
        contract = OptionContractSummary(
            instrument_name="BTC-27DEC24-100000-C",
            expiry="2024-12-27",
            strike=100_000.0,
            option_type="call",
        )
        result = self._make_result(contracts=[contract])
        assert len(result.contracts) == 1
        assert result.contracts[0].instrument_name == "BTC-27DEC24-100000-C"

    def test_result_is_frozen(self) -> None:
        result = self._make_result()
        with pytest.raises(Exception):
            result.index_price = 99_000.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


class TestExtractFloat:
    def test_returns_float_for_numeric(self) -> None:
        assert _extract_float({"x": 1.5}, "x") == 1.5

    def test_returns_none_for_missing_key(self) -> None:
        assert _extract_float({}, "x") is None

    def test_returns_none_for_none_value(self) -> None:
        assert _extract_float({"x": None}, "x") is None

    def test_returns_none_for_non_numeric_string(self) -> None:
        assert _extract_float({"x": "N/A"}, "x") is None

    def test_converts_int_to_float(self) -> None:
        result = _extract_float({"x": 42}, "x")
        assert result == 42.0
        assert isinstance(result, float)

    def test_zero_is_returned_as_float_zero(self) -> None:
        """Explicitly provided zero must be returned as 0.0, not None."""
        assert _extract_float({"x": 0}, "x") == 0.0

    def test_zero_float_returned_not_none(self) -> None:
        assert _extract_float({"x": 0.0}, "x") == 0.0


class TestResolveOptionType:
    def test_call_string(self) -> None:
        assert _resolve_option_type("call", "BTC-27DEC24-100000-C") == "call"

    def test_put_string(self) -> None:
        assert _resolve_option_type("put", "BTC-27DEC24-100000-P") == "put"

    def test_uppercase_call(self) -> None:
        assert _resolve_option_type("CALL", "BTC-27DEC24-100000-C") == "call"

    def test_uppercase_put(self) -> None:
        assert _resolve_option_type("PUT", "BTC-27DEC24-100000-P") == "put"

    def test_fallback_to_name_suffix_c(self) -> None:
        assert _resolve_option_type("", "BTC-27DEC24-100000-C") == "call"

    def test_fallback_to_name_suffix_p(self) -> None:
        assert _resolve_option_type("", "BTC-27DEC24-100000-P") == "put"

    def test_unknown_returns_none(self) -> None:
        assert _resolve_option_type("future", "BTC-PERPETUAL") is None

    def test_empty_name_and_type_returns_none(self) -> None:
        assert _resolve_option_type("", "") is None


class TestExpiryToDateStr:
    def test_valid_timestamp(self) -> None:
        # 2024-12-27 08:00:00 UTC = 1735286400000 ms
        ts_ms = 1_735_286_400_000
        result = _expiry_to_date_str(ts_ms, "BTC-27DEC24-100000-C")
        assert result == "2024-12-27"

    def test_none_timestamp_returns_unknown(self) -> None:
        result = _expiry_to_date_str(None, "BTC-27DEC24-100000-C")
        assert result == "unknown"

    def test_invalid_timestamp_returns_unknown(self) -> None:
        result = _expiry_to_date_str("not-a-number", "BTC-27DEC24-100000-C")
        assert result == "unknown"


class TestAggregateOI:
    def test_sums_calls_and_puts_separately(self) -> None:
        contracts = [
            OptionContractSummary(
                instrument_name="BTC-27DEC24-100000-C",
                expiry="2024-12-27",
                strike=100_000.0,
                option_type="call",
                open_interest=1_000.0,
            ),
            OptionContractSummary(
                instrument_name="BTC-27DEC24-100000-P",
                expiry="2024-12-27",
                strike=100_000.0,
                option_type="put",
                open_interest=800.0,
            ),
            OptionContractSummary(
                instrument_name="BTC-27DEC24-90000-C",
                expiry="2024-12-27",
                strike=90_000.0,
                option_type="call",
                open_interest=500.0,
            ),
        ]
        call_oi, put_oi = _aggregate_oi(contracts)
        assert call_oi == pytest.approx(1_500.0)
        assert put_oi == pytest.approx(800.0)

    def test_none_oi_treated_as_zero(self) -> None:
        """Contracts with open_interest=None contribute 0 to totals."""
        contracts = [
            OptionContractSummary(
                instrument_name="BTC-27DEC24-100000-C",
                expiry="2024-12-27",
                strike=100_000.0,
                option_type="call",
                open_interest=None,
            ),
            OptionContractSummary(
                instrument_name="BTC-27DEC24-100000-P",
                expiry="2024-12-27",
                strike=100_000.0,
                option_type="put",
                open_interest=600.0,
            ),
        ]
        call_oi, put_oi = _aggregate_oi(contracts)
        assert call_oi == 0.0
        assert put_oi == pytest.approx(600.0)

    def test_empty_contracts(self) -> None:
        call_oi, put_oi = _aggregate_oi([])
        assert call_oi == 0.0
        assert put_oi == 0.0


class TestComputePCR:
    def test_normal_ratio(self) -> None:
        assert _compute_pcr(1_000.0, 800.0) == pytest.approx(0.8)

    def test_zero_call_oi_returns_none(self) -> None:
        """put_call_oi_ratio must be None — not zero, not infinity — when
        total_call_oi == 0 (Requirement 14.4)."""
        assert _compute_pcr(0.0, 500.0) is None

    def test_equal_oi_returns_one(self) -> None:
        assert _compute_pcr(500.0, 500.0) == pytest.approx(1.0)

    def test_zero_put_oi(self) -> None:
        assert _compute_pcr(1_000.0, 0.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# DeribitOptionsOverview.compute() — integration-style unit tests
# ---------------------------------------------------------------------------


class TestDeribitOptionsOverviewCompute:
    """Unit tests for DeribitOptionsOverview.compute() using mocked DeribitClient."""

    @pytest.mark.asyncio
    async def test_happy_path_single_call_contract(self) -> None:
        """Single call instrument → result has 1 contract, correct totals."""
        instruments = [_make_instrument("BTC-27DEC24-100000-C", "call", 100_000.0)]
        tickers = [_make_ticker(mark_price=0.05, mark_iv=80.0, open_interest=1_500.0,
                                best_bid_price=0.049, best_ask_price=0.051)]
        client = _make_mock_client(
            instruments=instruments,
            index_price_value=95_000.0,
            tickers=tickers,
        )

        engine = DeribitOptionsOverview()
        result = await engine.compute("BTC", client)

        assert result.currency == "BTC"
        assert result.index_price == 95_000.0
        assert len(result.contracts) == 1

        contract = result.contracts[0]
        assert contract.instrument_name == "BTC-27DEC24-100000-C"
        assert contract.option_type == "call"
        assert contract.mark_iv == 80.0
        assert contract.open_interest == 1_500.0
        assert contract.best_bid == pytest.approx(0.049)
        assert contract.best_ask == pytest.approx(0.051)
        assert contract.bid_ask_spread == pytest.approx(0.002, abs=1e-9)

        assert result.total_call_oi == pytest.approx(1_500.0)
        assert result.total_put_oi == 0.0
        # put/call ratio: 0.0 / 1500 = 0.0
        assert result.put_call_oi_ratio == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_call_and_put_contracts(self) -> None:
        """Two contracts (one call, one put) — OI totals and PCR computed correctly."""
        instruments = [
            _make_instrument("BTC-27DEC24-100000-C", "call", 100_000.0),
            _make_instrument("BTC-27DEC24-100000-P", "put", 100_000.0),
        ]
        tickers = [
            _make_ticker(open_interest=1_000.0),
            _make_ticker(open_interest=800.0),
        ]
        client = _make_mock_client(instruments=instruments, tickers=tickers)

        result = await DeribitOptionsOverview().compute("BTC", client)

        assert len(result.contracts) == 2
        assert result.total_call_oi == pytest.approx(1_000.0)
        assert result.total_put_oi == pytest.approx(800.0)
        assert result.put_call_oi_ratio == pytest.approx(0.8)

    @pytest.mark.asyncio
    async def test_null_iv_preserved_not_zero_substituted(self) -> None:
        """mark_iv=None from Deribit must be preserved as None (Requirement 14.5)."""
        instruments = [_make_instrument()]
        tickers = [_make_ticker(mark_iv=None)]
        client = _make_mock_client(instruments=instruments, tickers=tickers)

        result = await DeribitOptionsOverview().compute("BTC", client)

        assert result.contracts[0].mark_iv is None

    @pytest.mark.asyncio
    async def test_null_open_interest_preserved(self) -> None:
        """open_interest=None must be preserved; total_call_oi still sums to 0."""
        instruments = [_make_instrument()]
        tickers = [_make_ticker(open_interest=None)]
        client = _make_mock_client(instruments=instruments, tickers=tickers)

        result = await DeribitOptionsOverview().compute("BTC", client)

        assert result.contracts[0].open_interest is None
        assert result.total_call_oi == 0.0
        # 0 call OI → PCR is None
        assert result.put_call_oi_ratio is None

    @pytest.mark.asyncio
    async def test_null_bid_ask_preserved(self) -> None:
        """best_bid/best_ask=None must be preserved; spread must be None."""
        instruments = [_make_instrument()]
        tickers = [_make_ticker(best_bid_price=None, best_ask_price=None)]
        client = _make_mock_client(instruments=instruments, tickers=tickers)

        result = await DeribitOptionsOverview().compute("BTC", client)

        contract = result.contracts[0]
        assert contract.best_bid is None
        assert contract.best_ask is None
        assert contract.bid_ask_spread is None

    @pytest.mark.asyncio
    async def test_pcr_none_when_no_calls_have_oi(self) -> None:
        """put_call_oi_ratio must be None when all call OI is absent (Req 14.4)."""
        instruments = [
            _make_instrument("BTC-27DEC24-100000-C", "call"),
            _make_instrument("BTC-27DEC24-100000-P", "put"),
        ]
        # Call has no OI, put has OI
        tickers = [
            _make_ticker(open_interest=None),
            _make_ticker(open_interest=500.0),
        ]
        client = _make_mock_client(instruments=instruments, tickers=tickers)

        result = await DeribitOptionsOverview().compute("BTC", client)

        assert result.total_call_oi == 0.0
        assert result.total_put_oi == pytest.approx(500.0)
        assert result.put_call_oi_ratio is None

    @pytest.mark.asyncio
    async def test_ticker_fetch_failure_skips_contract(self) -> None:
        """When get_ticker raises, the contract is silently skipped."""
        instruments = [
            _make_instrument("BTC-27DEC24-100000-C", "call"),
            _make_instrument("BTC-27DEC24-90000-C", "call"),
        ]
        # First ticker raises, second succeeds
        client = _make_mock_client(instruments=instruments)
        client.get_ticker = AsyncMock(
            side_effect=[ValueError("Deribit error"), _make_ticker(open_interest=200.0)]
        )

        result = await DeribitOptionsOverview().compute("BTC", client)

        # Only the second contract should be present
        assert len(result.contracts) == 1
        assert result.contracts[0].instrument_name == "BTC-27DEC24-90000-C"

    @pytest.mark.asyncio
    async def test_all_ticker_failures_yield_empty_contracts(self) -> None:
        """When all ticker fetches fail, contracts list is empty."""
        instruments = [_make_instrument()]
        client = _make_mock_client(instruments=instruments)
        client.get_ticker = AsyncMock(side_effect=ConnectionError("network failure"))

        result = await DeribitOptionsOverview().compute("BTC", client)

        assert result.contracts == []
        assert result.total_call_oi == 0.0
        assert result.total_put_oi == 0.0
        assert result.put_call_oi_ratio is None

    @pytest.mark.asyncio
    async def test_unknown_option_type_skipped(self) -> None:
        """Instruments with unrecognised option_type are silently skipped."""
        instruments = [
            _make_instrument("BTC-PERPETUAL", option_type="future"),
            _make_instrument("BTC-27DEC24-100000-C", option_type="call"),
        ]
        tickers = [_make_ticker()]
        client = _make_mock_client(instruments=instruments, tickers=tickers)
        # get_ticker only called once (skipped instrument never reaches ticker fetch)
        client.get_ticker = AsyncMock(return_value=_make_ticker(open_interest=300.0))

        result = await DeribitOptionsOverview().compute("BTC", client)

        # Only the valid call contract should be present
        assert len(result.contracts) == 1
        assert result.contracts[0].instrument_name == "BTC-27DEC24-100000-C"

    @pytest.mark.asyncio
    async def test_currency_uppercased_in_result(self) -> None:
        """Lowercase currency input must produce uppercase result.currency."""
        client = _make_mock_client()
        result = await DeribitOptionsOverview().compute("btc", client)
        assert result.currency == "BTC"

    @pytest.mark.asyncio
    async def test_computed_at_is_utc_iso8601_z(self) -> None:
        """computed_at must be a non-empty UTC ISO-8601 string ending with Z."""
        client = _make_mock_client()
        result = await DeribitOptionsOverview().compute("BTC", client)
        assert _ISO8601_Z_RE.match(result.computed_at), (
            f"computed_at {result.computed_at!r} does not match ISO-8601 Z format"
        )

    @pytest.mark.asyncio
    async def test_empty_instruments_list(self) -> None:
        """Empty instruments list → empty contracts, zero OI, PCR None."""
        client = _make_mock_client(instruments=[])
        result = await DeribitOptionsOverview().compute("ETH", client)

        assert result.contracts == []
        assert result.total_call_oi == 0.0
        assert result.total_put_oi == 0.0
        assert result.put_call_oi_ratio is None

    @pytest.mark.asyncio
    async def test_index_price_passed_to_client_correctly(self) -> None:
        """get_index_price must be called with '{currency.lower()}_usd'."""
        client = _make_mock_client(index_price_value=3_200.0)
        await DeribitOptionsOverview().compute("ETH", client)

        client.get_index_price.assert_awaited_once_with("eth_usd")

    @pytest.mark.asyncio
    async def test_get_instruments_called_with_kind_option(self) -> None:
        """get_instruments must be called with kind='option'."""
        client = _make_mock_client()
        await DeribitOptionsOverview().compute("SOL", client)

        client.get_instruments.assert_awaited_once_with(
            currency="SOL", kind="option"
        )

    @pytest.mark.asyncio
    async def test_multiple_contracts_bid_ask_spread_computed(self) -> None:
        """bid_ask_spread is correctly computed for each contract."""
        instruments = [
            _make_instrument("BTC-27DEC24-100000-C", "call"),
            _make_instrument("BTC-27DEC24-90000-P", "put"),
        ]
        tickers = [
            _make_ticker(best_bid_price=0.04, best_ask_price=0.06),  # spread 0.02
            _make_ticker(best_bid_price=0.10, best_ask_price=0.12),  # spread 0.02
        ]
        client = _make_mock_client(instruments=instruments, tickers=tickers)

        result = await DeribitOptionsOverview().compute("BTC", client)

        for contract in result.contracts:
            assert contract.bid_ask_spread == pytest.approx(0.02, abs=1e-9)

    @pytest.mark.asyncio
    async def test_instrument_without_name_skipped(self) -> None:
        """Instruments without instrument_name are silently skipped."""
        instruments = [
            {"option_type": "call", "strike": 100_000.0, "expiration_timestamp": 0},
            _make_instrument("BTC-27DEC24-100000-C", "call"),
        ]
        client = _make_mock_client(instruments=instruments)
        client.get_ticker = AsyncMock(return_value=_make_ticker(open_interest=100.0))

        result = await DeribitOptionsOverview().compute("BTC", client)

        # Only the named instrument makes it through
        assert len(result.contracts) == 1

    @pytest.mark.asyncio
    async def test_result_immutable(self) -> None:
        """OptionsOverviewResult must be frozen (immutable)."""
        client = _make_mock_client()
        result = await DeribitOptionsOverview().compute("BTC", client)
        with pytest.raises(Exception):
            result.index_price = 0.0  # type: ignore[misc]
