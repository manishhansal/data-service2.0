"""
Unit tests for src/providers/adapters/jugaad_data.py — Task 4.8.

Covers:
- Interval validation: ``1d`` accepted; ``3m`` raises ``ValueError``;
  other intervals raise ``ValueError``.
- Credential-free provider metadata: correct ``source_type`` and ``provider``
  fields on every returned row.
- Row normaliser: correct canonical field shape from raw bhavcopy dicts.
- OI semantics: ``oi`` is never populated from ``tradedValue``; absent OI
  results in ``oi=None`` and ``oi_missing=True``.
- Fetch end-to-end: mocked HTTP responses produce correct normalised output.
- Non-trading days (404) are silently skipped.
- Network errors are silently skipped (empty list returned, not raised).

Requirements: 5.9, 5.12
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.providers.adapters.jugaad_data import (
    BLOCKED_INTERVAL,
    PROVIDER_ID,
    REQUESTS_PER_SECOND,
    SOURCE_TYPE,
    SUPPORTED_INTERVAL,
    JugaadDataAdapter,
    JugaadDataError,
    _normalise_row,
    _validate_interval,
)
from src.core.schemas.provider import SourceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw_row(
    symbol: str = "NIFTY",
    date: datetime.date | None = None,
    open_: float = 22000.0,
    high: float = 22100.0,
    low: float = 21900.0,
    close: float = 22050.0,
    contracts: int = 5000,
    oi: int | None = 250000,
    traded_value: float = 1_100_000_000.0,
) -> dict[str, Any]:
    """Build a minimal raw bhavcopy row dict."""
    if date is None:
        date = datetime.date(2024, 1, 15)
    row: dict[str, Any] = {
        "SYMBOL": symbol,
        "TIMESTAMP": date,
        "OPEN": str(open_),
        "HIGH": str(high),
        "LOW": str(low),
        "CLOSE": str(close),
        "CONTRACTS": str(contracts),
        # TRDVAL represents traded value — must NOT be used for OI.
        "TRDVAL": str(traded_value),
    }
    if oi is not None:
        row["OPEN_INT"] = str(oi)
    return row


# ---------------------------------------------------------------------------
# Interval validation
# ---------------------------------------------------------------------------


class TestValidateInterval:
    def test_1d_is_accepted(self) -> None:
        """The ``1d`` interval must not raise."""
        _validate_interval("1d")  # Should not raise.

    def test_3m_raises_value_error(self) -> None:
        """``3m`` must always raise ``ValueError`` — permanent Indian ban."""
        with pytest.raises(ValueError, match="3m"):
            _validate_interval("3m")

    def test_3m_error_message_contains_permanently_unsupported(self) -> None:
        with pytest.raises(ValueError, match="permanently unsupported"):
            _validate_interval("3m")

    @pytest.mark.parametrize(
        "interval",
        ["1m", "5m", "10m", "15m", "30m", "1h", "1w", "1M", "2h", "4h"],
    )
    def test_non_1d_intervals_raise_value_error(self, interval: str) -> None:
        """All intervals other than ``1d`` must raise ``ValueError``."""
        with pytest.raises(ValueError):
            _validate_interval(interval)

    def test_error_message_mentions_1d_only(self) -> None:
        with pytest.raises(ValueError, match="1d"):
            _validate_interval("5m")


# ---------------------------------------------------------------------------
# Provider metadata constants
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    def test_provider_id_is_jugaad_data(self) -> None:
        assert PROVIDER_ID == "jugaad_data"

    def test_source_type_is_credential_free(self) -> None:
        assert SOURCE_TYPE == SourceType.CREDENTIAL_FREE

    def test_supported_interval_is_1d(self) -> None:
        assert SUPPORTED_INTERVAL == "1d"

    def test_blocked_interval_is_3m(self) -> None:
        assert BLOCKED_INTERVAL == "3m"

    def test_rate_limit_is_1_rps(self) -> None:
        assert REQUESTS_PER_SECOND == 1.0


# ---------------------------------------------------------------------------
# Row normaliser
# ---------------------------------------------------------------------------


class TestNormaliseRow:
    def test_returns_dict(self) -> None:
        row = _make_raw_row()
        result = _normalise_row(row)
        assert isinstance(result, dict)

    def test_canonical_fields_present(self) -> None:
        required = {
            "time", "open", "high", "low", "close",
            "volume", "volume_unavailable",
            "oi", "oi_missing",
            "symbol", "exchange", "interval",
            "source_type", "provider",
        }
        result = _normalise_row(_make_raw_row())
        assert required.issubset(result.keys())

    def test_open_parsed_correctly(self) -> None:
        result = _normalise_row(_make_raw_row(open_=22000.5))
        assert result["open"] == pytest.approx(22000.5)

    def test_high_parsed_correctly(self) -> None:
        result = _normalise_row(_make_raw_row(high=22100.0))
        assert result["high"] == pytest.approx(22100.0)

    def test_low_parsed_correctly(self) -> None:
        result = _normalise_row(_make_raw_row(low=21900.0))
        assert result["low"] == pytest.approx(21900.0)

    def test_close_parsed_correctly(self) -> None:
        result = _normalise_row(_make_raw_row(close=22050.0))
        assert result["close"] == pytest.approx(22050.0)

    def test_volume_from_contracts_field(self) -> None:
        result = _normalise_row(_make_raw_row(contracts=5000))
        assert result["volume"] == 5000
        assert result["volume_unavailable"] is False

    def test_volume_unavailable_when_contracts_missing(self) -> None:
        row = _make_raw_row()
        row.pop("CONTRACTS")
        result = _normalise_row(row)
        assert result["volume"] == 0
        assert result["volume_unavailable"] is True

    def test_oi_populated_when_open_int_present(self) -> None:
        result = _normalise_row(_make_raw_row(oi=250000))
        assert result["oi"] == 250000
        assert result["oi_missing"] is False

    def test_oi_is_none_when_missing(self) -> None:
        """OI must be None (not zero, not tradedValue) when not provided."""
        row = _make_raw_row(oi=None)
        result = _normalise_row(row)
        assert result["oi"] is None
        assert result["oi_missing"] is True

    def test_oi_never_populated_from_traded_value(self) -> None:
        """Critical: oi must never equal tradedValue (Requirement 3.3, 6.2)."""
        traded_value = 1_100_000_000.0
        row = _make_raw_row(oi=None, traded_value=traded_value)
        result = _normalise_row(row)
        # OI must be None, not the traded value.
        assert result["oi"] is None
        assert result["oi_missing"] is True

    def test_oi_never_equals_traded_value_even_when_both_present(self) -> None:
        """OI and tradedValue must remain distinct fields."""
        row = _make_raw_row(oi=250000, traded_value=1_100_000_000.0)
        result = _normalise_row(row)
        # OI must come from OPEN_INT, not TRDVAL.
        assert result["oi"] == 250000
        assert result["oi"] != 1_100_000_000.0

    def test_source_type_is_credential_free(self) -> None:
        result = _normalise_row(_make_raw_row())
        assert result["source_type"] == SourceType.CREDENTIAL_FREE.value

    def test_provider_is_jugaad_data(self) -> None:
        result = _normalise_row(_make_raw_row())
        assert result["provider"] == "jugaad_data"

    def test_interval_is_1d(self) -> None:
        result = _normalise_row(_make_raw_row())
        assert result["interval"] == "1d"

    def test_symbol_from_row(self) -> None:
        result = _normalise_row(_make_raw_row(symbol="RELIANCE"))
        assert result["symbol"] == "RELIANCE"

    def test_time_is_epoch_seconds(self) -> None:
        date = datetime.date(2024, 1, 15)
        result = _normalise_row(_make_raw_row(date=date))
        # Should be a non-zero integer representing UTC epoch seconds.
        assert isinstance(result["time"], int)
        assert result["time"] > 0

    def test_time_is_correct_epoch(self) -> None:
        date = datetime.date(2024, 1, 15)
        expected_epoch = int(
            datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc).timestamp()
        )
        result = _normalise_row(_make_raw_row(date=date))
        assert result["time"] == expected_epoch

    def test_datetime_timestamp_handled(self) -> None:
        """datetime.datetime TIMESTAMP should be parsed correctly."""
        row = _make_raw_row()
        row["TIMESTAMP"] = datetime.datetime(2024, 1, 15, 0, 0, tzinfo=datetime.timezone.utc)
        result = _normalise_row(row)
        assert result["time"] > 0

    def test_string_timestamp_dd_mon_yyyy(self) -> None:
        """'DD-Mon-YYYY' format string timestamps should be parsed."""
        row = _make_raw_row()
        row["TIMESTAMP"] = "15-Jan-2024"
        result = _normalise_row(row)
        assert result["time"] > 0

    def test_string_timestamp_yyyy_mm_dd(self) -> None:
        """'YYYY-MM-DD' format string timestamps should be parsed."""
        row = _make_raw_row()
        row["TIMESTAMP"] = "2024-01-15"
        result = _normalise_row(row)
        assert result["time"] > 0


# ---------------------------------------------------------------------------
# JugaadDataAdapter — fetch_fo_eod
# ---------------------------------------------------------------------------


class TestFetchFoEodIntervalValidation:
    """Validate that fetch_fo_eod enforces interval constraints."""

    async def test_1d_does_not_raise(self) -> None:
        adapter = JugaadDataAdapter()
        # Dates before 2024-07-08 (old ZIP format era) to avoid the UDiff warning path
        with patch.object(adapter, "_sync_fo_bhavcopy", return_value=[]):
            result = await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval="1d",
            )
        assert isinstance(result, list)

    async def test_3m_raises_value_error(self) -> None:
        adapter = JugaadDataAdapter()
        with pytest.raises(ValueError, match="3m"):
            await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval="3m",
            )

    @pytest.mark.parametrize("interval", ["1m", "5m", "15m", "1h"])
    async def test_non_1d_raises_value_error(self, interval: str) -> None:
        adapter = JugaadDataAdapter()
        with pytest.raises(ValueError):
            await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval=interval,
            )


class TestFetchFoEodReturnShape:
    """Verify the normalised output shape from fetch_fo_eod."""

    async def test_returns_list(self) -> None:
        adapter = JugaadDataAdapter()
        raw_fo = [JugaadDataAdapter._normalise_fo_row(
            _make_raw_row("NIFTY", datetime.date(2024, 1, 15)), "NIFTY"
        )]
        with patch.object(adapter, "_sync_fo_bhavcopy", return_value=raw_fo):
            result = await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 15),
                to_date=datetime.date(2024, 1, 15),
            )
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_rows_have_canonical_fields(self) -> None:
        adapter = JugaadDataAdapter()
        raw_fo = [JugaadDataAdapter._normalise_fo_row(
            _make_raw_row("NIFTY", datetime.date(2024, 1, 15)), "NIFTY"
        )]
        with patch.object(adapter, "_sync_fo_bhavcopy", return_value=raw_fo):
            result = await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 15),
                to_date=datetime.date(2024, 1, 15),
            )
        row = result[0]
        for field in ("time", "open", "high", "low", "close", "volume",
                      "oi", "oi_missing", "source_type", "provider"):
            assert field in row, f"Missing field: {field}"

    async def test_source_type_credential_free(self) -> None:
        adapter = JugaadDataAdapter()
        raw_fo = [JugaadDataAdapter._normalise_fo_row(
            _make_raw_row("NIFTY", datetime.date(2024, 1, 15)), "NIFTY"
        )]
        with patch.object(adapter, "_sync_fo_bhavcopy", return_value=raw_fo):
            result = await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 15),
                to_date=datetime.date(2024, 1, 15),
            )
        assert result[0]["source_type"] == "CREDENTIAL_FREE"

    async def test_provider_field_is_jugaad_data(self) -> None:
        adapter = JugaadDataAdapter()
        raw_fo = [JugaadDataAdapter._normalise_fo_row(
            _make_raw_row("NIFTY", datetime.date(2024, 1, 15)), "NIFTY"
        )]
        with patch.object(adapter, "_sync_fo_bhavcopy", return_value=raw_fo):
            result = await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 15),
                to_date=datetime.date(2024, 1, 15),
            )
        assert result[0]["provider"] == "jugaad_data"

    async def test_empty_range_returns_empty_list(self) -> None:
        adapter = JugaadDataAdapter()
        with patch.object(adapter, "_sync_fo_bhavcopy", return_value=[]):
            result = await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 1),
            )
        assert result == []


class TestFetchFoEodOiSemantics:
    """OI must never be populated from tradedValue (Requirements 3.3, 6.2)."""

    async def test_oi_from_open_int_not_trdval(self) -> None:
        """When OPEN_INT is present, use it; never use TRDVAL for OI."""
        adapter = JugaadDataAdapter()
        raw_fo = [JugaadDataAdapter._normalise_fo_row(
            _make_raw_row("NIFTY", datetime.date(2024, 1, 15), oi=300000), "NIFTY"
        )]
        with patch.object(adapter, "_sync_fo_bhavcopy", return_value=raw_fo):
            result = await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 15),
                to_date=datetime.date(2024, 1, 15),
            )
        assert result[0]["oi"] == 300000
        assert result[0]["oi"] != 1_100_000_000

    async def test_oi_is_none_when_not_provided(self) -> None:
        """Absent OI must yield None + oi_missing=True."""
        adapter = JugaadDataAdapter()
        raw_fo = [JugaadDataAdapter._normalise_fo_row(
            _make_raw_row("NIFTY", datetime.date(2024, 1, 15), oi=None), "NIFTY"
        )]
        with patch.object(adapter, "_sync_fo_bhavcopy", return_value=raw_fo):
            result = await adapter.fetch_fo_eod(
                symbol="NIFTY",
                from_date=datetime.date(2024, 1, 15),
                to_date=datetime.date(2024, 1, 15),
            )
        assert result[0]["oi"] is None
        assert result[0]["oi_missing"] is True


class TestBhavcopySingleDay:
    """Tests for _sync_fo_bhavcopy graceful error handling."""

    async def test_404_returns_empty_list(self) -> None:
        """404 response (holiday/non-trading day) returns empty list."""
        adapter = JugaadDataAdapter()
        # Patch httpx.get to simulate 404
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_get.return_value = mock_resp
            result = adapter._sync_fo_bhavcopy("NIFTY",
                datetime.date(2024, 1, 15), datetime.date(2024, 1, 15))
        assert result == []

    async def test_non_200_non_404_returns_empty_list(self) -> None:
        adapter = JugaadDataAdapter()
        with patch("httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_get.return_value = mock_resp
            result = adapter._sync_fo_bhavcopy("NIFTY",
                datetime.date(2024, 1, 15), datetime.date(2024, 1, 15))
        assert result == []

    async def test_network_error_returns_empty_list(self) -> None:
        """Network errors must be handled gracefully."""
        adapter = JugaadDataAdapter()
        with patch("httpx.get", side_effect=Exception("connection refused")):
            result = adapter._sync_fo_bhavcopy("NIFTY",
                datetime.date(2024, 1, 15), datetime.date(2024, 1, 15))
        assert result == []
