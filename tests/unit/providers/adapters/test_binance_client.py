"""
Unit tests for src/providers/adapters/binance_rest.py — Task 11.1.

Covers:
- Interval validation: all Binance intervals (including 3m) are accepted;
  unknown intervals raise ValueError.
- 3m is NOT banned for crypto — it must work without error.
- Kline normalisation: raw Binance array → canonical dict shape.
- get_klines: correct URL, params, and normalised output.
- get_ticker_price: correct URL and passthrough.
- get_exchange_info: with and without symbol filter.
- get_24hr_stats: correct URL.
- Futures endpoints: mark price, open interest, OI history, L/S ratio,
  funding rate history.
- HTTP error handling:
  - 4xx raised immediately (no retry).
  - 429 raised immediately (no retry; gateway handles Retry-After).
  - 5xx retried up to max_retries with backoff; final exception propagated.
- API key injection: X-MBX-APIKEY header present when key is supplied;
  absent otherwise.
- Context manager: session closed on __aexit__.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.8, 13.9
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.providers.adapters.binance_rest import (
    BINANCE_INTERVALS,
    PROVIDER_ID,
    BinanceClient,
    _normalise_kline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw_kline(
    open_time: int = 1705300000000,
    open_: str = "65000.00",
    high: str = "65500.00",
    low: str = "64800.00",
    close: str = "65200.00",
    volume: str = "1234.56",
    close_time: int = 1705303599999,
) -> list[Any]:
    """Build a minimal Binance kline array (12 elements)."""
    return [
        open_time,
        open_,
        high,
        low,
        close,
        volume,
        close_time,
        "80345678.90",   # quoteAssetVolume
        5000,            # numberOfTrades
        "600.00",        # takerBuyBaseAssetVolume
        "39000000.00",   # takerBuyQuoteAssetVolume
        "0",             # ignore
    ]


def _mock_response(
    status_code: int = 200,
    json_body: Any = None,
) -> MagicMock:
    """Build a mock ``httpx.Response``."""
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    if json_body is not None:
        mock.json.return_value = json_body
    else:
        mock.json.side_effect = ValueError("no body")

    if status_code >= 400:
        # Make raise_for_status() raise HTTPStatusError.
        request = httpx.Request("GET", "https://api.binance.com/test")
        mock.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=request,
            response=mock,
        )
    else:
        mock.raise_for_status.return_value = None
    return mock


def _client_with_mock_session(mock_session: AsyncMock) -> BinanceClient:
    """Create a BinanceClient pre-wired with a mock session."""
    client = BinanceClient(session=mock_session)
    return client


# ---------------------------------------------------------------------------
# Provider constants
# ---------------------------------------------------------------------------


class TestProviderConstants:
    def test_provider_id_is_binance(self) -> None:
        assert PROVIDER_ID == "binance"

    def test_binance_intervals_includes_3m(self) -> None:
        """3m must be supported for Binance (crypto exception — not Indian ban)."""
        assert "3m" in BINANCE_INTERVALS

    def test_binance_intervals_includes_standard_intervals(self) -> None:
        for interval in ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]:
            assert interval in BINANCE_INTERVALS

    def test_binance_intervals_includes_extended_intervals(self) -> None:
        for interval in ["3d", "1w", "1M"]:
            assert interval in BINANCE_INTERVALS


# ---------------------------------------------------------------------------
# Kline normaliser
# ---------------------------------------------------------------------------


class TestNormaliseKline:
    def test_returns_dict(self) -> None:
        result = _normalise_kline(_make_raw_kline())
        assert isinstance(result, dict)

    def test_canonical_keys_present(self) -> None:
        result = _normalise_kline(_make_raw_kline())
        assert set(result.keys()) == {"time", "open", "high", "low", "close", "volume", "closeTime"}

    def test_time_is_open_time_ms(self) -> None:
        result = _normalise_kline(_make_raw_kline(open_time=1705300000000))
        assert result["time"] == 1705300000000

    def test_close_time_preserved(self) -> None:
        result = _normalise_kline(_make_raw_kline(close_time=1705303599999))
        assert result["closeTime"] == 1705303599999

    def test_open_parsed_as_float(self) -> None:
        result = _normalise_kline(_make_raw_kline(open_="65000.00"))
        assert result["open"] == pytest.approx(65000.0)

    def test_high_parsed_as_float(self) -> None:
        result = _normalise_kline(_make_raw_kline(high="65500.00"))
        assert result["high"] == pytest.approx(65500.0)

    def test_low_parsed_as_float(self) -> None:
        result = _normalise_kline(_make_raw_kline(low="64800.00"))
        assert result["low"] == pytest.approx(64800.0)

    def test_close_parsed_as_float(self) -> None:
        result = _normalise_kline(_make_raw_kline(close="65200.00"))
        assert result["close"] == pytest.approx(65200.0)

    def test_volume_parsed_as_float(self) -> None:
        result = _normalise_kline(_make_raw_kline(volume="1234.56"))
        assert result["volume"] == pytest.approx(1234.56)

    def test_time_is_int(self) -> None:
        result = _normalise_kline(_make_raw_kline())
        assert isinstance(result["time"], int)

    def test_close_time_is_int(self) -> None:
        result = _normalise_kline(_make_raw_kline())
        assert isinstance(result["closeTime"], int)

    def test_open_is_float(self) -> None:
        result = _normalise_kline(_make_raw_kline())
        assert isinstance(result["open"], float)


# ---------------------------------------------------------------------------
# Interval validation
# ---------------------------------------------------------------------------


class TestIntervalValidation:
    async def test_3m_is_accepted_for_crypto(self) -> None:
        """3m must NOT raise ValueError for Binance crypto data."""
        klines = [_make_raw_kline()]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, klines))

        client = _client_with_mock_session(mock_session)
        # Must not raise.
        result = await client.get_klines("BTCUSDT", "3m", limit=1)
        assert len(result) == 1

    async def test_unknown_interval_raises_value_error(self) -> None:
        client = BinanceClient()
        with pytest.raises(ValueError, match="not a supported Binance interval"):
            await client.get_klines("BTCUSDT", "10m")  # 10m is not Binance-native

    async def test_all_binance_intervals_accepted(self) -> None:
        klines = [_make_raw_kline()]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, klines))

        client = _client_with_mock_session(mock_session)
        for interval in BINANCE_INTERVALS:
            result = await client.get_klines("BTCUSDT", interval, limit=1)
            assert isinstance(result, list)


# ---------------------------------------------------------------------------
# get_klines
# ---------------------------------------------------------------------------


class TestGetKlines:
    async def test_returns_normalised_list(self) -> None:
        raw_klines = [_make_raw_kline(), _make_raw_kline(open_time=1705303600000)]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, raw_klines))

        client = _client_with_mock_session(mock_session)
        result = await client.get_klines("BTCUSDT", "1h", limit=2)

        assert len(result) == 2
        assert result[0]["time"] == 1705300000000

    async def test_url_uses_api_v3_klines(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, []))

        client = _client_with_mock_session(mock_session)
        await client.get_klines("ETHUSDT", "1d")

        call_url = mock_session.get.call_args[0][0]
        assert "/api/v3/klines" in call_url

    async def test_symbol_passed_as_uppercase(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, []))

        client = _client_with_mock_session(mock_session)
        await client.get_klines("btcusdt", "1h")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["symbol"] == "BTCUSDT"

    async def test_start_ms_included_when_provided(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, []))

        client = _client_with_mock_session(mock_session)
        await client.get_klines("BTCUSDT", "1h", start_ms=1705300000000)

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params.get("startTime") == 1705300000000

    async def test_end_ms_included_when_provided(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, []))

        client = _client_with_mock_session(mock_session)
        await client.get_klines("BTCUSDT", "1h", end_ms=1705386400000)

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params.get("endTime") == 1705386400000

    async def test_start_ms_absent_when_not_provided(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, []))

        client = _client_with_mock_session(mock_session)
        await client.get_klines("BTCUSDT", "1h")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert "startTime" not in params

    async def test_empty_response_returns_empty_list(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, []))

        client = _client_with_mock_session(mock_session)
        result = await client.get_klines("BTCUSDT", "1m")
        assert result == []

    async def test_limit_passed_in_params(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, []))

        client = _client_with_mock_session(mock_session)
        await client.get_klines("BTCUSDT", "1h", limit=100)

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["limit"] == 100


# ---------------------------------------------------------------------------
# get_ticker_price
# ---------------------------------------------------------------------------


class TestGetTickerPrice:
    async def test_returns_price_dict(self) -> None:
        payload = {"symbol": "BTCUSDT", "price": "65000.00"}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, payload))

        client = _client_with_mock_session(mock_session)
        result = await client.get_ticker_price("BTCUSDT")

        assert result["symbol"] == "BTCUSDT"
        assert result["price"] == "65000.00"

    async def test_url_uses_api_v3_ticker_price(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, {"symbol": "BTCUSDT", "price": "1.0"})
        )

        client = _client_with_mock_session(mock_session)
        await client.get_ticker_price("BTCUSDT")

        call_url = mock_session.get.call_args[0][0]
        assert "/api/v3/ticker/price" in call_url

    async def test_symbol_uppercased(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, {"symbol": "ETHUSDT", "price": "3000.00"})
        )

        client = _client_with_mock_session(mock_session)
        await client.get_ticker_price("ethusdt")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["symbol"] == "ETHUSDT"


# ---------------------------------------------------------------------------
# get_exchange_info
# ---------------------------------------------------------------------------


class TestGetExchangeInfo:
    async def test_url_uses_api_v3_exchange_info(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, {"symbols": []})
        )

        client = _client_with_mock_session(mock_session)
        await client.get_exchange_info()

        call_url = mock_session.get.call_args[0][0]
        assert "/api/v3/exchangeInfo" in call_url

    async def test_no_symbol_param_when_none(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, {}))

        client = _client_with_mock_session(mock_session)
        await client.get_exchange_info(symbol=None)

        # params may be passed as positional or keyword arg
        call_args = mock_session.get.call_args
        params = call_args[1].get("params") if call_args[1] else {}
        if params is None and len(call_args[0]) > 1:
            params = call_args[0][1]
        assert params is not None and "symbol" not in params

    async def test_symbol_param_when_provided(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, {}))

        client = _client_with_mock_session(mock_session)
        await client.get_exchange_info(symbol="BTCUSDT")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["symbol"] == "BTCUSDT"

    async def test_returns_dict(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, {"timezone": "UTC"}))

        client = _client_with_mock_session(mock_session)
        result = await client.get_exchange_info()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# get_24hr_stats
# ---------------------------------------------------------------------------


class TestGet24hrStats:
    async def test_url_uses_api_v3_ticker_24hr(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, {"symbol": "BTCUSDT"})
        )

        client = _client_with_mock_session(mock_session)
        await client.get_24hr_stats("BTCUSDT")

        call_url = mock_session.get.call_args[0][0]
        assert "/api/v3/ticker/24hr" in call_url

    async def test_symbol_passed_uppercase(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(
            return_value=_mock_response(200, {"symbol": "ETHUSDT"})
        )

        client = _client_with_mock_session(mock_session)
        await client.get_24hr_stats("ethusdt")

        kwargs = mock_session.get.call_args[1]
        params = kwargs.get("params") or mock_session.get.call_args[0][1]
        assert params["symbol"] == "ETHUSDT"

    async def test_returns_dict(self) -> None:
        payload = {
            "symbol": "BTCUSDT",
            "priceChange": "500.00",
            "priceChangePercent": "0.77",
            "lastPrice": "65000.00",
        }
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, payload))

        client = _client_with_mock_session(mock_session)
        result = await client.get_24hr_stats("BTCUSDT")
        assert result["symbol"] == "BTCUSDT"
        assert result["lastPrice"] == "65000.00"


# ---------------------------------------------------------------------------
# Futures endpoints
# ---------------------------------------------------------------------------


class TestFuturesEndpoints:
    async def test_get_futures_mark_price_url(self) -> None:
        payload = {"symbol": "BTCUSDT", "markPrice": "65000.00"}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, payload))

        client = _client_with_mock_session(mock_session)
        result = await client.get_futures_mark_price("BTCUSDT")

        call_url = mock_session.get.call_args[0][0]
        assert "/fapi/v1/premiumIndex" in call_url
        assert result["markPrice"] == "65000.00"

    async def test_get_futures_open_interest_url(self) -> None:
        payload = {"symbol": "BTCUSDT", "openInterest": "12345.678"}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, payload))

        client = _client_with_mock_session(mock_session)
        result = await client.get_futures_open_interest("BTCUSDT")

        call_url = mock_session.get.call_args[0][0]
        assert "/fapi/v1/openInterest" in call_url
        assert result["openInterest"] == "12345.678"

    async def test_get_futures_oi_history_url(self) -> None:
        payload = [{"symbol": "BTCUSDT", "sumOpenInterest": "1000.00", "timestamp": 1705300000000}]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, payload))

        client = _client_with_mock_session(mock_session)
        result = await client.get_futures_oi_history("BTCUSDT", "1h")

        call_url = mock_session.get.call_args[0][0]
        assert "openInterestHist" in call_url
        assert isinstance(result, list)

    async def test_get_long_short_ratio_url(self) -> None:
        payload = [{"symbol": "BTCUSDT", "longShortRatio": "1.23", "timestamp": 1705300000000}]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, payload))

        client = _client_with_mock_session(mock_session)
        result = await client.get_long_short_ratio("BTCUSDT")

        call_url = mock_session.get.call_args[0][0]
        assert "globalLongShortAccountRatio" in call_url
        assert isinstance(result, list)

    async def test_get_funding_rate_history_url(self) -> None:
        payload = [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingTime": 1705300000000}]
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, payload))

        client = _client_with_mock_session(mock_session)
        result = await client.get_funding_rate_history("BTCUSDT")

        call_url = mock_session.get.call_args[0][0]
        assert "/fapi/v1/fundingRate" in call_url
        assert isinstance(result, list)

    async def test_futures_uses_fapi_base_url(self) -> None:
        """Futures endpoints must use fapi.binance.com, not api.binance.com."""
        payload = {"symbol": "BTCUSDT", "markPrice": "65000.00"}
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(200, payload))

        client = BinanceClient(
            base_url="https://api.binance.com",
            futures_base_url="https://fapi.binance.com",
            session=mock_session,
        )
        await client.get_futures_mark_price("BTCUSDT")

        call_url = mock_session.get.call_args[0][0]
        assert "fapi.binance.com" in call_url


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


class TestHttpErrorHandling:
    async def test_400_raised_immediately_no_retry(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(400))

        client = BinanceClient(session=mock_session, max_retries=3)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.get_klines("BTCUSDT", "1h")

        # Should only have been called once — no retry for 4xx.
        assert mock_session.get.call_count == 1
        assert exc_info.value.response.status_code == 400

    async def test_429_raised_immediately_no_retry(self) -> None:
        """HTTP 429 must NOT be retried — gateway handles Retry-After."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(429))

        client = BinanceClient(session=mock_session, max_retries=3)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.get_klines("BTCUSDT", "1h")

        assert mock_session.get.call_count == 1
        assert exc_info.value.response.status_code == 429

    async def test_404_raised_immediately_no_retry(self) -> None:
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(404))

        client = BinanceClient(session=mock_session, max_retries=3)
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_ticker_price("BTCUSDT")

        assert mock_session.get.call_count == 1

    async def test_5xx_retried_up_to_max_retries(self) -> None:
        """5xx errors should be retried up to max_retries times."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(503))

        client = BinanceClient(session=mock_session, max_retries=3)

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_klines("BTCUSDT", "1h")

        assert mock_session.get.call_count == 3

    async def test_5xx_succeeds_on_second_attempt(self) -> None:
        """Should succeed if 5xx is transient and succeeds on retry."""
        good_response = _mock_response(200, [_make_raw_kline()])
        bad_response = _mock_response(503)

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=[bad_response, good_response])

        client = BinanceClient(session=mock_session, max_retries=3)

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await client.get_klines("BTCUSDT", "1h")

        assert len(result) == 1
        assert mock_session.get.call_count == 2

    async def test_5xx_max_1_retry_raises_after_1(self) -> None:
        """With max_retries=1, a single 5xx exhausts retries."""
        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=_mock_response(500))

        client = BinanceClient(session=mock_session, max_retries=1)

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_klines("BTCUSDT", "1h")

        assert mock_session.get.call_count == 1


# ---------------------------------------------------------------------------
# API key injection
# ---------------------------------------------------------------------------


class TestApiKeyInjection:
    async def test_api_key_header_present_when_key_supplied(self) -> None:
        """X-MBX-APIKEY header must be set when api_key is provided."""
        # Create a client without injecting a pre-built session so the
        # client builds its own session with the headers we can inspect.
        client = BinanceClient(api_key="test-key-abc123")
        headers = client._build_headers()
        assert "X-MBX-APIKEY" in headers
        assert headers["X-MBX-APIKEY"] == "test-key-abc123"

    async def test_api_key_header_absent_when_no_key(self) -> None:
        """X-MBX-APIKEY header must NOT be present when api_key is None."""
        client = BinanceClient(api_key=None)
        headers = client._build_headers()
        assert "X-MBX-APIKEY" not in headers

    async def test_user_agent_always_set(self) -> None:
        client = BinanceClient()
        headers = client._build_headers()
        assert "User-Agent" in headers
        assert "data-service" in headers["User-Agent"]


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    async def test_aenter_returns_client(self) -> None:
        client = BinanceClient()
        async with client as c:
            assert c is client

    async def test_aexit_closes_owned_session(self) -> None:
        """When the client owns the session, __aexit__ must close it."""
        mock_session = MagicMock()
        mock_session.aclose = AsyncMock()

        client = BinanceClient()
        client._session = mock_session  # inject after construction
        client._owns_session = True

        await client.__aexit__(None, None, None)
        mock_session.aclose.assert_called_once()

    async def test_aexit_does_not_close_injected_session(self) -> None:
        """When an external session is injected, __aexit__ must NOT close it."""
        mock_session = MagicMock()
        mock_session.aclose = AsyncMock()

        client = BinanceClient(session=mock_session)  # external session
        await client.__aexit__(None, None, None)
        mock_session.aclose.assert_not_called()

    async def test_close_sets_session_to_none(self) -> None:
        mock_session = MagicMock()
        mock_session.aclose = AsyncMock()

        client = BinanceClient()
        client._session = mock_session
        client._owns_session = True

        await client.close()
        assert client._session is None


# ---------------------------------------------------------------------------
# Default base URL
# ---------------------------------------------------------------------------


class TestDefaultBaseUrls:
    def test_default_spot_base_url(self) -> None:
        client = BinanceClient()
        assert "api.binance.com" in client._base_url

    def test_default_futures_base_url(self) -> None:
        client = BinanceClient()
        assert "fapi.binance.com" in client._futures_base_url

    def test_custom_base_url(self) -> None:
        client = BinanceClient(base_url="https://testnet.binance.vision")
        assert client._base_url == "https://testnet.binance.vision"

    def test_trailing_slash_stripped_from_base_url(self) -> None:
        client = BinanceClient(base_url="https://api.binance.com/")
        assert not client._base_url.endswith("/")
