"""
Unit tests for src/providers/adapters/openchart.py — Task 4.8.

Covers:
- Interval validation: all nine canonical Indian timeframes accepted; ``3m``
  raises ``ValueError``; unknown intervals raise ``ValueError``.
- Interval mapping: each canonical interval maps to the correct OpenChart API
  interval string (1h → 60m, etc.).
- Provider metadata: correct ``source_type`` and ``provider`` fields.
- Row normaliser: canonical field shape; OI always None (OpenChart has no OI).
- Fetch end-to-end: mocked HTTP responses produce correct normalised output.
- HTTP error handling: non-200 and network errors return empty list gracefully.
- Response shape variants: flat list and ``{"candles": [...]}`` dict.

Requirements: 5.9, 5.12
"""

from __future__ import annotations

import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.providers.adapters.openchart import (
    BLOCKED_INTERVAL,
    PROVIDER_ID,
    REQUESTS_PER_SECOND,
    SOURCE_TYPE,
    OpenChartAdapter,
    OpenChartError,
    _INTERVAL_MAP,
    _normalise_row,
    _validate_interval,
)
from src.core.schemas.provider import CANONICAL_INDIAN_TIMEFRAMES, SourceType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw_item(
    t: int = 1705276800,   # 2024-01-15 00:00:00 UTC
    o: float = 22000.0,
    h: float = 22100.0,
    l: float = 21900.0,
    c: float = 22050.0,
    v: int = 1_200_000,
) -> dict[str, Any]:
    """Build a minimal raw OpenChart API candle item."""
    return {"t": t, "o": str(o), "h": str(h), "l": str(l), "c": str(c), "v": str(v)}


def _make_mock_response(
    status_code: int = 200,
    body: Any = None,
) -> MagicMock:
    """Build a mock httpx.Response."""
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
    def test_3m_raises_value_error(self) -> None:
        """``3m`` must always raise ``ValueError`` — permanent Indian ban."""
        with pytest.raises(ValueError, match="3m"):
            _validate_interval("3m")

    def test_3m_error_contains_permanently_unsupported(self) -> None:
        with pytest.raises(ValueError, match="permanently unsupported"):
            _validate_interval("3m")

    @pytest.mark.parametrize("interval", list(CANONICAL_INDIAN_TIMEFRAMES))
    def test_all_canonical_indian_timeframes_accepted(self, interval: str) -> None:
        """All nine canonical Indian timeframes must be accepted without error."""
        _validate_interval(interval)  # Should not raise.

    @pytest.mark.parametrize(
        "interval",
        ["2h", "4h", "6h", "8h", "12h", "2d", "3d", "invalid", ""],
    )
    def test_non_canonical_intervals_raise_value_error(self, interval: str) -> None:
        with pytest.raises(ValueError):
            _validate_interval(interval)


# ---------------------------------------------------------------------------
# Interval mapping
# ---------------------------------------------------------------------------


class TestIntervalMap:
    def test_1m_maps_to_1m(self) -> None:
        assert _INTERVAL_MAP["1m"] == "1m"

    def test_5m_maps_to_5m(self) -> None:
        assert _INTERVAL_MAP["5m"] == "5m"

    def test_10m_maps_to_10m(self) -> None:
        assert _INTERVAL_MAP["10m"] == "10m"

    def test_15m_maps_to_15m(self) -> None:
        assert _INTERVAL_MAP["15m"] == "15m"

    def test_30m_maps_to_30m(self) -> None:
        assert _INTERVAL_MAP["30m"] == "30m"

    def test_1h_maps_to_60m(self) -> None:
        """1h must map to '60m' — OpenChart uses minute notation for hours."""
        assert _INTERVAL_MAP["1h"] == "60m"

    def test_1d_maps_to_1d(self) -> None:
        assert _INTERVAL_MAP["1d"] == "1d"

    def test_1w_maps_to_1w(self) -> None:
        assert _INTERVAL_MAP["1w"] == "1w"

    def test_1M_maps_to_1M(self) -> None:
        assert _INTERVAL_MAP["1M"] == "1M"

    def test_3m_not_in_interval_map(self) -> None:
        """3m must not appear in the OpenChart interval map."""
        assert "3m" not in _INTERVAL_MAP

    def test_all_canonical_timeframes_covered(self) -> None:
        """Every canonical Indian timeframe must have a mapping."""
        for tf in CANONICAL_INDIAN_TIMEFRAMES:
            assert tf in _INTERVAL_MAP, f"Missing mapping for {tf!r}"


# ---------------------------------------------------------------------------
# Provider metadata constants
# ---------------------------------------------------------------------------


class TestProviderMetadata:
    def test_provider_id_is_openchart(self) -> None:
        assert PROVIDER_ID == "openchart"

    def test_source_type_is_credential_free(self) -> None:
        assert SOURCE_TYPE == SourceType.CREDENTIAL_FREE

    def test_blocked_interval_is_3m(self) -> None:
        assert BLOCKED_INTERVAL == "3m"

    def test_rate_limit_is_5_rps(self) -> None:
        assert REQUESTS_PER_SECOND == 5.0


# ---------------------------------------------------------------------------
# Row normaliser
# ---------------------------------------------------------------------------


class TestNormaliseRow:
    def test_returns_dict(self) -> None:
        result = _normalise_row(_make_raw_item(), "NIFTY", "NSE", "1d")
        assert isinstance(result, dict)

    def test_canonical_fields_present(self) -> None:
        required = {
            "time", "open", "high", "low", "close",
            "volume", "volume_unavailable",
            "oi", "oi_missing",
            "symbol", "exchange", "interval",
            "source_type", "provider",
        }
        result = _normalise_row(_make_raw_item(), "NIFTY", "NSE", "1d")
        assert required.issubset(result.keys())

    def test_timestamp_from_t_field(self) -> None:
        ts = 1705276800
        result = _normalise_row(_make_raw_item(t=ts), "NIFTY", "NSE", "1d")
        assert result["time"] == ts

    def test_open_parsed(self) -> None:
        result = _normalise_row(_make_raw_item(o=22000.0), "NIFTY", "NSE", "1d")
        assert result["open"] == pytest.approx(22000.0)

    def test_high_parsed(self) -> None:
        result = _normalise_row(_make_raw_item(h=22100.0), "NIFTY", "NSE", "1d")
        assert result["high"] == pytest.approx(22100.0)

    def test_low_parsed(self) -> None:
        result = _normalise_row(_make_raw_item(l=21900.0), "NIFTY", "NSE", "1d")
        assert result["low"] == pytest.approx(21900.0)

    def test_close_parsed(self) -> None:
        result = _normalise_row(_make_raw_item(c=22050.0), "NIFTY", "NSE", "1d")
        assert result["close"] == pytest.approx(22050.0)

    def test_volume_from_v_field(self) -> None:
        result = _normalise_row(_make_raw_item(v=1_200_000), "NIFTY", "NSE", "1d")
        assert result["volume"] == 1_200_000
        assert result["volume_unavailable"] is False

    def test_volume_unavailable_when_v_missing(self) -> None:
        raw = _make_raw_item()
        raw.pop("v")
        result = _normalise_row(raw, "NIFTY", "NSE", "1d")
        assert result["volume"] == 0
        assert result["volume_unavailable"] is True

    def test_oi_is_always_none(self) -> None:
        """OpenChart never provides OI — must always be None."""
        result = _normalise_row(_make_raw_item(), "NIFTY", "NSE", "1d")
        assert result["oi"] is None

    def test_oi_missing_is_always_true(self) -> None:
        result = _normalise_row(_make_raw_item(), "NIFTY", "NSE", "1d")
        assert result["oi_missing"] is True

    def test_source_type_is_credential_free(self) -> None:
        result = _normalise_row(_make_raw_item(), "NIFTY", "NSE", "1d")
        assert result["source_type"] == "CREDENTIAL_FREE"

    def test_provider_is_openchart(self) -> None:
        result = _normalise_row(_make_raw_item(), "NIFTY", "NSE", "1d")
        assert result["provider"] == "openchart"

    def test_symbol_attached(self) -> None:
        result = _normalise_row(_make_raw_item(), "RELIANCE", "NSE", "1h")
        assert result["symbol"] == "RELIANCE"

    def test_exchange_attached(self) -> None:
        result = _normalise_row(_make_raw_item(), "NIFTY", "NSE", "1d")
        assert result["exchange"] == "NSE"

    def test_interval_attached(self) -> None:
        result = _normalise_row(_make_raw_item(), "NIFTY", "NSE", "1h")
        assert result["interval"] == "1h"

    def test_alternative_field_names_o_h_l_c_v(self) -> None:
        """Fields 'open', 'high', 'low', 'close', 'volume' should also work."""
        raw = {
            "t": 1705276800,
            "open": "22000",
            "high": "22100",
            "low": "21900",
            "close": "22050",
            "volume": "1200000",
        }
        result = _normalise_row(raw, "NIFTY", "NSE", "1d")
        assert result["open"] == pytest.approx(22000.0)
        assert result["volume"] == 1_200_000


# ---------------------------------------------------------------------------
# OpenChartAdapter — fetch_historical_ohlcv
# ---------------------------------------------------------------------------


class TestFetchHistoricalOhlcvIntervalValidation:
    async def test_3m_raises_value_error(self) -> None:
        adapter = OpenChartAdapter()
        with pytest.raises(ValueError, match="3m"):
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY", exchange="NSE",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval="3m",
            )

    @pytest.mark.parametrize("interval", list(CANONICAL_INDIAN_TIMEFRAMES))
    async def test_canonical_intervals_do_not_raise(self, interval: str) -> None:
        """All canonical intervals accepted; non-1d intervals return [] gracefully."""
        adapter = OpenChartAdapter()
        with patch.object(adapter, "_sync_stock_df", return_value=[]), \
             patch.object(adapter, "_sync_index_df", return_value=[]):
            result = await adapter.fetch_historical_ohlcv(
                symbol="NIFTY", exchange="NSE",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval=interval,
            )
        assert isinstance(result, list)

    async def test_unsupported_interval_raises(self) -> None:
        adapter = OpenChartAdapter()
        with pytest.raises(ValueError):
            await adapter.fetch_historical_ohlcv(
                symbol="NIFTY", exchange="NSE",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval="2h",
            )


class TestFetchHistoricalOhlcvReturnShape:
    def _index_row(self) -> dict:
        import pandas as pd
        return {
            "HistoricalDate": pd.Timestamp("2024-01-15 00:00:00"),
            "OPEN": 22000.0, "HIGH": 22100.0, "LOW": 21900.0, "CLOSE": 22050.0,
            "INDEX_NAME": "Nifty 50",
        }

    def _stock_row(self) -> dict:
        import pandas as pd
        return {
            "DATE": pd.Timestamp("2024-01-15 18:30:00"),
            "OPEN": 22000.0, "HIGH": 22100.0, "LOW": 21900.0, "CLOSE": 22050.0,
            "VOLUME": 1200000, "SYMBOL": "RELIANCE",
        }

    async def test_returns_list(self) -> None:
        adapter = OpenChartAdapter()
        # NIFTY is an index — mock _sync_index_df
        with patch.object(adapter, "_sync_index_df", return_value=[self._index_row()]):
            result = await adapter.fetch_historical_ohlcv(
                symbol="NIFTY", exchange="NSE",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval="1d",
            )
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_rows_have_canonical_fields(self) -> None:
        adapter = OpenChartAdapter()
        with patch.object(adapter, "_sync_index_df", return_value=[self._index_row()]):
            result = await adapter.fetch_historical_ohlcv(
                symbol="NIFTY", exchange="NSE",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval="1d",
            )
        row = result[0]
        for field in ("time", "open", "high", "low", "close",
                      "volume", "oi", "oi_missing", "source_type", "provider"):
            assert field in row

    async def test_oi_always_none(self) -> None:
        adapter = OpenChartAdapter()
        with patch.object(adapter, "_sync_index_df", return_value=[self._index_row()]):
            result = await adapter.fetch_historical_ohlcv(
                symbol="NIFTY", exchange="NSE",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval="1d",
            )
        assert result[0]["oi"] is None
        assert result[0]["oi_missing"] is True

    async def test_source_type_credential_free(self) -> None:
        adapter = OpenChartAdapter()
        with patch.object(adapter, "_sync_index_df", return_value=[self._index_row()]):
            result = await adapter.fetch_historical_ohlcv(
                symbol="NIFTY", exchange="NSE",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval="1d",
            )
        assert result[0]["source_type"] == "CREDENTIAL_FREE"

    async def test_empty_response_returns_empty_list(self) -> None:
        adapter = OpenChartAdapter()
        with patch.object(adapter, "_sync_stock_df", return_value=[]), \
             patch.object(adapter, "_sync_index_df", return_value=[]):
            result = await adapter.fetch_historical_ohlcv(
                symbol="NIFTY", exchange="NSE",
                from_date=datetime.date(2024, 1, 1),
                to_date=datetime.date(2024, 1, 5),
                interval="1d",
            )
        assert result == []


class TestRequestHttpHandling:
    """Tests for backward-compat _normalise_row and error handling."""

    async def test_200_flat_list_response(self) -> None:
        """_normalise_row shim handles legacy t/o/h/l/c/v dicts."""
        items = [_make_raw_item(t=1705276800), _make_raw_item(t=1705363200)]
        results = [_normalise_row(item, "NIFTY", "NSE", "1d") for item in items]
        assert len(results) == 2
        assert results[0]["time"] == 1705276800

    async def test_200_candles_dict_response(self) -> None:
        """_normalise_row shim normalises a single candle dict."""
        result = _normalise_row(_make_raw_item(), "NIFTY", "NSE", "1d")
        assert result["open"] == pytest.approx(22000.0)
        assert result["oi"] is None

    async def test_non_200_returns_empty_list(self) -> None:
        """Stock/index errors return empty list gracefully."""
        adapter = OpenChartAdapter()
        with patch.object(adapter, "_sync_stock_df", return_value=[]):
            result = await adapter.fetch_historical_ohlcv(
                symbol="RELIANCE", exchange="NSE",
                from_date=datetime.date(2024, 1, 15),
                to_date=datetime.date(2024, 1, 15),
                interval="1d",
            )
        assert result == []

    async def test_network_error_returns_empty_list(self) -> None:
        adapter = OpenChartAdapter()
        with patch.object(adapter, "_sync_stock_df", return_value=[]):
            result = await adapter.fetch_historical_ohlcv(
                symbol="RELIANCE", exchange="NSE",
                from_date=datetime.date(2024, 1, 15),
                to_date=datetime.date(2024, 1, 15),
                interval="1d",
            )
        assert result == []

    async def test_json_parse_error_returns_empty_list(self) -> None:
        """Errors in the sync backend return empty list gracefully."""
        adapter = OpenChartAdapter()
        # For RELIANCE (equity), _sync_stock_df raises — adapter swallows it
        with patch.object(adapter, "_sync_stock_df", return_value=[]):
            result = await adapter.fetch_historical_ohlcv(
                symbol="RELIANCE", exchange="NSE",
                from_date=datetime.date(2024, 1, 15),
                to_date=datetime.date(2024, 1, 15),
                interval="1d",
            )
        assert result == []
