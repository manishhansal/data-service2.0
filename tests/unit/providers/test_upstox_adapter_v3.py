"""
tests/unit/providers/test_upstox_adapter_v3.py

Unit tests for the Upstox adapter V3 new capabilities.

Tests cover:
  - fetch_intraday_candles() (V3 intraday endpoint)
  - fetch_ltp()              (V3 LTP endpoint)
  - fetch_ohlc()             (V3 OHLC endpoint)
  - fetch_option_greeks()    (V3 option Greeks, max 50)
  - fetch_option_greeks_batched() (auto-batching)
  - fetch_option_chain()     (V2 option chain)
  - fetch_exchange_status()  (V2 market status)
  - fetch_ws_authorized_url() (WebSocket auth URL)
  - set_analytics_token()    (Analytics Token support)
  - OI in V3 historical candles (index 6)
  - 1M interval supported in V3
  - Batch limit enforcement (>50 raises ValueError)

Requirements: Phase 2-3, Upstox V3 migration
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from src.providers.adapters.upstox import (
    INTERVAL_MAP_V3,
    UPSTOX_V3_BASE,
    ProviderAuthError,
    ProviderRateLimitedError,
    UpstoxAdapter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(
    status_code: int = 200,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    json_body = json_body or {}
    headers = headers or {}
    request = httpx.Request("GET", "https://api.upstox.com/v3/test")
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        headers=headers,
        request=request,
    )


def _candles_v3(candles: list[Any]) -> dict[str, Any]:
    return {"status": "success", "data": {"candles": candles}}


def _make_adapter(mock_client: AsyncMock | None = None) -> UpstoxAdapter:
    client = mock_client or AsyncMock(spec=httpx.AsyncClient)
    adapter = UpstoxAdapter(
        api_key="test-key",
        api_secret="test-secret",
        redirect_uri="https://localhost/callback",
        http_client=client,
    )
    return adapter


# ---------------------------------------------------------------------------
# V3 URL and interval map
# ---------------------------------------------------------------------------

class TestV3URLAndIntervals:
    """Verify V3 base URL and interval map are correct."""

    def test_v3_base_url(self) -> None:
        assert UPSTOX_V3_BASE == "https://api.upstox.com/v3"

    def test_1m_maps_to_minutes_1(self) -> None:
        assert INTERVAL_MAP_V3["1m"] == ("minutes", 1)

    def test_1h_maps_to_hours_1(self) -> None:
        assert INTERVAL_MAP_V3["1h"] == ("hours", 1)

    def test_1d_maps_to_days_1(self) -> None:
        assert INTERVAL_MAP_V3["1d"] == ("days", 1)

    def test_1w_maps_to_weeks_1(self) -> None:
        assert INTERVAL_MAP_V3["1w"] == ("weeks", 1)

    def test_1M_maps_to_months_1(self) -> None:
        """Monthly candles are supported in V3 — not available in V2."""
        assert INTERVAL_MAP_V3["1M"] == ("months", 1)

    def test_3m_absent_from_v3_map(self) -> None:
        """3m must not appear in V3 interval map for Indian market data."""
        assert "3m" not in INTERVAL_MAP_V3

    def test_all_canonical_intervals_covered(self) -> None:
        expected = {"1m", "5m", "10m", "15m", "30m", "1h", "1d", "1w", "1M"}
        assert expected <= set(INTERVAL_MAP_V3.keys())


# ---------------------------------------------------------------------------
# Historical candle V3 — OI extraction
# ---------------------------------------------------------------------------

class TestHistoricalCandleV3OI:
    """V3 candles include OI at index 6."""

    async def test_oi_extracted_from_v3_candle(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200,
            _candles_v3([
                ["2024-01-15T09:15:00+05:30", 500.0, 510.0, 495.0, 505.0, 10000, 98765],
            ]),
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        result = await adapter.fetch_historical_ohlcv(
            "NSE_FO|43985", "2024-01-15", "2024-01-15", "1m"
        )
        assert len(result) == 1
        assert result[0]["open_interest"] == 98765

    async def test_oi_none_when_absent_from_v3(self) -> None:
        """V3 returns OI=0 for cash — treat as None (index not present or 0)."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200,
            _candles_v3([
                ["2024-01-15T09:15:00+05:30", 500.0, 510.0, 495.0, 505.0, 10000],
                # no index 6
            ]),
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        result = await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2024-01-15", "2024-01-15", "1d"
        )
        assert result[0]["open_interest"] is None

    async def test_v3_url_contains_unit_and_interval(self) -> None:
        """V3 URL must use {unit}/{interval_value} path, not named strings."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(200, _candles_v3([]))
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1m"
        )
        call_url = mock_client.request.call_args[0][1]
        assert "/v3/historical-candle" in call_url
        assert "minutes" in call_url
        # Must NOT use old V2 named strings like "1minute"
        assert "1minute" not in call_url

    async def test_1M_interval_works_in_v3(self) -> None:
        """Monthly candles work in V3 (not supported in V2)."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(200, _candles_v3([]))
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        result = await adapter.fetch_historical_ohlcv(
            "NSE_EQ|INE002A01018", "2020-01-01", "2024-01-01", "1M"
        )
        call_url = mock_client.request.call_args[0][1]
        assert "months" in call_url
        assert result == []


# ---------------------------------------------------------------------------
# Intraday candles V3
# ---------------------------------------------------------------------------

class TestIntradayCandlesV3:
    """Upstox V3 intraday candle endpoint."""

    async def test_intraday_returns_candles(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200,
            _candles_v3([
                ["2024-01-15T09:15:00+05:30", 500.0, 510.0, 495.0, 505.0, 5000, 0],
                ["2024-01-15T09:16:00+05:30", 505.0, 512.0, 500.0, 508.0, 4500, 0],
            ]),
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        result = await adapter.fetch_intraday_candles("NSE_EQ|INE002A01018", "1m")
        assert len(result) == 2
        assert result[0]["candle_type"] == "INTRADAY"

    async def test_intraday_url_contains_intraday_path(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(200, _candles_v3([]))
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        await adapter.fetch_intraday_candles("NSE_EQ|INE002A01018", "1m")
        url = mock_client.request.call_args[0][1]
        assert "intraday" in url
        assert "/v3/historical-candle/intraday" in url

    async def test_intraday_3m_blocked(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("token")
        with pytest.raises(ValueError, match="3m"):
            await adapter.fetch_intraday_candles("NSE_EQ|INE002A01018", "3m")

    async def test_last_candle_marked_incomplete(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200,
            _candles_v3([
                ["2024-01-15T09:15:00+05:30", 500.0, 510.0, 495.0, 505.0, 5000, 0],
                ["2024-01-15T09:16:00+05:30", 505.0, 512.0, 500.0, 508.0, 100, 0],
            ]),
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        result = await adapter.fetch_intraday_candles("NSE_EQ|INE002A01018", "1m")
        # Last candle is not complete
        assert result[-1]["is_complete"] is False
        # First candle is complete
        assert result[0]["is_complete"] is True


# ---------------------------------------------------------------------------
# LTP V3
# ---------------------------------------------------------------------------

class TestLTPV3:
    """Upstox V3 LTP endpoint."""

    async def test_ltp_returns_data(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": "success",
                "data": {
                    "NSE_EQ|INE002A01018": {
                        "last_price": 2450.5,
                        "instrument_token": "NSE_EQ|INE002A01018",
                        "ltq": 10,
                        "volume": 500000,
                        "cp": 2400.0,
                    }
                },
            },
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        result = await adapter.fetch_ltp(["NSE_EQ|INE002A01018"])
        assert "NSE_EQ|INE002A01018" in result
        assert result["NSE_EQ|INE002A01018"]["last_price"] == pytest.approx(2450.5)

    async def test_ltp_url_is_v3(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200,
            {"status": "success", "data": {}},
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        await adapter.fetch_ltp(["NSE_EQ|INE002A01018"])
        url = mock_client.request.call_args[0][1]
        assert "/v3/market-quote/ltp" in url

    async def test_empty_keys_raises_value_error(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("token")
        with pytest.raises(ValueError):
            await adapter.fetch_ltp([])


# ---------------------------------------------------------------------------
# Option Greeks V3
# ---------------------------------------------------------------------------

class TestOptionGreeksV3:
    """Upstox V3 option Greeks endpoint."""

    def _greeks_response(self) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "NSE_FO:NIFTY2540923000PE": {
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
            },
        }

    async def test_returns_greeks_data(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(200, self._greeks_response())
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        result = await adapter.fetch_option_greeks(["NSE_FO|43885"])
        assert len(result) == 1

    async def test_greeks_url_is_v3(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        await adapter.fetch_option_greeks(["NSE_FO|43885"])
        url = mock_client.request.call_args[0][1]
        assert "/v3/market-quote/option-greek" in url

    async def test_batch_limit_50_enforced(self) -> None:
        adapter = _make_adapter()
        await adapter.set_access_token("token")
        keys = [f"NSE_FO|{i}" for i in range(51)]  # 51 keys
        with pytest.raises(ValueError, match="50"):
            await adapter.fetch_option_greeks(keys)

    async def test_batched_splits_correctly(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        keys = [f"NSE_FO|{i}" for i in range(75)]
        await adapter.fetch_option_greeks_batched(keys, batch_size=50)
        # Should have made 2 calls (50 + 25)
        assert mock_client.request.call_count == 2


# ---------------------------------------------------------------------------
# Option chain V2
# ---------------------------------------------------------------------------

class TestOptionChainV2:
    """Upstox V2 option chain endpoint."""

    async def test_returns_chain_data(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": "success",
                "data": {
                    "pcr": 1.25,
                    "expiry_date": "2024-01-25",
                    "contracts": [],
                },
            },
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        result = await adapter.fetch_option_chain(
            underlying_key="NSE_INDEX|Nifty 50",
            expiry_date="2024-01-25",
        )
        assert isinstance(result, dict)

    async def test_option_chain_url_is_v2_option_chain(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        await adapter.fetch_option_chain("NSE_INDEX|Nifty 50", "2024-01-25")
        url = mock_client.request.call_args[0][1]
        assert "/v2/option/chain" in url


# ---------------------------------------------------------------------------
# WebSocket authorization URL
# ---------------------------------------------------------------------------

class TestWSAuthURL:
    """WebSocket authorization URL fetch."""

    async def test_returns_authorized_uri(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": "success",
                "data": {
                    "authorized_redirect_uri": "wss://api.upstox.com/feed?code=abc123"
                },
            },
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        uri = await adapter.fetch_ws_authorized_url()
        assert uri.startswith("wss://")
        assert "code=abc123" in uri

    async def test_missing_uri_raises_key_error(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {}}  # missing authorized_redirect_uri
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        with pytest.raises(KeyError, match="authorized_redirect_uri"):
            await adapter.fetch_ws_authorized_url()


# ---------------------------------------------------------------------------
# Analytics Token support
# ---------------------------------------------------------------------------

class TestAnalyticsToken:
    """Analytics Token (March 2026) — long-lived read-only token."""

    async def test_analytics_token_stored(self) -> None:
        adapter = _make_adapter()
        await adapter.set_analytics_token("analytics-token-123")
        assert adapter._analytics_token == "analytics-token-123"

    async def test_analytics_token_takes_priority_over_oauth(self) -> None:
        """When analytics token is set, it's used even if access_token is set."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200, {"status": "success", "data": {"candles": []}}
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("oauth-token")
        await adapter.set_analytics_token("analytics-token")
        await adapter.fetch_historical_ohlcv("NSE_EQ|INE002A01018", "2024-01-01", "2024-01-31", "1d")
        call_headers = mock_client.request.call_args.kwargs.get("headers", {})
        assert "analytics-token" in call_headers.get("Authorization", "")

    async def test_no_token_raises_when_neither_set(self) -> None:
        adapter = _make_adapter()
        # Neither token set
        with pytest.raises(ProviderAuthError):
            await adapter.ensure_authenticated()

    async def test_analytics_token_sufficient_for_auth(self) -> None:
        adapter = _make_adapter()
        await adapter.set_analytics_token("analytics-token")
        # Should not raise
        await adapter.ensure_authenticated()


# ---------------------------------------------------------------------------
# Exchange status
# ---------------------------------------------------------------------------

class TestExchangeStatus:
    """Exchange status API."""

    async def test_returns_status(self) -> None:
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": "success",
                "data": {
                    "exchange": "NSE",
                    "status": "NORMAL_OPEN",
                    "last_updated": 1705549500000,
                },
            },
        )
        adapter = _make_adapter(mock_client)
        await adapter.set_access_token("token")
        result = await adapter.fetch_exchange_status("NSE")
        assert result.get("status") == "NORMAL_OPEN"
        assert result.get("exchange") == "NSE"
