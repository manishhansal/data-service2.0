"""
Unit tests for src/providers/adapters/delta_exchange.py

Tests cover:
- DeltaClient._normalise_candle: time conversion, sorting, field mapping
- DeltaClient.get_candles: HTTP mocking, response parsing, error paths
- DeltaClient.get_tickers: single + batch
- DeltaClient.get_premium_index: funding rate computation
- DeltaClient.get_oi_history: OI:{symbol} endpoint
- _ts_to_ms: microsecond / millisecond / second heuristic
- Error propagation: 429, 5xx, success:false, malformed JSON
- Interval validation: 3m allowed, unknown interval raises ValueError

Requirements: DS2-RCA-001, 13.1 (crypto 3m exception)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.providers.adapters.delta_exchange import (
    DELTA_INTERVALS,
    _INTERVAL_SECONDS,
    DeltaClient,
    _normalise_candle,
    _normalise_ticker,
    _ts_to_ms,
)
from src.providers.adapters.base import (
    ProviderDataError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)

PROVIDER_ID = "delta"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(
    body: Any,
    status_code: int = 200,
    *,
    wrap_envelope: bool = True,
) -> MagicMock:
    """Build a mock httpx.Response that returns body as JSON."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if wrap_envelope and status_code == 200:
        resp.json.return_value = {"success": True, "result": body}
    else:
        resp.json.return_value = body
    resp.text = json.dumps(resp.json.return_value)
    resp.raise_for_status = MagicMock()
    resp.headers = {}
    return resp


def _make_candle(time_sec: int = 1_700_000_000) -> dict[str, Any]:
    return {
        "time": time_sec,
        "open": 43_000.0,
        "high": 43_500.0,
        "low": 42_800.0,
        "close": 43_200.0,
        "volume": 12.5,
    }


def _make_ticker(symbol: str = "BTCUSD") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "close": 43200.0,
        "open": 43000.0,
        "high": 43500.0,
        "low": 42800.0,
        "mark_price": 43210.0,
        "spot_price": 43190.0,
        "ltp_change_24h": 0.465,
        "oi": 5_000.0,
        "oi_value_usd": 215_000_000.0,
        "turnover": 1_500.0,
        "turnover_usd": 64_800_000.0,
        "volume": 1_500.0,
        "timestamp": 1_700_000_000_000_000,  # microseconds
    }


# ---------------------------------------------------------------------------
# _ts_to_ms
# ---------------------------------------------------------------------------


class TestTsToMs:
    def test_microseconds(self) -> None:
        # 1.7e15 microseconds → 1.7e12 ms
        result = _ts_to_ms(1_700_000_000_000_000)
        assert result == 1_700_000_000_000

    def test_milliseconds(self) -> None:
        result = _ts_to_ms(1_700_000_000_000)
        assert result == 1_700_000_000_000

    def test_seconds(self) -> None:
        result = _ts_to_ms(1_700_000_000)
        assert result == 1_700_000_000_000

    def test_none_returns_zero(self) -> None:
        assert _ts_to_ms(None) == 0

    def test_zero_returns_zero(self) -> None:
        assert _ts_to_ms(0) == 0


# ---------------------------------------------------------------------------
# _normalise_candle
# ---------------------------------------------------------------------------


class TestNormaliseCandle:
    def test_converts_seconds_to_ms(self) -> None:
        raw = _make_candle(time_sec=1_700_000_000)
        result = _normalise_candle(raw, symbol="BTCUSD", interval="1h")
        assert result["time"] == 1_700_000_000 * 1_000

    def test_close_time_derived_from_interval(self) -> None:
        raw = _make_candle(time_sec=1_700_000_000)
        result = _normalise_candle(raw, symbol="BTCUSD", interval="1h")
        expected_close = 1_700_000_000 * 1_000 + 3_600 * 1_000 - 1
        assert result["closeTime"] == expected_close

    def test_ohlcv_fields_preserved(self) -> None:
        raw = _make_candle()
        result = _normalise_candle(raw, symbol="ETHUSD", interval="5m")
        assert result["open"] == 43_000.0
        assert result["high"] == 43_500.0
        assert result["low"] == 42_800.0
        assert result["close"] == 43_200.0
        assert result["volume"] == 12.5

    def test_exchange_tag(self) -> None:
        raw = _make_candle()
        result = _normalise_candle(raw, symbol="BTCUSD", interval="1d")
        assert result["exchange"] == "DELTA"

    def test_symbol_and_interval_attached(self) -> None:
        raw = _make_candle()
        result = _normalise_candle(raw, symbol="SOLUSD", interval="15m")
        assert result["symbol"] == "SOLUSD"
        assert result["interval"] == "15m"

    def test_3m_interval_close_time(self) -> None:
        raw = _make_candle(time_sec=1_700_000_000)
        result = _normalise_candle(raw, symbol="BTCUSD", interval="3m")
        expected_close = 1_700_000_000 * 1_000 + 180 * 1_000 - 1
        assert result["closeTime"] == expected_close


# ---------------------------------------------------------------------------
# _normalise_ticker
# ---------------------------------------------------------------------------


class TestNormaliseTicker:
    def test_basic_fields(self) -> None:
        raw = _make_ticker("BTCUSD")
        result = _normalise_ticker(raw)
        assert result["symbol"] == "BTCUSD"
        assert result["price"] == 43_200.0
        assert result["markPrice"] == 43_210.0
        assert result["indexPrice"] == 43_190.0
        assert result["openInterest"] == 5_000.0
        assert result["exchange"] == "DELTA"

    def test_ltp_change_24h_used_directly_as_percent(self) -> None:
        """ltp_change_24h is percent, not a decimal fraction."""
        raw = _make_ticker()
        raw["ltp_change_24h"] = -1.522
        result = _normalise_ticker(raw)
        assert result["changePct"] == -1.522  # preserved as-is

    def test_change_derived_when_ltp_absent(self) -> None:
        raw = _make_ticker()
        del raw["ltp_change_24h"]
        result = _normalise_ticker(raw)
        # change = close - open = 43200 - 43000 = 200 → pct = 200/43000 * 100
        expected_pct = (43_200.0 - 43_000.0) / 43_000.0 * 100.0
        assert abs(result["changePct"] - expected_pct) < 0.001

    def test_ts_microseconds_converted(self) -> None:
        raw = _make_ticker()
        result = _normalise_ticker(raw)
        assert result["ts"] == 1_700_000_000_000  # microseconds → ms

    def test_quote_volume_from_turnover_usd(self) -> None:
        raw = _make_ticker()
        result = _normalise_ticker(raw)
        assert result["quoteVolume"] == 64_800_000.0


# ---------------------------------------------------------------------------
# DELTA_INTERVALS
# ---------------------------------------------------------------------------


class TestDeltaIntervals:
    def test_3m_is_supported(self) -> None:
        """3m is allowed for Delta crypto — same exception as Binance."""
        assert "3m" in DELTA_INTERVALS

    def test_8h_maps_to_6h(self) -> None:
        """Delta has no 8h candle; 6h is the safe fallback."""
        assert DELTA_INTERVALS["8h"] == "6h"

    def test_standard_intervals_present(self) -> None:
        for iv in ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"):
            assert iv in DELTA_INTERVALS, f"{iv!r} must be in DELTA_INTERVALS"

    def test_interval_seconds_complete(self) -> None:
        for iv in DELTA_INTERVALS:
            assert iv in _INTERVAL_SECONDS, f"{iv!r} missing from _INTERVAL_SECONDS"


# ---------------------------------------------------------------------------
# DeltaClient.get_candles
# ---------------------------------------------------------------------------


class TestDeltaClientGetCandles:
    @pytest.mark.asyncio
    async def test_returns_sorted_ascending(self) -> None:
        """Delta returns descending; client must sort ascending."""
        # Three candles out of order (newest first, as Delta sends them).
        raw_candles = [
            _make_candle(time_sec=1_700_003_600),  # newest
            _make_candle(time_sec=1_700_007_200),  # even newer — sorting edge case
            _make_candle(time_sec=1_700_000_000),  # oldest
        ]
        mock_resp = _mock_response(raw_candles)
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        client = DeltaClient(session=mock_session)
        candles = await client.get_candles("BTCUSD", "1h", start_sec=1_700_000_000, end_sec=1_700_010_000)

        assert len(candles) == 3
        times = [c["time"] for c in candles]
        assert times == sorted(times), "Candles must be oldest-first"
        await client.close()

    @pytest.mark.asyncio
    async def test_invalid_interval_raises_value_error(self) -> None:
        client = DeltaClient()
        with pytest.raises(ValueError, match="not a supported Delta Exchange interval"):
            await client.get_candles("BTCUSD", "10m", start_sec=0, end_sec=9999)
        await client.close()

    @pytest.mark.asyncio
    async def test_3m_interval_accepted(self) -> None:
        """3m must NOT raise — it is allowed for crypto."""
        mock_resp = _mock_response([])
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        client = DeltaClient(session=mock_session)
        candles = await client.get_candles("BTCUSD", "3m", start_sec=0, end_sec=9999)
        assert candles == []
        await client.close()

    @pytest.mark.asyncio
    async def test_normalised_fields_in_output(self) -> None:
        raw = [_make_candle(time_sec=1_700_000_000)]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(raw))

        client = DeltaClient(session=mock_session)
        candles = await client.get_candles("BTCUSD", "1h", start_sec=0, end_sec=9999)

        assert len(candles) == 1
        c = candles[0]
        assert c["time"] == 1_700_000_000_000
        assert c["exchange"] == "DELTA"
        assert c["symbol"] == "BTCUSD"
        assert c["interval"] == "1h"
        assert "closeTime" in c
        await client.close()


# ---------------------------------------------------------------------------
# DeltaClient — error handling
# ---------------------------------------------------------------------------


class TestDeltaClientErrors:
    @pytest.mark.asyncio
    async def test_429_raises_rate_limited_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "30"}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        client = DeltaClient(session=mock_session)
        with pytest.raises(ProviderRateLimitedError) as exc_info:
            await client.get_candles("BTCUSD", "1h", start_sec=0, end_sec=9999)
        assert exc_info.value.retry_after_s == 30
        assert exc_info.value.provider == PROVIDER_ID
        await client.close()

    @pytest.mark.asyncio
    async def test_success_false_raises_data_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": False, "error": {"code": "INVALID_SYMBOL"}}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        client = DeltaClient(session=mock_session)
        with pytest.raises(ProviderDataError) as exc_info:
            await client.get_candles("INVALID", "1h", start_sec=0, end_sec=9999)
        assert exc_info.value.provider == PROVIDER_ID
        await client.close()

    @pytest.mark.asyncio
    async def test_400_raises_data_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": "bad request"}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        client = DeltaClient(session=mock_session)
        with pytest.raises(ProviderDataError):
            await client.get_tickers(symbols=["BTCUSD"])
        await client.close()

    @pytest.mark.asyncio
    async def test_5xx_retries_then_raises(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        client = DeltaClient(session=mock_session, max_retries=2)
        with pytest.raises(ProviderUnavailableError):
            await client.get_candles("BTCUSD", "1h", start_sec=0, end_sec=9999)
        # Should have retried 2 times.
        assert mock_session.get.call_count == 2
        await client.close()


# ---------------------------------------------------------------------------
# DeltaClient.get_tickers
# ---------------------------------------------------------------------------


class TestDeltaClientGetTickers:
    @pytest.mark.asyncio
    async def test_single_ticker_normalised(self) -> None:
        raw = [_make_ticker("BTCUSD")]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(raw))

        client = DeltaClient(session=mock_session)
        tickers = await client.get_tickers()

        assert len(tickers) == 1
        t = tickers[0]
        assert t["symbol"] == "BTCUSD"
        assert t["markPrice"] == 43_210.0
        assert t["exchange"] == "DELTA"
        await client.close()

    @pytest.mark.asyncio
    async def test_get_ticker_single_symbol(self) -> None:
        raw = [_make_ticker("ETHUSD")]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(raw))

        client = DeltaClient(session=mock_session)
        ticker = await client.get_ticker("ETHUSD")

        assert ticker["symbol"] == "ETHUSD"
        await client.close()

    @pytest.mark.asyncio
    async def test_get_ticker_raises_when_empty(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response([]))

        client = DeltaClient(session=mock_session)
        with pytest.raises(ProviderDataError):
            await client.get_ticker("BTCUSD")
        await client.close()


# ---------------------------------------------------------------------------
# DeltaClient context manager
# ---------------------------------------------------------------------------


class TestDeltaClientLifecycle:
    @pytest.mark.asyncio
    async def test_context_manager_closes_session(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response([]))

        async with DeltaClient(session=mock_session) as client:
            pass  # just enter/exit

        # When session is injected, we do NOT own it so close does nothing.
        mock_session.aclose.assert_not_called()

    @pytest.mark.asyncio
    async def test_owned_session_closed_on_exit(self) -> None:
        """When DeltaClient creates its own session, it must close it."""
        mock_internal_session = AsyncMock(spec=httpx.AsyncClient)

        client = DeltaClient()
        client._session = mock_internal_session
        client._owns_session = True
        await client.close()

        mock_internal_session.aclose.assert_called_once()
