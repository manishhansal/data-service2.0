"""
Unit tests for src/providers/adapters/yahoo_finance.py — Task 4.8.

Covers:
- Interval validation: only ``1d`` is accepted; all other intervals, including
  ``3m``, raise ``YahooFinanceIntervalError`` (subclass of ``ValueError``).
- Instrument type validation: only ``EQ`` is accepted; F&O and other types
  raise ``YahooFinanceInstrumentError`` (subclass of ``ValueError``).
- Provider metadata: ``source_type=SECONDARY_FALLBACK``, ``provider="yahoo_finance"``.
- ``SECONDARY_FALLBACK`` provenance tag present on every row.
- ``max_quality_grade="B"`` on every row.
- OI always ``None`` with ``oi_missing=True``.
- IV, Greeks, bid, ask always ``None``.
- Symbol resolution: NSE symbols get ``.NS`` suffix; BSE get ``.BO``.
- Row normaliser: canonical field shape from parallel API arrays.
- Fetch end-to-end: mocked HTTP responses produce correct normalised output.
- HTTP error handling: non-200 and network errors return empty list.

Requirements: 5.9, 5.12
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.providers.adapters.yahoo_finance import (
    BLOCKED_INTERVAL,
    MAX_QUALITY_GRADE,
    PROVIDER_ID,
    REQUESTS_PER_SECOND,
    SOURCE_TYPE,
    SUPPORTED_INTERVAL,
    YahooFinanceAdapter,
    YahooFinanceError,
    YahooFinanceIntervalError,
    YahooFinanceInstrumentError,
    _normalise_rows,
    _resolve_yf_symbol,
    _validate_instrument_type,
    _validate_interval,
)
from src.core.schemas.provider import SourceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_yf_response_body(
    timestamps: list[int] | None = None,
    opens: list[float | None] | None = None,
    highs: list[float | None] | None = None,
    lows: list[float | None] | None = None,
    closes: list[float | None] | None = None,
    volumes: list[float | None] | None = None,
) -> dict[str, Any]:
    """Build a minimal Yahoo Finance v8 chart API response body."""
    if timestamps is None:
        timestamps = [1705276800]
    if opens   is None: opens   = [22000.0]
    if highs   is None: highs   = [22100.0]
    if lows    is None: lows    = [21900.0]
    if closes  is None: closes  = [22050.0]
    if volumes is None: volumes = [1_200_000.0]
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open":   opens,
                                "high":   highs,
                                "low":    lows,
                                "close":  closes,
                                "volume": volumes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def _make_mock_response(
    status_code: int = 200,
    body: Any = None,
) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    if body is not None:
        mock.json.return_value = body
    else:
        mock.json.side_effect = Exception("no body")
    return mock


# ---------------------------------------------------------------------------
# Interval validation
# ---------------------------------------------------------------------------


class TestValidateInterval:
    def test_1d_is_accepted(self) -> None:
        _validate_interval("1d")  # Should not raise.

    def test_3m_raises_yahoo_finance_interval_error(self) -> None:
        with pytest.raises(YahooFinanceIntervalError, match="3m"):
            _validate_interval("3m")

    def test_3m_is_subclass_of_value_error(self) -> None:
        with pytest.raises(ValueError):
            _validate_interval("3m")

    def test_3m_error_mentions_permanently_unsupported(self) -> None:
        with pytest.raises(YahooFinanceIntervalError, match="permanently unsupported"):
            _validate_interval("3m")

    @pytest.mark.parametrize(
        "interval",
        ["1m", "5m", "10m", "15m", "30m", "1h", "1w", "1M", "2h", "4h", "6h", "12h"],
    )
    def test_non_1d_intervals_raise(self, interval: str) -> None:
        with pytest.raises(YahooFinanceIntervalError):
            _validate_interval(interval)

    def test_error_message_mentions_secondary_fallback(self) -> None:
        with pytest.raises(YahooFinanceIntervalError, match="secondary fallback"):
            _validate_interval("1m")


# ---------------------------------------------------------------------------
# Instrument type validation
# ---------------------------------------------------------------------------


class TestValidateInstrumentType:
    def test_eq_is_accepted(self) -> None:
        _validate_instrument_type("EQ")  # Should not raise.

    def test_eq_case_insensitive(self) -> None:
        _validate_instrument_type("eq")  # Should not raise.

    @pytest.mark.parametrize(
        "instrument_type",
        ["FO", "FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK", "ETF", "IDX",
         "CRYPTO_SPOT", "CRYPTO_FUTURES", "CRYPTO_OPTIONS"],
    )
    def test_non_eq_raises_yahoo_finance_instrument_error(self, instrument_type: str) -> None:
        with pytest.raises(YahooFinanceInstrumentError):
            _validate_instrument_type(instrument_type)

    def test_instrument_error_is_subclass_of_value_error(self) -> None:
        with pytest.raises(ValueError):
            _validate_instrument_type("FO")

    def test_error_message_mentions_equity_restriction(self) -> None:
        with pytest.raises(YahooFinanceInstrumentError, match="equity"):
            _validate_instrument_type("FO")

    def test_error_message_mentions_fo_not_allowed(self) -> None:
        with pytest.raises(YahooFinanceInstrumentError, match="F&O"):
            _validate_instrument_type("FO")


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


class TestResolveYfSymbol:
    def test_nse_symbol_gets_ns_suffix(self) -> None:
        assert _resolve_yf_symbol("RELIANCE", "NSE") == "RELIANCE.NS"

    def test_bse_symbol_gets_bo_suffix(self) -> None:
        assert _resolve_yf_symbol("RELIANCE", "BSE") == "RELIANCE.BO"

    def test_nfo_exchange_gets_ns_suffix(self) -> None:
        assert _resolve_yf_symbol("NIFTY", "NFO") == "NIFTY.NS"

    def test_symbol_already_with_suffix_unchanged(self) -> None:
        assert _resolve_yf_symbol("RELIANCE.NS", "NSE") == "RELIANCE.NS"

    def test_symbol_uppercased(self) -> None:
        assert _resolve_yf_symbol("reliance", "NSE") == "RELIANCE.NS"

    def test_default_exchange_is_nse(self) -> None:
        assert _resolve_yf_symbol("RELIANCE") == "RELIANCE.NS"


# ---------------------------------------------------------------------------
# Provider metadata constants
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    def test_provider_id_is_yahoo_finance(self) -> None:
        assert PROVIDER_ID == "yahoo_finance"

    def test_source_type_is_secondary_fallback(self) -> None:
        assert SOURCE_TYPE == SourceType.SECONDARY_FALLBACK

    def test_supported_interval_is_1d(self) -> None:
        assert SUPPORTED_INTERVAL == "1d"

    def test_blocked_interval_is_3m(self) -> None:
        assert BLOCKED_INTERVAL == "3m"

    def test_max_quality_grade_is_b(self) -> None:
        assert MAX_QUALITY_GRADE == "B"

    def test_rate_limit_is_1_rps(self) -> None:
        assert REQUESTS_PER_SECOND == 1.0


# ---------------------------------------------------------------------------
# Row normaliser
# ---------------------------------------------------------------------------


class TestNormaliseRows:
    def test_returns_list(self) -> None:
        result = _normalise_rows([1705276800], {"open": [22000.0], "high": [22100.0],
            "low": [21900.0], "close": [22050.0], "volume": [1_200_000.0]},
            "RELIANCE", "NSE")
        assert isinstance(result, list)
        assert len(result) == 1

    def test_canonical_fields_present(self) -> None:
        required = {
            "time", "open", "high", "low", "close",
            "volume", "volume_unavailable",
            "oi", "oi_missing",
            "symbol", "exchange", "interval",
            "source_type", "provider",
            "max_quality_grade",
        }
        result = _normalise_rows(
            [1705276800],
            {"open": [22000.0], "high": [22100.0], "low": [21900.0],
             "close": [22050.0], "volume": [1_200_000.0]},
            "RELIANCE", "NSE",
        )
        assert required.issubset(result[0].keys())

    def test_source_type_is_secondary_fallback(self) -> None:
        result = _normalise_rows(
            [1705276800],
            {"open": [22000.0], "high": [22100.0], "low": [21900.0],
             "close": [22050.0], "volume": [1_200_000.0]},
            "RELIANCE", "NSE",
        )
        assert result[0]["source_type"] == "SECONDARY_FALLBACK"

    def test_max_quality_grade_is_b(self) -> None:
        result = _normalise_rows(
            [1705276800],
            {"open": [22000.0], "high": [22100.0], "low": [21900.0],
             "close": [22050.0], "volume": [1_200_000.0]},
            "RELIANCE", "NSE",
        )
        assert result[0]["max_quality_grade"] == "B"

    def test_oi_is_always_none(self) -> None:
        """Yahoo Finance never provides OI (Requirement 3.3, 6.2)."""
        result = _normalise_rows(
            [1705276800],
            {"open": [22000.0], "high": [22100.0], "low": [21900.0],
             "close": [22050.0], "volume": [1_200_000.0]},
            "RELIANCE", "NSE",
        )
        assert result[0]["oi"] is None
        assert result[0]["oi_missing"] is True

    def test_iv_is_always_none(self) -> None:
        result = _normalise_rows(
            [1705276800],
            {"open": [22000.0], "high": [22100.0], "low": [21900.0],
             "close": [22050.0], "volume": [1_200_000.0]},
            "RELIANCE", "NSE",
        )
        assert result[0]["iv"] is None
        assert result[0]["iv_missing"] is True

    def test_bid_ask_are_always_none(self) -> None:
        result = _normalise_rows(
            [1705276800],
            {"open": [22000.0], "high": [22100.0], "low": [21900.0],
             "close": [22050.0], "volume": [1_200_000.0]},
            "RELIANCE", "NSE",
        )
        assert result[0]["bid"] is None
        assert result[0]["ask"] is None

    def test_interval_is_1d(self) -> None:
        result = _normalise_rows(
            [1705276800],
            {"open": [22000.0], "high": [22100.0], "low": [21900.0],
             "close": [22050.0], "volume": [1_200_000.0]},
            "RELIANCE", "NSE",
        )
        assert result[0]["interval"] == "1d"

    def test_provider_is_yahoo_finance(self) -> None:
        result = _normalise_rows(
            [1705276800],
            {"open": [22000.0], "high": [22100.0], "low": [21900.0],
             "close": [22050.0], "volume": [1_200_000.0]},
            "RELIANCE", "NSE",
        )
        assert result[0]["provider"] == "yahoo_finance"

    def test_multiple_rows_returned(self) -> None:
        timestamps = [1705276800, 1705363200, 1705449600]
        ohlcv: dict[str, list[float | None]] = {
            "open":   [22000.0, 22100.0, 22200.0],
            "high":   [22100.0, 22200.0, 22300.0],
            "low":    [21900.0, 22000.0, 22100.0],
            "close":  [22050.0, 22150.0, 22250.0],
            "volume": [1_200_000.0, 1_300_000.0, 1_100_000.0],
        }
        result = _normalise_rows(timestamps, ohlcv, "RELIANCE", "NSE")
        assert len(result) == 3

    def test_volume_unavailable_when_none(self) -> None:
        result = _normalise_rows(
            [1705276800],
            {"open": [22000.0], "high": [22100.0], "low": [21900.0],
             "close": [22050.0], "volume": [None]},
            "RELIANCE", "NSE",
        )
        assert result[0]["volume"] == 0
        assert result[0]["volume_unavailable"] is True

    def test_empty_timestamps_returns_empty_list(self) -> None:
        result = _normalise_rows([], {}, "RELIANCE", "NSE")
        assert result == []


# ---------------------------------------------------------------------------
# YahooFinanceAdapter — fetch_historical_ohlcv
# ---------------------------------------------------------------------------


class TestFetchHistoricalOhlcvIntervalEnforcement:
    """Only 1d is allowed — any other interval must raise."""

    async def test_1d_does_not_raise(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_resp = _make_mock_response(status_code=200, body=_make_yf_response_body())
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )
        assert isinstance(result, list)

    async def test_fetch_with_interval_1d_accepted(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_resp = _make_mock_response(status_code=200, body=_make_yf_response_body())
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv_with_interval(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
            interval="1d",
        )
        assert isinstance(result, list)

    @pytest.mark.parametrize(
        "interval",
        ["1m", "5m", "3m", "15m", "30m", "1h", "1w", "1M", "4h"],
    )
    async def test_non_1d_intervals_raise(self, interval: str) -> None:
        adapter = YahooFinanceAdapter()
        with pytest.raises(YahooFinanceIntervalError):
            await adapter.fetch_historical_ohlcv_with_interval(
                symbol="RELIANCE",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 31),
                interval=interval,
            )


class TestFetchHistoricalOhlcvInstrumentEnforcement:
    """Non-EQ instruments must always raise YahooFinanceInstrumentError."""

    @pytest.mark.parametrize(
        "instrument_type",
        ["FO", "FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK", "ETF", "IDX"],
    )
    async def test_non_eq_instruments_raise(self, instrument_type: str) -> None:
        adapter = YahooFinanceAdapter()
        with pytest.raises(YahooFinanceInstrumentError):
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 31),
                instrument_type=instrument_type,
            )

    async def test_eq_instrument_accepted(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_resp = _make_mock_response(status_code=200, body=_make_yf_response_body())
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
            instrument_type="EQ",
        )
        assert isinstance(result, list)


class TestFetchHistoricalOhlcvReturnShape:
    async def test_returns_list(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_resp = _make_mock_response(status_code=200, body=_make_yf_response_body())
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_secondary_fallback_tag_on_every_row(self) -> None:
        adapter = YahooFinanceAdapter()
        body = _make_yf_response_body(
            timestamps=[1705276800, 1705363200],
            opens=[22000.0, 22100.0],
            highs=[22100.0, 22200.0],
            lows=[21900.0, 22000.0],
            closes=[22050.0, 22150.0],
            volumes=[1_200_000.0, 1_300_000.0],
        )
        mock_resp = _make_mock_response(status_code=200, body=body)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )
        assert len(result) == 2
        for row in result:
            assert row["source_type"] == "SECONDARY_FALLBACK"

    async def test_max_quality_grade_b_on_every_row(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_resp = _make_mock_response(status_code=200, body=_make_yf_response_body())
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )
        for row in result:
            assert row["max_quality_grade"] == "B"

    async def test_oi_none_on_every_row(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_resp = _make_mock_response(status_code=200, body=_make_yf_response_body())
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )
        for row in result:
            assert row["oi"] is None
            assert row["oi_missing"] is True


class TestFetchHttpErrorHandling:
    async def test_non_200_returns_empty_list(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_resp = _make_mock_response(status_code=429)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )
        assert result == []

    async def test_network_error_returns_empty_list(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )
        assert result == []

    async def test_malformed_json_returns_empty_list(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("bad json")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )
        assert result == []

    async def test_missing_chart_key_returns_empty_list(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_resp = _make_mock_response(status_code=200, body={"error": "not found"})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.fetch_historical_ohlcv(
            symbol="RELIANCE",
            from_date=datetime.date(2024, 1, 1),
            to_date=datetime.date(2024, 1, 31),
        )
        assert result == []
