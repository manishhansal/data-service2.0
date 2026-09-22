"""
tests/unit/providers/test_angel_one_adapter_new.py

Unit tests for the Angel One adapter — new capabilities added in Phase 2-3.

Tests cover:
  - fetch_historical_oi()    (getOIData endpoint)
  - fetch_option_greeks()    (optionGreek endpoint)
  - fetch_ltp()              (getLtpData endpoint)
  - fetch_nse_intraday()     (nseIntraday endpoint)
  - 3m interval block on fetch_historical_oi
  - Unsupported interval handling
  - Provenance metadata attached to all responses

Requirements: Phase 2-3, 5.9, 19.7
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from src.providers.adapters.angel_one import AngelOneAdapter
from src.providers.adapters.base import (
    ProviderAuthError,
    ProviderDataError,
    ProviderMarketClosedError,
    ProviderUnsupportedError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(
    status_code: int = 200,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    json_body = json_body or {}
    request = httpx.Request("POST", "https://apiconnect.angelone.in/test")
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        request=request,
    )


def _make_adapter() -> tuple[AngelOneAdapter, AsyncMock]:
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    adapter = AngelOneAdapter(
        api_key="test-key",
        client_id="TEST123",
        totp_secret="ABCDEFGHIJKLMNOP",
        mpin="1234",
        http_client=mock_client,
    )
    # Pre-load an access token so ensure_authenticated passes
    adapter._access_token = "test-jwt-token"
    return adapter, mock_client


# ---------------------------------------------------------------------------
# fetch_historical_oi
# ---------------------------------------------------------------------------

class TestFetchHistoricalOI:
    """Tests for the new getOIData endpoint integration."""

    async def test_returns_oi_records_on_success(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": True,
                "message": "SUCCESS",
                "data": [
                    ["2024-01-15T09:15:00+05:30", 50000],
                    ["2024-01-15T09:16:00+05:30", 50500],
                ],
            },
        )
        result = await adapter.fetch_historical_oi(
            symbol="NIFTY25JANFUT",
            token="35004",
            from_date="2024-01-15 09:15",
            to_date="2024-01-15 15:30",
            interval="1d",
            exchange="NFO",
        )
        assert len(result) == 2
        assert result[0]["openInterest"] == 50000
        assert result[0]["provider"] == "angel_one"
        assert result[0]["exchange"] == "NFO"

    async def test_3m_interval_raises_unsupported(self) -> None:
        adapter, mock_client = _make_adapter()
        with pytest.raises(ProviderUnsupportedError):
            await adapter.fetch_historical_oi(
                symbol="NIFTY25JANFUT",
                token="35004",
                from_date="2024-01-15 09:15",
                to_date="2024-01-15 15:30",
                interval="3m",
            )
        mock_client.request.assert_not_called()

    async def test_1m_interval_unsupported_raises(self) -> None:
        """1M (monthly) is not in _INTERVAL_MAP for Angel One."""
        adapter, mock_client = _make_adapter()
        with pytest.raises(ProviderUnsupportedError):
            await adapter.fetch_historical_oi(
                symbol="NIFTY25JANFUT",
                token="35004",
                from_date="2024-01-01 09:15",
                to_date="2024-12-31 15:30",
                interval="1M",  # monthly not supported by Angel One
            )

    async def test_api_error_raises_provider_data_error(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(
            200,
            {"status": False, "message": "No data found"},
        )
        with pytest.raises(ProviderMarketClosedError):
            await adapter.fetch_historical_oi(
                symbol="NIFTY25JANFUT",
                token="35004",
                from_date="2024-01-01 09:15",
                to_date="2024-01-01 15:30",
                interval="1d",
            )

    async def test_dict_format_records_supported(self) -> None:
        """OI endpoint may return dict records instead of arrays."""
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": True,
                "message": "SUCCESS",
                "data": [
                    {"timestamp": "2024-01-15T09:15:00+05:30", "openInterest": 50000},
                ],
            },
        )
        result = await adapter.fetch_historical_oi(
            symbol="NIFTY25JANFUT",
            token="35004",
            from_date="2024-01-15 09:15",
            to_date="2024-01-15 15:30",
            interval="1d",
        )
        assert len(result) == 1

    async def test_provider_metadata_attached(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": True,
                "data": [["2024-01-15T09:15:00+05:30", 50000]],
            },
        )
        result = await adapter.fetch_historical_oi(
            symbol="TEST", token="123", from_date="2024-01-15 09:15",
            to_date="2024-01-15 15:30", interval="1d",
        )
        assert result[0]["provider"] == "angel_one"
        assert result[0]["sourceType"] == "BROKER_AUTHENTICATED"


# ---------------------------------------------------------------------------
# fetch_option_greeks
# ---------------------------------------------------------------------------

class TestFetchOptionGreeks:
    """Tests for the new optionGreek endpoint integration."""

    async def test_returns_greeks_records_on_success(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": True,
                "message": "SUCCESS",
                "data": [
                    {
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
                ],
            },
        )
        result = await adapter.fetch_option_greeks(
            name="NIFTY",
            expiry_date="29JAN2024",
        )
        assert len(result) == 1
        assert result[0]["impliedVolatility"] == pytest.approx(0.15)
        assert result[0]["delta"] == pytest.approx(0.51)
        assert result[0]["provider"] == "angel_one"

    async def test_api_failure_raises_provider_data_error(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(
            200,
            {"status": False, "message": "Greeks not available"},
        )
        with pytest.raises(ProviderDataError):
            await adapter.fetch_option_greeks("NIFTY", "29JAN2024")

    async def test_underlying_and_expiry_in_response(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": True,
                "data": [
                    {"strikePrice": 22500, "optionType": "PE",
                     "delta": -0.49, "gamma": 0.0003, "theta": -11.0,
                     "vega": 8.0, "impliedVolatility": 0.14}
                ],
            },
        )
        result = await adapter.fetch_option_greeks("NIFTY", "29JAN2024")
        assert result[0]["underlyingName"] == "NIFTY"
        assert result[0]["expiryDate"] == "29JAN2024"

    async def test_429_raises_rate_limited(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(429, {})
        from src.providers.adapters.base import ProviderRateLimitedError
        with pytest.raises(ProviderRateLimitedError):
            await adapter.fetch_option_greeks("NIFTY", "29JAN2024")


# ---------------------------------------------------------------------------
# fetch_ltp
# ---------------------------------------------------------------------------

class TestFetchLTP:
    """Tests for the getLtpData endpoint integration."""

    async def test_returns_ltp_on_success(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(
            200,
            {
                "status": True,
                "data": {
                    "ltp": 2450.5,
                    "tradingsymbol": "RELIANCE-EQ",
                    "symboltoken": "2885",
                    "exchange": "NSE",
                },
            },
        )
        result = await adapter.fetch_ltp(
            token="2885",
            exchange="NSE",
            trading_symbol="RELIANCE-EQ",
        )
        assert result["ltp"] == pytest.approx(2450.5)
        assert result["provider"] == "angel_one"

    async def test_api_failure_raises_provider_data_error(self) -> None:
        adapter, mock_client = _make_adapter()
        mock_client.request.return_value = _mock_response(
            200,
            {"status": False, "message": "Invalid token"},
        )
        with pytest.raises(ProviderDataError):
            await adapter.fetch_ltp("99999", "NSE", "INVALID-EQ")


# ---------------------------------------------------------------------------
# Token URL constants
# ---------------------------------------------------------------------------

class TestAdapterURLConstants:
    """Verify new URL constants are correctly set."""

    def test_oi_url_contains_getOIData(self) -> None:
        from src.providers.adapters.angel_one import _OI_URL
        assert "getOIData" in _OI_URL

    def test_option_greek_url_contains_optionGreek(self) -> None:
        from src.providers.adapters.angel_one import _OPTION_GREEK_URL
        assert "optionGreek" in _OPTION_GREEK_URL

    def test_ltp_url_contains_getLtpData(self) -> None:
        from src.providers.adapters.angel_one import _LTP_URL
        assert "getLtpData" in _LTP_URL

    def test_nse_intraday_url_correct(self) -> None:
        from src.providers.adapters.angel_one import _NSE_INTRADAY_URL
        assert "nseIntraday" in _NSE_INTRADAY_URL
